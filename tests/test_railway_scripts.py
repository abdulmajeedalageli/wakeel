"""scripts/env_for_railway.py and scripts/smoke_test.py."""

import json
from pathlib import Path

import httpx

import scripts.env_for_railway as env_script
from scripts.smoke_test import run_checks

ROOT = Path(__file__).resolve().parent.parent

SAMPLE_ENV = """# comment line
WHATSAPP_ACCESS_TOKEN=abc123
WHATSAPP_API_VERSION=v26.0      # Latest Graph API version
TELEGRAM_BOT_TOKEN=          # from @BotFather
BUSINESS_NAME="frame"
WORK_HOURS=09:00-17:00
DB_PATH=data/bot.db             # relative
GOOGLE_SERVICE_ACCOUNT_FILE=secrets/service-account.json
DOMAIN=frame.duckdns.org
URL_WITH_HASH=https://example.com/a#b
"""

KEY = {"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----\n"}


# --- env_for_railway ---

def test_parse_env_strips_comments_and_quotes():
    env = env_script.parse_env(SAMPLE_ENV)
    assert env["WHATSAPP_ACCESS_TOKEN"] == "abc123"
    assert env["WHATSAPP_API_VERSION"] == "v26.0"
    assert env["TELEGRAM_BOT_TOKEN"] == ""
    assert env["BUSINESS_NAME"] == "frame"
    assert env["WORK_HOURS"] == "09:00-17:00"
    assert env["URL_WITH_HASH"] == "https://example.com/a#b"  # '#' without a space isn't a comment


def test_build_variables_for_railway():
    variables = env_script.build_variables(env_script.parse_env(SAMPLE_ENV), json.dumps(KEY, indent=2))
    assert "TELEGRAM_BOT_TOKEN" not in variables  # empty values are dropped
    assert "GOOGLE_SERVICE_ACCOUNT_FILE" not in variables and "DOMAIN" not in variables
    assert variables["DB_PATH"] == "/app/data/bot.db"
    assert variables["RAILWAY_RUN_UID"] == "0"
    one_line = variables["GOOGLE_SERVICE_ACCOUNT_JSON"]
    assert "\n" not in one_line
    assert json.loads(one_line) == KEY  # the private key's line breaks survive as \\n escapes


def test_render_quotes_the_json_line():
    text = env_script.render({"A": "1", "GOOGLE_SERVICE_ACCOUNT_JSON": '{"x":1}'})
    assert text == "A=1\nGOOGLE_SERVICE_ACCOUNT_JSON='{\"x\":1}'\n"


def test_main_writes_the_file_and_prints_only_names(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(SAMPLE_ENV, encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "service-account.json").write_text(json.dumps(KEY), encoding="utf-8")
    monkeypatch.setattr(env_script, "ROOT", tmp_path)

    assert env_script.main() == 0

    written = (tmp_path / "railway-variables.env").read_text(encoding="utf-8")
    assert "WHATSAPP_ACCESS_TOKEN=abc123" in written
    printed = capsys.readouterr().out
    assert "WHATSAPP_ACCESS_TOKEN" in printed
    assert "abc123" not in printed and "PRIVATE KEY" not in printed  # values are never printed


def test_output_file_is_git_ignored():
    assert env_script.OUTPUT_NAME in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


# --- smoke_test ---

HEALTHY = {"status": "ok", "db": "ok", "sheet": {"status": "ok", "properties": 33, "skipped_rows": 2},
           "ai": "configured", "telegram": "configured", "paused": False}


def fake_app(health: dict | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(status, json=health if health is not None else HEALTHY)
        if path == "/webhook":
            return httpx.Response(403)
        if path == "/privacy":
            return httpx.Response(200, text='<h2 id="delete">')
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_smoke_test_all_pass_on_a_healthy_app():
    checks = run_checks("https://bot.example/", fake_app())
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert len(checks) == 11


def test_smoke_test_reports_each_problem():
    broken = dict(HEALTHY, ai="not_configured", paused=True, sheet={"status": "error", "properties": 0})
    failed = {c.name for c in run_checks("https://bot.example", fake_app(broken)) if not c.ok}
    assert failed == {"AI (Gemini) configured", "bot not paused", "property sheet loaded"}


def test_smoke_test_handles_an_unreachable_app():
    def handler(request):
        raise httpx.ConnectError("no route")

    checks = run_checks("https://bot.example", httpx.Client(transport=httpx.MockTransport(handler)))
    assert [(c.name, c.ok) for c in checks] == [("health endpoint answers", False)]
