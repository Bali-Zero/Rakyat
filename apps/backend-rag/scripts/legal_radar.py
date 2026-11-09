#!/usr/bin/env python3
"""Legal Radar — Weekly scan for new Indonesian regulations.

Dispatches to Gemini Search via ai-dispatch.sh per domain and
sends a Telegram summary to Zero for review.

Called by OpenClaw cron: Sunday 08:00 WITA
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

# Standalone scripts also resolve the shared, non-reenableable retirement policy.
import sys as _policy_sys
import urllib.request
from datetime import date
from pathlib import Path
from pathlib import Path as _PolicyPath

from dotenv import load_dotenv

_policy_root = str(_PolicyPath(__file__).resolve().parents[1])
if _policy_root not in _policy_sys.path:
    _policy_sys.path.insert(0, _policy_root)
from backend.core.telegram_retirement import telegram_is_retired

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

logger = logging.getLogger(__name__)

DOMAINS = ["immigration", "company", "tax", "property"]
SEARCH_QUERIES: dict[str, str] = {
    "immigration": "peraturan imigrasi Indonesia terbaru 2025 2026 Permenkumham Permen Imigrasi",
    "company": "peraturan OSS BKPM PT PMA NIB terbaru 2025 2026",
    "tax": "peraturan pajak DJP coretax BPJS terbaru 2025 2026",
    "property": "peraturan tanah properti HGB hak pakai terbaru 2025 2026",
}
# Derive root from script location — works on both Pro and Air regardless of home dir layout
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


def dispatch_gemini_search(query: str) -> str:
    """Run ai-dispatch.sh search and return stdout (empty string on failure)."""
    script = os.path.join(PROJECT_ROOT, "scripts", "ai-dispatch.sh")
    try:
        result = subprocess.run(
            [script, "search", query],
            capture_output=True, text=True, timeout=120, cwd=PROJECT_ROOT,
        )
        return result.stdout if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("Gemini search failed for query '%s': %s", query[:50], e)
        return ""


def build_radar_message(domain_results: dict[str, str], scan_date: date) -> str:
    lines = [
        f"📡 *Legal Radar — {scan_date.strftime('%d %b %Y')}*",
        "Weekly scan for new Indonesian regulations",
        "",
    ]
    for domain, result in domain_results.items():
        if result.strip():
            preview = result.strip()[:300].replace("\n", " ")
            lines += [f"*{domain.title()}:*", f"_{preview}_", ""]
        else:
            lines += [f"*{domain.title()}:* No new findings", ""]

    lines.append("Review and update `legal_instruments` table if needed.")
    lines.append("Run `claims_extractor.py` for any new T0 sources.")
    return "\n".join(lines)


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    if telegram_is_retired():
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_ZERO_CHAT_ID", "")
    if not telegram_is_retired() and (not token or not chat_id):
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_ZERO_CHAT_ID must be set")
        sys.exit(1)

    scan_date = date.today()
    print(f"Legal Radar scan — {scan_date}")

    domain_results: dict[str, str] = {}
    for domain in DOMAINS:
        print(f"  Searching: {domain}...")
        domain_results[domain] = dispatch_gemini_search(SEARCH_QUERIES[domain])

    message = build_radar_message(domain_results, scan_date)
    if telegram_is_retired():
        logger.info("Legal Radar scan complete; Telegram is retired")
    else:
        send_telegram_message(token, chat_id, message)
        print("Legal Radar summary sent to Telegram")


if __name__ == "__main__":
    main()
