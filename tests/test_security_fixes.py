"""
安全与遗留问题回归测试：
- 未设置 APP_SECRET_KEY 时生成并持久化随机密钥
- 免验证码重置两步验证的接口已移除，改为离线命令
- 默认不开放跨域
- 默认密码登录时提示修改，且不能改回默认密码
- 通用任务异常时记录结束时间；实时日志 WS 登录失效以 4401 关闭
不需要真实 Telegram 连接。
"""

import contextlib
import stat
from types import SimpleNamespace
from unittest.mock import patch

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ── JWT 密钥 ────────────────────────────────────────────────────────────────


def test_secret_key_is_generated_once_and_private(tmp_path):
    from backend.core.config import SECRET_KEY_FILENAME, load_or_create_secret_key

    first = load_or_create_secret_key(tmp_path)
    second = load_or_create_secret_key(tmp_path)

    path = tmp_path / SECRET_KEY_FILENAME
    assert first == second
    assert len(first) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert {p.name for p in tmp_path.iterdir()} == {
        SECRET_KEY_FILENAME,
        ".secret_key.lock",
    }


def test_secret_key_keeps_existing_file(tmp_path):
    from backend.core.config import SECRET_KEY_FILENAME, load_or_create_secret_key

    (tmp_path / SECRET_KEY_FILENAME).write_text(
        "existing-strong-key-for-test\n", encoding="utf-8"
    )
    assert load_or_create_secret_key(tmp_path) == "existing-strong-key-for-test"


# ── 用户与认证接口 ──────────────────────────────────────────────────────────


@pytest.fixture
def auth_app():
    from backend.api.routes import auth, user
    from backend.core.database import Base, get_db
    from backend.core.security import hash_password
    from backend.models.user import User

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[User.__table__])
    session_local = sessionmaker(bind=engine)

    def override_db():
        with session_local() as db:
            yield db

    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(user.router, prefix="/api/user")
    app.dependency_overrides[get_db] = override_db

    def add_user(username: str, password: str, totp_secret: str | None = None):
        with session_local() as db:
            db.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    totp_secret=totp_secret,
                )
            )
            db.commit()

    with patch("backend.api.routes.auth.BackgroundTasks.add_task"):
        yield TestClient(app), add_user, session_local


def _login(client, username, password, totp_code=None):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password, "totp_code": totp_code},
    )


def test_totp_reset_endpoints_are_removed(auth_app):
    client, add_user, _ = auth_app
    secret = pyotp.random_base32()
    add_user("admin", "s3cret-pass", totp_secret=secret)

    resp = client.post(
        "/api/auth/reset-totp", json={"username": "admin", "password": "s3cret-pass"}
    )
    assert resp.status_code in (404, 405)

    token = _login(client, "admin", "s3cret-pass", pyotp.TOTP(secret).now()).json()[
        "access_token"
    ]
    resp = client.post(
        "/api/user/totp/reset", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code in (404, 405)

    # 没有验证码仍然无法登录
    assert _login(client, "admin", "s3cret-pass").status_code == 401


def test_login_flags_default_password(auth_app):
    client, add_user, _ = auth_app
    add_user("admin", "admin123")
    add_user("other", "a-strong-pass")

    assert _login(client, "admin", "admin123").json()["must_change_password"] is True
    assert (
        _login(client, "other", "a-strong-pass").json()["must_change_password"] is False
    )


def test_change_password_rejects_default_password(auth_app):
    client, add_user, _ = auth_app
    add_user("admin", "old-password")
    token = _login(client, "admin", "old-password").json()["access_token"]

    resp = client.put(
        "/api/user/password",
        headers={"Authorization": f"Bearer {token}"},
        json={"old_password": "old-password", "new_password": "admin123"},
    )
    assert resp.status_code == 400
    assert _login(client, "admin", "old-password").status_code == 200


def test_offline_reset_totp_command(auth_app):
    from backend.cli import reset_totp as cli
    from backend.models.user import User

    _, add_user, session_local = auth_app
    add_user("admin", "pass-123456", totp_secret=pyotp.random_base32())

    with patch.object(cli, "get_session_local", return_value=session_local):
        assert cli.main(["admin"]) == 0
        assert cli.main(["missing"]) == 1
        assert cli.main([]) == 2

    with session_local() as db:
        assert db.query(User).filter_by(username="admin").one().totp_secret is None


# ── 跨域 ────────────────────────────────────────────────────────────────────


def test_cors_disabled_by_default():
    from backend.main import app

    client = TestClient(app)
    resp = client.get("/health", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


# ── 通用任务 ────────────────────────────────────────────────────────────────


class _FakeDb:
    def __init__(self):
        self.commits = 0

    def add(self, obj):
        pass

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        pass

    def rollback(self):
        pass


@pytest.mark.asyncio
async def test_task_failure_records_finished_at(tmp_path):
    import backend.services.tasks as task_service

    task = SimpleNamespace(
        id=987654,
        name="daily",
        account=SimpleNamespace(account_name="acc"),
        last_run_at=None,
    )

    async def boom(**kwargs):
        raise RuntimeError("cli crashed")

    with (
        patch.object(task_service, "async_run_task_cli", boom),
        patch.object(task_service, "_create_log_file", return_value=tmp_path / "x.log"),
    ):
        log = await task_service.run_task_once(_FakeDb(), task)

    assert log.status == "failed"
    assert log.finished_at is not None
    assert task.last_run_at == log.finished_at
    assert not task_service.is_task_running(task.id)


def test_task_ws_closes_with_4401_on_invalid_token():
    from starlette.websockets import WebSocketDisconnect

    import backend.api.routes.tasks as routes

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/tasks")
    with (
        patch.object(
            routes,
            "get_session_local",
            return_value=lambda: contextlib.nullcontext(None),
        ),
        patch.object(routes, "verify_token", return_value=None),
    ):
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect("/api/tasks/ws/1?token=bad") as ws,
        ):
            ws.receive_json()
    assert exc.value.code == 4401
