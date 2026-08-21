from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_systemd_unit_is_dedicated_loopback_and_hardened() -> None:
    unit = _read("westbusan-tourism-ai.service")

    assert "User=westbusan-tourism" in unit
    assert "Group=westbusan-tourism" in unit
    assert "EnvironmentFile=/etc/westbusan-tourism-ai/openai.env" in unit
    assert "--host 127.0.0.1" in unit
    assert "--port 18081" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/var/cache/westbusan-tourism-ai" in unit
    assert "credit-guarantee" not in unit
    assert "minsaeng" not in unit


def test_nginx_snippet_only_proxies_tourism_ai_paths_with_small_bodies() -> None:
    nginx = _read("westbusan-tourism-ai-nginx.conf")

    assert "location = /tourism/api/insights" in nginx
    assert "location = /tourism/api/healthz" in nginx
    assert "client_max_body_size 2k" in nginx
    assert "proxy_pass http://127.0.0.1:18081/insights" in nginx
    assert "proxy_pass http://127.0.0.1:18081/healthz" in nginx
    assert "limit_except POST" in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr" in nginx
    assert "location /" not in nginx


def test_operations_runbook_forbids_secret_and_existing_service_mutation() -> None:
    runbook = (ROOT / "docs" / "TOURISM_AI_OPERATIONS.md").read_text(
        encoding="utf-8"
    )

    assert "키 값을 출력하지 않는다" in runbook
    assert "신용보증 서비스를 재시작하지 않는다" in runbook
    assert "/etc/westbusan-tourism-ai/openai.env" in runbook
    assert "롤백" in runbook
