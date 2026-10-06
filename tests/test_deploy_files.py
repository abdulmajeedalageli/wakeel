"""Safety checks on the deployment files (plain text checks; Docker isn't needed to run them)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_runs_one_worker_as_non_root_without_access_log():
    dockerfile = read("Dockerfile")
    assert "FROM python:3.12-slim" in dockerfile
    assert re.search(r"^USER bot$", dockerfile, re.MULTILINE)
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
    assert "--workers 1 " in cmd
    assert "--no-access-log" in cmd
    assert "--port ${PORT:-8000}" in cmd  # Railway sets PORT; docker compose uses 8000
    assert "exec uvicorn" in cmd  # uvicorn gets stop signals directly
    assert "HEALTHCHECK" in dockerfile and "/health" in dockerfile


def test_dockerfile_never_copies_secrets_tests_or_everything():
    copies = [line for line in read("Dockerfile").splitlines() if line.startswith("COPY")]
    assert not any(line.startswith("COPY . ") for line in copies)
    for forbidden in (".env", "secrets", "data", "tests", "test_data"):
        assert not any(re.search(rf"\b{re.escape(forbidden)}\b", line) for line in copies), forbidden
    assert "requirements-dev.txt" not in read("Dockerfile")


def test_dockerignore_excludes_secrets_and_data():
    ignored = read(".dockerignore").splitlines()
    for entry in (".env", "secrets/", "data/", "*.db", ".git/", "tests/"):
        assert entry in ignored, entry


def test_compose_restarts_and_mounts_secrets_read_only():
    compose = read("docker-compose.yml")
    assert compose.count("restart: always") == 2
    assert "./secrets:/app/secrets:ro" in compose
    assert "./data:/app/data" in compose
    assert "DB_PATH: /app/data/bot.db" in compose
    # The bot itself is never published to the internet; only Caddy has ports.
    app_block = compose.split("  caddy:")[0]
    assert "ports:" not in app_block
    assert '"80:80"' in compose and '"443:443"' in compose


def test_caddy_only_forwards_webhook_and_health():
    caddyfile = read("deploy/Caddyfile")
    assert "@bot path /webhook /health /privacy" in caddyfile
    assert caddyfile.count("reverse_proxy") == 1
    assert "handle @bot {\n\t\treverse_proxy app:8000" in caddyfile
    assert 'respond "Not found" 404' in caddyfile
    assert "max_size 1MB" in caddyfile


def test_railway_config_restarts_and_checks_health_with_one_copy():
    import json

    config = json.loads(read("railway.json"))
    assert config["build"]["builder"] == "DOCKERFILE"
    assert config["deploy"]["healthcheckPath"] == "/health"
    assert config["deploy"]["restartPolicyType"] == "ALWAYS"
    # One copy only (Telegram polling, in-memory locks): no replica settings above 1.
    for region in config["deploy"].get("multiRegionConfig", {}).values():
        assert region.get("numReplicas", 1) <= 1
