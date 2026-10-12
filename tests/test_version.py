"""版本展示保持数字版本号，不受构建标识影响。"""

import re

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("build_sha", ["", "dev", "a" * 40])
def test_version_endpoint_uses_numeric_version(monkeypatch, build_sha):
    from backend.main import app
    from backend.services.self_update import current_build, parse_version
    from tg_signer import __version__

    monkeypatch.setenv("BUILD_SHA", build_sha)
    monkeypatch.setenv("BUILD_DATE", "2026-10-12 12:00")
    response = TestClient(app).get("/api/version")

    assert response.status_code == 200
    data = response.json()
    assert data["version"] == __version__ == current_build()["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", data["version"])
    assert parse_version(data["version"]) is not None
    assert data["built_at"] == "2026-10-12 12:00"
