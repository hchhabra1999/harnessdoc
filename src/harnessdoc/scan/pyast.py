"""Static analysis of one Python module: find LLM call sites and resolve what goes into them."""

from __future__ import annotations

import ast
from pathlib import Path

from .model import CallSite, Resolved

CHARS_PER_TOKEN = 4  # rough, deterministic; real counts come from usage logs

SDK_ROOTS = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google.genai": "gemini",
    "google.generativeai": "gemini",
    "litellm": "litellm",
}

CLIENT_CONSTRUCTORS = {
    "anthropic": {"Anthropic", "AsyncAnthropic", "AnthropicBedrock", "AnthropicBedrockMantle",
                  "AnthropicVertex", "AnthropicFoundry"},
    "openai": {"OpenAI", "AsyncOpenAI", "AzureOpenAI", "AsyncAzureOpenAI"},
    "gemini": {"Client", "GenerativeModel"},
}

# (api suffix, provider, is_batch). Longest suffixes first; "beta" segments are stripped before matching.
API_PATTERNS: list[tuple[tuple[str, ...], str, bool]] = [
    (("messages", "batches", "create"), "anthropic", True),
    (("chat", "completions", "create"), "openai", False),
    (("chat", "completions", "parse"), "openai", False),
    (("chat", "completions", "stream"), "openai", False),
    (("models", "generate_content_stream"), "gemini", False),
    (("models", "generate_content"), "gemini", False),
    (("messages", "create"), "anthropic", False),
    (("messages", "stream"), "anthropic", False),
    (("messages", "parse"), "anthropic", False),
    (("responses", "create"), "openai", False),
    (("responses", "parse"), "openai", False),
    (("responses", "stream"), "openai", False),
    (("batches", "create"), "openai", True),
    (("generate_content",), "gemini", False),
]
LITELLM_FUNCS = {"completion", "acompletion", "batch_completion"}

VOLATILE_CALLS = (
    "datetime.now", "datetime.utcnow", "datetime.today", "date.today", "time.time", "time.time_ns",
    "uuid.uuid4", "uuid.uuid1", "uuid4", "random.random", "random.randint", "random.choice", "os.getpid",
)
MAX_TOKEN_KWARGS = ("max_tokens", "max_completion_tokens", "max_output_tokens")
TEST_DIR_NAMES = {"tests", "test", "evals", "eval"}


def is_test_path(rel: str) -> bool:
    p = Path(rel)
    return (
        any(part in TEST_DIR_NAMES for part in p.parts[:-1])
        or p.name.startswith("test_")
        or p.name.endswith("_test.py")
    )


def dotted(node: ast.AST) -> str | None:
    """`a.b.c` -> "a.b.c"; calls in the chain are looked through: `A().b` -> "A().b"."""
    parts: list[str] = []
    while True:
        if isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Name):
            parts.append(node.id)
            break
        elif isinstance(node, ast.Call):
            inner = dotted(node.func)
            if inner is None:
                return None
            parts.append(inner + "()")
            break
        else:
            return None
    return ".".join(reversed(parts))


def _docstring(node: ast.AST) -> str:
    try:
        return ast.get_docstring(node) or ""  # type: ignore[arg-type]
    except TypeError:
        return ""


def module_name(rel: str) -> str:
    parts = list(Path(rel).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class Project:
    """All analysed modules, so names imported from sibling modules can be resolved."""

    def __init__(self) -> None:
        self.modules: dict[str, ModuleAnalyzer] = {}

    def add(self, mod: ModuleAnalyzer) -> None:
        self.modules[mod.module] = mod

    def lookup(self, qualified: str) -> tuple[ModuleAnalyzer, str] | None:
        """'acme_support.tools.build_tools' -> (tools module, 'build_tools'). Also matches by suffix so
        imports written relative to a different source root (e.g. 'src.' prefixes) still resolve."""
        mod_name, _, attr = qualified.rpartition(".")
        if not mod_name:
            return None
        if mod_name in self.modules:
            return self.modules[mod_name], attr
        candidates = sorted(m for m in self.modules if m.endswith("." + mod_name))
        if len(candidates) == 1:
            return self.modules[candidates[0]], attr
        return None


class ModuleAnalyzer:
    def __init__(self, path: Path, rel: str, repo_files: list[str], project: Project | None = None):
        self.path = path
        self.rel = rel
        self.repo_files = repo_files
        self.project = project
        self.module = module_name(rel)
        self.package = self.module if Path(rel).name == "__init__.py" else self.module.rpartition(".")[0]
        self.source = path.read_text(encoding="utf-8", errors="replace")
        self.tree = ast.parse(self.source, filename=rel)
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent

        self.imports: dict[str, str] = {}  # local name -> fully qualified
        self.constants: dict[str, ast.expr] = {}
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.clients: dict[str, str] = {}  # variable (or "self.x") -> provider
        self._collect()

    # ------------------------------------------------------------------ symbol tables

    def _collect(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.imports[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:  # relative import: resolve against this module's package
                    pkg = self.package.split(".") if self.package else []
                    pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                    base = ".".join(filter(None, [*pkg, base]))
                for a in node.names:
                    self.imports[a.asname or a.name] = f"{base}.{a.name}" if base else a.name
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                self.constants[node.targets[0].id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
                self.constants[node.target.id] = node.value
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                self.functions[node.name] = node
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                provider = self._constructor_provider(node.value)
                if provider:
                    for t in node.targets:
                        name = dotted(t)
                        if name:
                            self.clients[name] = provider

    def qualify(self, name: str) -> str:
        head, _, rest = name.partition(".")
        full = self.imports.get(head, head)
        return f"{full}.{rest}" if rest else full

    def sdk_of(self, qualified: str) -> str | None:
        for root, provider in SDK_ROOTS.items():
            if qualified == root or qualified.startswith(root + "."):
                return provider
        return None

    @property
    def imported_sdks(self) -> set[str]:
        return {p for full in self.imports.values() if (p := self.sdk_of(full))}

    def _constructor_provider(self, call: ast.Call) -> str | None:
        name = dotted(call.func)
        if not name:
            return None
        q = self.qualify(name)
        provider = self.sdk_of(q)
        if provider and q.rsplit(".", 1)[-1] in CLIENT_CONSTRUCTORS.get(provider, set()):
            return provider
        return None

    # ------------------------------------------------------------------ call sites

    def call_sites(self) -> list[CallSite]:
        sdks = self.imported_sdks
        if not sdks:
            return []
        sites = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                site = self._match(node, sdks)
                if site:
                    sites.append(site)
        return sorted(sites, key=lambda s: s.line)

    def _match(self, call: ast.Call, sdks: set[str]) -> CallSite | None:
        name = dotted(call.func)
        if not name:
            return None
        parts = [p for p in name.split(".") if p != "beta"]

        # LiteLLM: litellm.completion(...) or `from litellm import completion`
        q = self.qualify(name)
        if self.sdk_of(q) == "litellm" and q.rsplit(".", 1)[-1] in LITELLM_FUNCS:
            return self._build(call, "litellm", q.rsplit(".", 1)[-1], batch=q.endswith("batch_completion"))

        for suffix, default_provider, is_batch in API_PATTERNS:
            n = len(suffix)
            if len(parts) > n and tuple(parts[-n:]) == suffix:
                receiver = ".".join(parts[:-n])
                provider = self._receiver_provider(receiver) or (
                    default_provider if default_provider in sdks else None
                )
                if provider is None:
                    return None
                return self._build(call, provider, ".".join(suffix), batch=is_batch)
        return None

    def _receiver_provider(self, receiver: str) -> str | None:
        if receiver in self.clients:
            return self.clients[receiver]
        if receiver.endswith("()"):
            return self.sdk_of(self.qualify(receiver[:-2]))
        return None

    def _build(self, call: ast.Call, provider: str, api: str, batch: bool) -> CallSite:
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        fn = self._enclosing(call, (ast.FunctionDef, ast.AsyncFunctionDef))
        for_loop = self._enclosing(call, (ast.For, ast.AsyncFor), stop_at_function=True)
        while_true = self._enclosing_while_true(call)
        retry = self._retry_continue(call, while_true)

        config = kw.get("config")
        config_kw = {k.arg: k.value for k in config.keywords if k.arg} if isinstance(config, ast.Call) else {}

        max_tokens = None
        for key in MAX_TOKEN_KWARGS:
            node = kw.get(key) or config_kw.get(key)
            if node is not None:
                max_tokens = self._int(node)
                break

        system_expr = (kw.get("system") or kw.get("instructions") or config_kw.get("system_instruction")
                       or self._system_from_messages(kw.get("messages") or kw.get("input")))
        system = self.resolve_text(system_expr) if system_expr is not None else Resolved("absent")
        tools = self.resolve_tools(kw["tools"]) if "tools" in kw else Resolved("absent")

        has_cache = any(k.arg == "cache_control" for k in call.keywords) or _has_key(call, "cache_control")
        if system_expr is not None and _has_key(system_expr, "cache_control"):
            has_cache = True

        stream_kw = kw.get("stream")
        streaming = "stream" in api or (isinstance(stream_kw, ast.Constant) and stream_kw.value is True)

        context = " ".join(
            filter(None, [self.rel, fn.name if fn else "", _docstring(self.tree), _docstring(fn) if fn else ""])
        ).lower()

        return CallSite(
            id=f"{self.rel}:{call.lineno}",
            file=self.rel,
            line=call.lineno,
            provider=provider,
            api=api,
            function=fn.name if fn else None,
            model=self.resolve_text(kw["model"]) if "model" in kw else Resolved("absent"),
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            prompt_text=" ".join(_string_parts(call)).lower(),
            has_cache_control=has_cache,
            streaming=streaming,
            batch_api=batch,
            in_for_loop=for_loop.lineno if for_loop else None,
            in_while_true=while_true.lineno if while_true else None,
            retry_continue=retry,
            loop_has_counter=_loop_has_counter(while_true) if while_true else False,
            is_test_file=is_test_path(self.rel),
            context_text=context,
            kw_lines={k.arg: k.value.lineno for k in call.keywords if k.arg},
        )

    # ------------------------------------------------------------------ context helpers

    def _enclosing(self, node: ast.AST, kinds: tuple, stop_at_function: bool = False):
        cur = self.parents.get(node)
        while cur is not None:
            if isinstance(cur, kinds):
                return cur
            if stop_at_function and isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                return None
            cur = self.parents.get(cur)
        return None

    def _enclosing_while_true(self, node: ast.AST) -> ast.While | None:
        cur = self.parents.get(node)
        while cur is not None and not isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
            if isinstance(cur, ast.While) and isinstance(cur.test, ast.Constant) and cur.test.value in (True, 1):
                return cur
            cur = self.parents.get(cur)
        return None

    def _retry_continue(self, node: ast.AST, loop: ast.While | None) -> int | None:
        """Line of an except-handler that `continue`s, when the call sits in the try body inside `loop`."""
        if loop is None:
            return None
        child, cur = node, self.parents.get(node)
        while cur is not None and cur is not loop:
            if isinstance(cur, ast.Try) and child in cur.body:
                for h in cur.handlers:
                    swallow = next((s for s in h.body if isinstance(s, ast.Continue | ast.Pass)), None)
                    if swallow and not any(isinstance(s, ast.Raise | ast.Break | ast.Return) for s in ast.walk(h)):
                        return swallow.lineno
            child, cur = cur, self.parents.get(cur)
        return None

    def _system_from_messages(self, messages: ast.AST | None) -> ast.expr | None:
        if not isinstance(messages, ast.List):
            return None
        for elt in messages.elts:
            if isinstance(elt, ast.Dict):
                d = {k.value: v for k, v in zip(elt.keys, elt.values, strict=False)
                     if isinstance(k, ast.Constant)}
                role = d.get("role")
                if isinstance(role, ast.Constant) and role.value in ("system", "developer"):
                    return d.get("content")
        return None

    def _int(self, node: ast.AST) -> int | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        if isinstance(node, ast.Name) and node.id in self.constants:
            return self._int(self.constants[node.id])
        return None

    # ------------------------------------------------------------------ value resolution

    def resolve_text(self, expr: ast.AST, depth: int = 0) -> Resolved:
        if depth > 6:
            return Resolved("dynamic")
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return Resolved("literal", expr.value, _tokens(expr.value))
        if isinstance(expr, ast.Name) and expr.id in self.constants:
            r = self.resolve_text(self.constants[expr.id], depth + 1)
            return r if r.kind != "literal" else Resolved("literal", r.value, r.tokens)
        if isinstance(expr, ast.JoinedStr):
            return self._resolve_fstring(expr, depth)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            left, right = self.resolve_text(expr.left, depth + 1), self.resolve_text(expr.right, depth + 1)
            tokens = (left.tokens or 0) + (right.tokens or 0) or None
            vol = left.volatile + right.volatile
            return Resolved("fstring", None, tokens, vol,
                            left.volatile_line or right.volatile_line,
                            leading_volatile=bool(left.volatile) and (right.tokens or 0) > (left.tokens or 0))
        if isinstance(expr, ast.List):  # list of content blocks
            parts = [self.resolve_text(e, depth + 1) for e in expr.elts]
            tokens = sum(p.tokens or 0 for p in parts) or None
            vol = [v for p in parts for v in p.volatile]
            return Resolved("literal" if not vol else "fstring", None, tokens, vol)
        if isinstance(expr, ast.Dict):
            d = {k.value: v for k, v in zip(expr.keys, expr.values, strict=False) if isinstance(k, ast.Constant)}
            if "text" in d:
                return self.resolve_text(d["text"], depth + 1)
            if "content" in d:
                return self.resolve_text(d["content"], depth + 1)
        if isinstance(expr, ast.Name) and (target := self.external(expr.id)):
            mod, attr = target
            if attr in mod.constants:
                return mod.resolve_text(mod.constants[attr], depth + 1)
        if isinstance(expr, ast.Call):
            if isinstance(expr.func, ast.Attribute) and expr.func.attr in ("read_text", "read"):
                return self._resolve_file(expr)
            fname = dotted(expr.func) or ""
            owner, local = self, fname
            if fname not in self.functions and (target := self.external(fname)):
                owner, local = target
            if local in owner.functions:
                ret = _first_return(owner.functions[local])
                if ret is not None:
                    r = owner.resolve_text(ret, depth + 1)
                    return Resolved("call", fname, r.tokens, r.volatile, r.volatile_line, r.leading_volatile,
                                    source_file=owner.rel if owner is not self else r.source_file)
                return Resolved("call", fname)
        vol = _volatile_calls(expr, self)
        if vol:
            return Resolved("dynamic", None, None, vol, getattr(expr, "lineno", None))
        return Resolved("dynamic")

    def _resolve_fstring(self, expr: ast.JoinedStr, depth: int) -> Resolved:
        tokens = 0
        volatile: list[str] = []
        vol_line = None
        static_before_volatile = 0
        static_after_volatile = 0
        for part in expr.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                t = _tokens(part.value)
                tokens += t
                if volatile:
                    static_after_volatile += t
                else:
                    static_before_volatile += t
                continue
            if isinstance(part, ast.FormattedValue):
                vol = _volatile_calls(part.value, self)
                if vol:
                    volatile += vol
                    vol_line = vol_line or part.value.lineno
                    continue
                r = self.resolve_text(part.value, depth + 1)
                t = r.tokens or 0
                tokens += t
                if r.volatile:
                    volatile += r.volatile
                    vol_line = vol_line or r.volatile_line
                if volatile:
                    static_after_volatile += t
                else:
                    static_before_volatile += t
        leading = bool(volatile) and static_after_volatile > static_before_volatile
        return Resolved("fstring", None, tokens or None, volatile, vol_line, leading)

    def _resolve_file(self, expr: ast.Call) -> Resolved:
        # Only the path expression (the receiver of .read_text()/.read()), never kwargs like encoding="utf-8".
        literals = [seg for s in _string_parts(expr.func) for seg in s.split("/") if seg not in ("", ".", "..")]
        for k in range(len(literals), 0, -1):
            tail = "/".join(literals[-k:])
            matches = sorted(f for f in self.repo_files if f == tail or f.endswith("/" + tail))
            if matches:
                path = matches[0]
                return Resolved("file", path, self._file_tokens(path))
        return Resolved("file", None)

    def _file_tokens(self, rel: str) -> int | None:
        root = self.path
        for _ in Path(self.rel).parts:
            root = root.parent
        try:
            return _tokens((root / rel).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return None

    def external(self, name: str) -> tuple[ModuleAnalyzer, str] | None:
        """Resolve a name imported from another analysed module of the same project."""
        if not self.project or name not in self.imports:
            return None
        return self.project.lookup(self.imports[name])

    def resolve_tools(self, expr: ast.AST) -> Resolved:
        owner: ModuleAnalyzer = self
        node: ast.AST = expr
        name = None
        if isinstance(expr, ast.Call):
            name = dotted(expr.func) or ""
            local = name
            if name not in self.functions and (target := self.external(name)):
                owner, local = target
            if local in owner.functions:
                node = owner.functions[local]
        elif isinstance(expr, ast.Name):
            name = expr.id
            if expr.id in self.constants:
                node = self.constants[expr.id]
            elif (target := self.external(expr.id)) and target[1] in target[0].constants:
                owner, node = target[0], target[0].constants[target[1]]
        nondet = owner._set_iteration(node)
        kind = "call" if isinstance(expr, ast.Call) else ("literal" if isinstance(expr, ast.List) else "constant")
        return Resolved(kind, name, None, nondeterministic_line=nondet,
                        source_file=owner.rel if owner is not self else None)

    def _set_iteration(self, node: ast.AST) -> int | None:
        """Line where tools are built by iterating a set (order differs between processes)."""
        for sub in ast.walk(node):
            iters = []
            if isinstance(sub, ast.comprehension):
                iters.append((sub.iter, getattr(sub, "lineno", None) or sub.iter.lineno))
            elif isinstance(sub, ast.For):
                iters.append((sub.iter, sub.lineno))
            for it, line in iters:
                if self._is_set(it):
                    return line
            if isinstance(sub, ast.Call) and dotted(sub.func) in ("list", "tuple") and sub.args \
                    and self._is_set(sub.args[0]):
                return sub.lineno
        return None

    def _is_set(self, node: ast.AST, depth: int = 0) -> bool:
        if depth > 4:
            return False
        if isinstance(node, ast.Set | ast.SetComp):
            return True
        if isinstance(node, ast.Call) and dotted(node.func) in ("set", "frozenset"):
            return True
        if isinstance(node, ast.Name) and node.id in self.constants:
            return self._is_set(self.constants[node.id], depth + 1)
        return False


# ---------------------------------------------------------------------- module-level helpers


def _tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN)) if text else 0


def _string_parts(node: ast.AST) -> list[str]:
    """String literals under `node` in source order (ast.walk is breadth-first, so sort by position)."""
    found = [
        (getattr(sub, "lineno", 0), getattr(sub, "col_offset", 0), sub.value)
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    ]
    return [s for _, _, s in sorted(found, key=lambda t: (t[0], t[1]))]


def _has_key(node: ast.AST, key: str) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == key for k in sub.keys):
            return True
        if isinstance(sub, ast.keyword) and sub.arg == key:
            return True
    return False


def _first_return(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.expr | None:
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Return) and sub.value is not None:
            return sub.value
    return None


def _volatile_calls(node: ast.AST, mod: ModuleAnalyzer) -> list[str]:
    found = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = dotted(sub.func)
            if not name:
                continue
            q = mod.qualify(name).replace("()", "")
            for v in VOLATILE_CALLS:
                if q == v or q.endswith("." + v):
                    found.append(v)
                    break
    return found


def _loop_has_counter(loop: ast.While) -> bool:
    incremented = {
        sub.target.id for sub in ast.walk(loop)
        if isinstance(sub, ast.AugAssign) and isinstance(sub.target, ast.Name)
    }
    if not incremented:
        return False
    for sub in ast.walk(loop):
        if isinstance(sub, ast.Compare):
            names = {n.id for n in ast.walk(sub) if isinstance(n, ast.Name)}
            if names & incremented:
                return True
    return False
