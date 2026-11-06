"""The support agent loop."""

from datetime import datetime
from pathlib import Path

import anthropic

from .tools import build_tools

client = anthropic.Anthropic()
POLICY = (Path(__file__).resolve().parent.parent / "prompts" / "policy.md").read_text()


def system_prompt() -> str:
    return f"Current time: {datetime.now().isoformat()}\n\n{POLICY}"


def run_tool(name: str, args: dict) -> str:
    return f"(stub result for {name})"


def respond(history: list[dict], user_message: str) -> str:
    history.append({"role": "user", "content": user_message})
    while True:
        try:
            response = client.messages.create(
                model="claude-opus-5-5",
                max_tokens=16000,
                system=system_prompt(),
                tools=build_tools(),
                messages=history,
            )
        except anthropic.APIError:
            continue

        history.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return next((b.text for b in response.content if b.type == "text"), "")

        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": run_tool(b.name, b.input)}
            for b in response.content
            if b.type == "tool_use"
        ]
        history.append({"role": "user", "content": results})
