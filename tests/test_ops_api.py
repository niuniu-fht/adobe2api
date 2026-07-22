from fastapi.testclient import TestClient

import app as app_module


def test_ops_endpoints_require_configured_key(monkeypatch):
    monkeypatch.setenv("ADOBE2API_OPS_KEY", "OPS_TEST_KEY")
    client = TestClient(app_module.app)

    missing = client.get("/api/v1/ops/snapshot")
    invalid = client.get(
        "/api/v1/ops/snapshot",
        headers={"X-Adobe2API-Ops-Key": "WRONG"},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401


def test_ops_snapshot_and_admin_config_are_redacted(monkeypatch):
    monkeypatch.setenv("ADOBE2API_OPS_KEY", "OPS_TEST_KEY")
    client = TestClient(app_module.app)
    headers = {"X-Adobe2API-Ops-Key": "OPS_TEST_KEY"}

    snapshot = client.get("/api/v1/ops/snapshot", headers=headers)
    config = client.get("/api/v1/config", headers=headers)

    assert snapshot.status_code == 200
    payload = snapshot.json()
    assert payload["ops_api_version"] == 1
    assert "cursor_logs" in payload["capabilities"]
    assert "requests" in payload
    assert "successful" in payload["requests"]
    assert "today" in payload["requests"]
    assert set(payload["requests"]["today"]) >= {"total", "successful", "failed"}
    assert "tokens" in payload
    assert config.status_code == 200
    config_payload = config.json()
    assert "api_key" not in config_payload
    assert "admin_password" not in config_payload
    assert "admin_session_secret" not in config_payload
    assert "api_key_configured" in config_payload


def test_ops_cursor_logs_return_versioned_response(monkeypatch):
    monkeypatch.setenv("ADOBE2API_OPS_KEY", "OPS_TEST_KEY")
    client = TestClient(app_module.app)

    response = client.get(
        "/api/v1/ops/logs?limit=5",
        headers={"X-Adobe2API-Ops-Key": "OPS_TEST_KEY"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ops_api_version"] == 1
    assert isinstance(payload["items"], list)
