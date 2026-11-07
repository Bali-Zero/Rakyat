"""Smoke test: Claude on a Console seat via subprocess (claude -p).

Run inside container with:
    fly ssh console -a nuzantara-rag --process-group api \
      -C "su nuzantara -c 'cd /app && python3 scripts/smoke_claude_oauth.py'"

Prints seat labels only — never a key or a workspace id.
"""
import asyncio
import sys
import time


async def main() -> int:
    from backend.llm.claude_oauth_client import (
        ClaudeOAuthError,
        ClaudeOAuthNotAvailable,
        collect_seats,
        complete_async,
    )

    seats = collect_seats()
    print(f"[enum] seats: {[seat.label for seat in seats]}")

    t0 = time.monotonic()
    try:
        resp = await complete_async(
            "Reply with exactly: SMOKE_OK",
            model="claude-haiku-4-5",
            timeout_s=30,
        )
        print(f"[live] text={resp.text[:120]!r}")
        print(f"[live] seat={resp.token_label} attempts={resp.attempts}")
        api = f"{resp.api_s:.1f}s" if resp.api_s is not None else "?"
        print(f"[live] elapsed={resp.elapsed_s:.1f}s (wrapper), "
              f"{time.monotonic()-t0:.1f}s (wall), api={api} — the rest is the CLI's own time")
        return 0
    except ClaudeOAuthNotAvailable as exc:
        print(f"[fail] no door (CLI missing or no seat): {exc}", file=sys.stderr)
        return 10
    except ClaudeOAuthError as exc:
        print(f"[fail] every seat failed: {exc}", file=sys.stderr)
        return 20
    except Exception as exc:
        print(f"[fail] unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 30


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
