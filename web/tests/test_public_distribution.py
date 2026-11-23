from pathlib import Path
import secrets

import pytest
from fastapi.testclient import TestClient

from backend.main import app


def test_local_web_boundaries(tmp_path, monkeypatch):
    key = secrets.token_urlsafe(32)
    monkeypatch.setenv("DABW_DEMO_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("DABW_TOKENS_YAML", str(tmp_path / "tokens.yaml"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="AUTH_TOKEN"):
        with TestClient(app):
            pass

    monkeypatch.setenv("AUTH_TOKEN", key)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/health").json() == {"ok": True}
        assert client.get("/api/tasks").status_code == 401
        assert client.post("/api/auth/check", json={"token": "invalid"}).status_code == 401
        assert client.get("/").status_code == 200
        for path in ("/tokens.yaml", "/config.toml", "/.env", "/backend/main.py", "/runs_local/state.db"):
            assert client.get(path).status_code == 404
        client.headers["Authorization"] = f"Bearer {key}"
        tasks = client.get("/api/tasks", params={"limit": 1000}).json()
        assert len(tasks) == 275
        assert client.get("/api/tasks/M1_T1_Si_vc_relax/agent_view").is_success
        settings = client.app.state.settings
        assert {a.id for a in settings.agents} == {"v1-stable", "v5.1"}
        assert all(Path(a.solver_path).is_file() for a in settings.agents)


def test_token_registry_is_owner_only(tmp_path):
    from backend.api.tokens import _atomic_write_yaml

    path = tmp_path / "tokens.yaml"
    _atomic_write_yaml(path, [])
    assert path.stat().st_mode & 0o777 == 0o600
