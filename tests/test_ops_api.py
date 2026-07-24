from fastapi.testclient import TestClient

import app as app_module
from api.routes.ops import build_account_health


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
    assert "image_queue" in payload["capabilities"]
    assert "requests" in payload
    assert "successful" in payload["requests"]
    assert "today" in payload["requests"]
    assert set(payload["requests"]["today"]) >= {"total", "successful", "failed"}
    assert "tokens" in payload
    assert "accounts" in payload["capabilities"]
    assert "accounts" in payload
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


class _FakeTokenManager:
    def list_all(self):
        return [
            {
                "id": "token-low",
                "value": "MASKED",
                "status": "active",
                "auto_refresh": True,
                "refresh_profile_id": "profile-low",
                "credits_available": 99,
                "credits_total": 1000,
                "expires_at": 12345,
            },
            {
                "id": "token-boundary",
                "value": "MASKED",
                "status": "active",
                "auto_refresh": True,
                "refresh_profile_id": "profile-boundary",
                "credits_available": 100,
                "credits_total": 1000,
            },
            {
                "id": "manual-token",
                "value": "MASKED",
                "status": "active",
                "auto_refresh": False,
                "credits_available": 1,
            },
        ]


class _FakeRefreshManager:
    def list_profiles(self):
        return [
            {
                "id": "profile-boundary",
                "name": "Boundary",
                "enabled": True,
                "account": {"email": "boundary@example.com", "user_id": "user-2"},
                "state": {"consecutive_failures": 0},
            },
            {
                "id": "profile-unknown",
                "name": "Unknown",
                "enabled": True,
                "account": {"email": "unknown@example.com", "user_id": "user-3"},
                "state": {"consecutive_failures": 0},
            },
            {
                "id": "profile-low",
                "name": "Low",
                "enabled": True,
                "account": {"email": "low@example.com", "user_id": "user-1"},
                "state": {
                    "consecutive_failures": 2,
                    "last_error": "Cookie: SECRET_COOKIE access_token=SECRET_TOKEN",
                },
            },
        ]


def test_account_health_is_profile_based_sorted_and_redacted():
    payload = build_account_health(_FakeTokenManager(), _FakeRefreshManager(), 100)

    assert [item["id"] for item in payload["items"]] == [
        "profile-low",
        "profile-boundary",
        "profile-unknown",
    ]
    assert payload["summary"]["total"] == 3
    assert payload["summary"]["low_credit"] == 1
    assert payload["summary"]["balance_unknown"] == 1
    assert payload["items"][0]["health"] == "refresh_failed"
    assert payload["items"][0]["low_credit"] is True
    assert payload["items"][1]["low_credit"] is False
    assert payload["items"][2]["health"] == "balance_unknown"
    assert all("value" not in item and "cookie" not in item for item in payload["items"])
    assert "SECRET_COOKIE" not in str(payload)
    assert "SECRET_TOKEN" not in str(payload)


def test_refresh_profile_batch_management_accepts_ops_key(monkeypatch):
    monkeypatch.setenv("ADOBE2API_OPS_KEY", "OPS_TEST_KEY")
    deleted = []
    enabled = []

    def fake_remove(profile_id):
        if profile_id == "missing":
            raise KeyError("profile not found")
        deleted.append(profile_id)

    def fake_enabled(profile_id, value):
        if profile_id == "missing":
            raise KeyError("profile not found")
        enabled.append((profile_id, value))
        return {"id": profile_id, "enabled": value}

    monkeypatch.setattr(app_module.refresh_manager, "remove_profile", fake_remove)
    monkeypatch.setattr(app_module.refresh_manager, "set_enabled", fake_enabled)
    client = TestClient(app_module.app)
    headers = {"X-Adobe2API-Ops-Key": "OPS_TEST_KEY"}

    disabled = client.put(
        "/api/v1/refresh-profiles/enabled-batch",
        headers=headers,
        json={"ids": ["profile-a", "profile-b"], "enabled": False},
    )
    removed = client.post(
        "/api/v1/refresh-profiles/delete-batch",
        headers=headers,
        json={"ids": ["profile-a", "missing"]},
    )

    assert disabled.status_code == 200
    assert disabled.json()["updated_count"] == 2
    assert enabled == [("profile-a", False), ("profile-b", False)]
    assert removed.status_code == 200
    assert removed.json()["status"] == "partial"
    assert deleted == ["profile-a"]
