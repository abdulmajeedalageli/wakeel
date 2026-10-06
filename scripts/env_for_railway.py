"""Make the variables file to paste into Railway (Variables → Raw Editor).

Reads your .env and secrets/service-account.json and writes railway-variables.env:
- comments and empty values removed (otherwise Railway would keep "# comments" inside values)
- DB_PATH=/app/data/bot.db (the Railway volume) and RAILWAY_RUN_UID=0 added
- the Google key added as GOOGLE_SERVICE_ACCOUNT_JSON, on one line

The output contains ALL your secrets. It's git-ignored; delete it after pasting.

Usage (from the project folder):
    python -m scripts.env_for_railway
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_NAME = "railway-variables.env"

# Set for Railway, whatever .env says.
RAILWAY_OVERRIDES = {"DB_PATH": "/app/data/bot.db", "RAILWAY_RUN_UID": "0"}
# Not used on Railway (no key file, no Caddy/DuckDNS domain).
SKIPPED = {"GOOGLE_SERVICE_ACCOUNT_FILE", "DOMAIN"}


def parse_env(text: str) -> dict[str, str]:
    """Read KEY=value lines like python-dotenv/docker do: '#' comments, quotes, inline ' # comments'."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"') and value[1:].find(value[0]) >= 0:
            value = value[1:1 + value[1:].find(value[0])]  # quoted: take what's inside
        else:
            if value.startswith("#"):
                value = ""  # "KEY=   # comment" means empty
            for marker in (" #", "\t#"):
                value = value.split(marker, 1)[0]
            value = value.strip()
        values[key] = value
    return values


def build_variables(env: dict[str, str], service_account_json: str | None) -> dict[str, str]:
    variables = {k: v for k, v in env.items() if v and k not in SKIPPED}
    variables.update(RAILWAY_OVERRIDES)
    if service_account_json:
        # Compact, one line. The private key's line breaks stay as "\n" escapes inside the JSON.
        variables["GOOGLE_SERVICE_ACCOUNT_JSON"] = json.dumps(json.loads(service_account_json), separators=(",", ":"))
    return variables


def render(variables: dict[str, str]) -> str:
    lines = []
    for key, value in variables.items():
        if key == "GOOGLE_SERVICE_ACCOUNT_JSON":
            lines.append(f"{key}='{value}'")  # single quotes: kept literally
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles (e.g. Arabic cp1256) can't print "→"
    env_file = ROOT / ".env"
    key_file = ROOT / "secrets" / "service-account.json"
    if not env_file.exists():
        print("No .env found in the project folder.")
        return 1
    key_json = key_file.read_text(encoding="utf-8") if key_file.exists() else None
    if key_json is None:
        print("Note: secrets/service-account.json not found, so the Google Sheet won't load on Railway.")

    variables = build_variables(parse_env(env_file.read_text(encoding="utf-8")), key_json)
    output = ROOT / OUTPUT_NAME
    output.write_text(render(variables), encoding="utf-8")
    # Show only the variable NAMES, never the values.
    print(f"Wrote {output.name} with {len(variables)} variables:")
    print("  " + ", ".join(variables))
    print("Paste its content into Railway → your service → Variables → Raw Editor, then delete the file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
