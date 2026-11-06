"""Generate a week of SYNTHETIC usage logs for the Acme demo app (deterministic, seeded).

    python examples/demo_app/logs/generate_logs.py            # -> examples/demo_app/logs/usage.jsonl
    harnessdoc measure examples/demo_app/logs/usage.jsonl --scan examples/demo_app

The numbers are made up but shaped by the demo code: every agent turn re-sends the ~966-token policy and
~420 tokens of tools uncached (the timestamp busts the cache), the router runs on an Opus-tier model, the
extractor calls an LLM for a regex's job, and the nightly job summarises each closed ticket with a premium
model through the real-time API. Records are raw provider responses wrapped with task id, site and time —
the same shape `harnessdoc.log.UsageLogger` or a simple JSONL dump of responses would give you.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

POLICY_TOKENS = 966
TOOLS_TOKENS = 420
START = datetime(2026, 10, 30, tzinfo=timezone.utc)


def anthropic(model: str, inp: int, out: int, rng: random.Random) -> dict:
    return {"id": f"msg_{rng.getrandbits(48):012x}", "type": "message", "model": model,
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}


def openai_chat(model: str, prompt: int, completion: int) -> dict:
    return {"object": "chat.completion", "model": model,
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "prompt_tokens_details": {"cached_tokens": 0}}}


def generate(days: int = 7, per_day: int = 70, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    lines: list[dict] = []
    for d in range(days):
        day = START + timedelta(days=d)
        closed = []
        for i in range(per_day + rng.randint(-8, 8)):
            task = f"T{d:02d}{i:03d}"
            t = day + timedelta(hours=8, seconds=rng.randint(0, 12 * 3600))

            def rec(site: str, response: dict, when: datetime, task: str = task) -> None:
                lines.append({"ts": when.isoformat(), "task_id": task, "site": site, "response": response})

            rec("classify_intent", anthropic("claude-opus-5-5", rng.randint(60, 140), rng.randint(2, 5), rng), t)
            rec("extract_order_id", openai_chat("gpt-6.1-sol", rng.randint(70, 160), rng.randint(5, 9)),
                t + timedelta(seconds=1))

            turns = min(8, 1 + int(rng.expovariate(0.45)))
            history = rng.randint(40, 120)
            for k in range(turns):
                inp = POLICY_TOKENS + TOOLS_TOKENS + 15 + history
                out = rng.randint(60, 260)
                rec("respond", anthropic("claude-opus-5-5", inp, out, rng), t + timedelta(seconds=3 + 9 * k))
                history += out + rng.randint(30, 220)  # assistant turn + tool results / next user message

            p_success = 0.88 - 0.05 * max(0, turns - 3)
            success = rng.random() < p_success
            lines.append({"type": "outcome", "task_id": task, "success": success,
                          "ts": (t + timedelta(minutes=5)).isoformat()})
            closed.append(rng.randint(700, 2600))

        night = day + timedelta(days=1, hours=2)
        for j, transcript_tokens in enumerate(closed):
            lines.append({"ts": (night + timedelta(seconds=4 * j)).isoformat(), "site": "summarise_closed_tickets",
                          "response": openai_chat("gpt-6-astra", transcript_tokens + 30, rng.randint(40, 75))})
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "usage.jsonl")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--per-day", type=int, default=70)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    lines = generate(args.days, args.per_day, args.seed)
    args.out.write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in lines), encoding="utf-8")
    print(f"wrote {len(lines)} lines → {args.out}")


if __name__ == "__main__":
    main()
