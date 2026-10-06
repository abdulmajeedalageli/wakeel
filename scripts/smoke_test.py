"""Quick public checks of the deployed bot. No secrets needed; nothing is sent to customers.

Usage:
    python -m scripts.smoke_test https://your-app.up.railway.app
"""

import sys
from dataclasses import dataclass

import httpx


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def run_checks(base_url: str, client: httpx.Client) -> list[Check]:
    base = base_url.rstrip("/")
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append(Check(name, bool(ok), detail))

    try:
        resp = client.get(f"{base}/health")
        health = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as exc:
        add("health endpoint answers", False, type(exc).__name__)
        return checks

    add("health endpoint answers (200)", resp.status_code == 200, f"HTTP {resp.status_code}")
    add("status ok", health.get("status") == "ok", str(health.get("status")))
    add("database ok", health.get("db") == "ok", str(health.get("db")))
    sheet = health.get("sheet") or {}
    add("property sheet loaded", sheet.get("status") == "ok" and (sheet.get("properties") or 0) > 0,
        f"{sheet.get('status')}, {sheet.get('properties')} properties, {sheet.get('skipped_rows')} rows skipped")
    add("AI (Gemini) configured", health.get("ai") == "configured", str(health.get("ai")))
    add("Telegram configured", health.get("telegram") == "configured", str(health.get("telegram")))
    add("bot not paused", health.get("paused") is False, str(health.get("paused")))

    add("API docs hidden (/docs is 404)", client.get(f"{base}/docs").status_code == 404)
    privacy = client.get(f"{base}/privacy")
    add("privacy page online (/privacy)", privacy.status_code == 200 and 'id="delete"' in privacy.text,
        f"HTTP {privacy.status_code}")
    wrong_token = client.get(
        f"{base}/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "smoke-test-wrong-token", "hub.challenge": "1"},
    )
    add("wrong verify token refused (403)", wrong_token.status_code == 403, f"HTTP {wrong_token.status_code}")
    unsigned = client.post(f"{base}/webhook", content=b"{}", headers={"Content-Type": "application/json"})
    add("unsigned webhook refused (403)", unsigned.status_code == 403, f"HTTP {unsigned.status_code}")
    return checks


def main(args: list[str]) -> int:
    local = args and args[0].startswith(("http://localhost", "http://127.0.0.1"))
    if len(args) != 1 or not (args[0].startswith("https://") or local):
        print(__doc__)
        return 2
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        checks = run_checks(args[0], client)
    for check in checks:
        print(f"{'PASS' if check.ok else 'FAIL'}  {check.name}" + (f"  ({check.detail})" if check.detail else ""))
    failed = [c for c in checks if not c.ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed.")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
