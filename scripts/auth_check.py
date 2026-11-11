"""
Verify the Claude Agent SDK reuses the Claude Code Max-plan OAuth token rather
than billing the API. The SDK auto-bills if ANTHROPIC_API_KEY is set in the
environment; we explicitly remove it here, then send a one-shot turn.

Run after `claude /login` has been completed once for the Max account.

Exit codes:
  0  auth ok
  1  SDK error (auth, network, etc.)
  2  ANTHROPIC_API_KEY was set and we refused to start
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback


def _scrub_env() -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is set in this process — refusing to run because "
            "it would silently bill the API instead of using your Max-plan auth.\n"
            "Unset it, e.g.  Remove-Item Env:ANTHROPIC_API_KEY  in PowerShell, "
            "and re-run.",
            file=sys.stderr,
        )
        sys.exit(2)


async def _main() -> int:
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            SystemMessage,
            TextBlock,
        )
    except ImportError as e:
        print(f"claude-agent-sdk import failed: {e}", file=sys.stderr)
        return 1

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="claude-opus-4-8",
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "Reply with exactly the two words: auth ok",
        },
    )

    print(f"claude-agent-sdk import: OK")
    print(f"sending one-shot turn (model=claude-opus-4-8) ...")

    final_text = ""
    session_id = ""

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query("Reply with exactly: auth ok")
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage) and getattr(msg, "subtype", "") == "init":
                    data = getattr(msg, "data", {}) or {}
                    session_id = data.get("session_id", "") or session_id
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            final_text += block.text
                if isinstance(msg, ResultMessage):
                    break
    except Exception:
        print("SDK call raised:", file=sys.stderr)
        traceback.print_exc()
        return 1

    print(f"session_id: {session_id or '<not reported>'}")
    print(f"response:   {final_text.strip()!r}")
    if "auth ok" in final_text.lower():
        print("RESULT: auth ok")
        print(
            "\nVerify with `claude /status` in another shell — the call should "
            "have counted against Max usage, not API."
        )
        return 0
    print("RESULT: response did not include 'auth ok' — investigate.", file=sys.stderr)
    return 1


def main() -> None:
    _scrub_env()
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
