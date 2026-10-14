"""签到网络异常的回归测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_one_shot_signer_propagates_network_error_instead_of_looping():
    from tg_signer.core import UserSigner

    class FailingApp:
        is_connected = False

        async def __aenter__(self):
            await self.start()
            return self

        async def __aexit__(self, *_args):
            return False

        async def start(self):
            raise OSError("simulated network failure")

    signer = object.__new__(UserSigner)
    signer.user = object()  # 跳过登录，直接覆盖任务执行阶段的错误路径
    signer.app = FailingApp()
    signer.load_config = lambda *args: SimpleNamespace(
        requires_ai=False,
        requires_updates=False,
        chats=[SimpleNamespace(chat_id=1)],
        sign_interval=0,
        random_seconds=0,
        sign_at="0 8 * * *",
    )
    signer.load_sign_record = dict
    signer._validate_sign_at = lambda value: value
    signer._refresh_retired_client = lambda: None
    signer.log = lambda *args, **kwargs: None

    with pytest.raises(OSError, match="simulated network failure"):
        await asyncio.wait_for(signer.normal_run(only_once=True), timeout=0.2)


@pytest.mark.asyncio
async def test_transient_precheck_skips_signer_and_returns_retryable_failure(tmp_path):
    from backend.services.sign_tasks import SignTaskService

    fake_settings = SimpleNamespace(resolve_workdir=lambda: tmp_path)
    with patch("backend.core.config.get_settings", return_value=fake_settings):
        service = SignTaskService()

    async def transient(*args, **kwargs):
        return "transient", "Request timed out"

    service.get_task = lambda *args, **kwargs: {"chats": []}
    service._check_account_before_task = transient
    service._save_run_info = lambda *args, **kwargs: None

    async def no_notification(*args, **kwargs):
        return None

    service._send_failure_notification = no_notification
    with patch(
        "backend.services.sign_tasks.BackendUserSigner",
        side_effect=AssertionError("transient precheck must not run a signer"),
    ):
        result = await service.run_task_with_logs("acc", "daily")

    assert result["success"] is False
    assert result["account_invalid"] is False
    assert "未执行签到" in result["error"]
    cleanup = service._cleanup_tasks.get(("acc", "daily"))
    if cleanup:
        cleanup.cancel()


@pytest.mark.asyncio
async def test_status_timeout_is_persisted_as_checking(tmp_path):
    from backend.services import telegram

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get_me(self):
            return object()

    service = telegram.TelegramService.__new__(telegram.TelegramService)
    service.session_dir = tmp_path
    service.account_exists = lambda account_name: True
    updates = []

    def record_status(account_name, **kwargs):
        updates.append((account_name, kwargs))

    with (
        patch("tg_signer.core.get_client", return_value=Client()),
        patch.object(telegram, "set_account_status", side_effect=record_status),
        patch.object(telegram.asyncio, "wait_for", side_effect=asyncio.TimeoutError),
    ):
        result = await service.check_account_status("acc")

    assert result["code"] == "TIMEOUT"
    assert updates == [
        (
            "acc",
            {
                "status": "checking",
                "message": "Request timed out",
                "code": "TIMEOUT",
                "needs_relogin": False,
            },
        )
    ]


def _monitor_for_health_test(monkeypatch, client):
    from unittest.mock import AsyncMock

    from backend.services import keyword_monitor

    svc = keyword_monitor.KeywordMonitorService()
    lock = asyncio.Lock()
    monkeypatch.setattr(keyword_monitor, "get_account_lock", lambda _: lock)
    svc._rules = [SimpleNamespace(account_name="acc")]
    svc._handler_refs = [("acc", client, None)]
    svc._active_key = "same-config"
    svc.restart_from_tasks = AsyncMock()
    return svc, lock


def _health_client(get_me):
    return SimpleNamespace(
        is_connected=True, is_initialized=True, _tg_signpulse_no_updates=False, get_me=get_me
    )


@pytest.mark.asyncio
async def test_monitor_rebuilds_unresponsive_connected_client_after_two_failures(monkeypatch):
    from unittest.mock import AsyncMock

    get_me = AsyncMock(side_effect=asyncio.TimeoutError)
    svc, lock = _monitor_for_health_test(monkeypatch, _health_client(get_me))

    await svc._watchdog_tick()
    svc.restart_from_tasks.assert_not_awaited()
    await svc._watchdog_tick()

    svc.restart_from_tasks.assert_awaited_once()
    assert svc._active_key == ""  # 不复用原配置下仍标记 connected 的坏连接
    assert not lock.locked()


@pytest.mark.asyncio
async def test_monitor_healthy_reply_resets_consecutive_failures(monkeypatch):
    from unittest.mock import AsyncMock

    get_me = AsyncMock(
        side_effect=[asyncio.TimeoutError, SimpleNamespace(id=1), asyncio.TimeoutError]
    )
    svc, _ = _monitor_for_health_test(monkeypatch, _health_client(get_me))
    for _ in range(3):
        await svc._watchdog_tick()

    svc.restart_from_tasks.assert_not_awaited()
    assert get_me.await_count == 3


@pytest.mark.asyncio
async def test_monitor_probe_timeout_cancels_rpc_and_releases_account_lock(monkeypatch):
    from backend.services import keyword_monitor

    cancelled = asyncio.Event()

    async def hanging_rpc():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(keyword_monitor, "MONITOR_HEALTH_TIMEOUT", 0.01)
    svc, lock = _monitor_for_health_test(monkeypatch, _health_client(hanging_rpc))
    await asyncio.wait_for(svc._watchdog_tick(), timeout=0.2)

    assert cancelled.is_set()
    assert not lock.locked()
    svc.restart_from_tasks.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_probe_skips_account_busy_with_signing(monkeypatch):
    from unittest.mock import AsyncMock

    get_me = AsyncMock(return_value=SimpleNamespace(id=1))
    svc, lock = _monitor_for_health_test(monkeypatch, _health_client(get_me))
    await lock.acquire()
    try:
        await asyncio.wait_for(svc._watchdog_tick(), timeout=0.2)
        get_me.assert_not_awaited()
        svc.restart_from_tasks.assert_not_awaited()
        assert lock.locked()
    finally:
        lock.release()
    await svc._watchdog_tick()
    get_me.assert_awaited_once()


@pytest.mark.asyncio
async def test_monitor_retries_configured_listener_after_failed_start(monkeypatch):
    from unittest.mock import AsyncMock

    svc, _ = _monitor_for_health_test(
        monkeypatch, _health_client(AsyncMock(return_value=SimpleNamespace(id=1)))
    )
    svc._handler_refs = []

    await svc._watchdog_tick()

    svc.restart_from_tasks.assert_awaited_once()
    assert svc._active_key == ""


@pytest.mark.asyncio
async def test_monitor_respects_full_rpc_flood_wait(monkeypatch):
    from unittest.mock import AsyncMock

    from pyrogram import errors

    from backend.services import keyword_monitor

    now = [100.0]
    monkeypatch.setattr(keyword_monitor.time, "monotonic", lambda: now[0])
    get_me = AsyncMock(side_effect=[errors.FloodWait(value=4), SimpleNamespace(id=1)])
    svc, _ = _monitor_for_health_test(monkeypatch, _health_client(get_me))

    await svc._watchdog_tick()
    now[0] = 103.0
    await svc._watchdog_tick()
    assert get_me.await_count == 1
    now[0] = 104.0
    await svc._watchdog_tick()

    assert get_me.await_count == 2
    svc.restart_from_tasks.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_ignores_probe_result_from_replaced_client(monkeypatch):
    from unittest.mock import AsyncMock

    fresh = _health_client(AsyncMock(return_value=SimpleNamespace(id=1)))

    async def old_rpc():
        svc._handler_refs = [("acc", fresh, None)]
        raise asyncio.TimeoutError

    svc, _ = _monitor_for_health_test(monkeypatch, _health_client(old_rpc))
    await svc._watchdog_tick()
    await svc._watchdog_tick()

    svc.restart_from_tasks.assert_not_awaited()
    assert svc._health_failures == {}
    fresh.get_me.assert_awaited_once()


@pytest.mark.asyncio
async def test_monitor_flood_response_breaks_connection_failure_streak(monkeypatch):
    from unittest.mock import AsyncMock

    from pyrogram import errors

    from backend.services import keyword_monitor

    now = [100.0]
    monkeypatch.setattr(keyword_monitor.time, "monotonic", lambda: now[0])
    get_me = AsyncMock(
        side_effect=[asyncio.TimeoutError, errors.FloodWait(value=4), asyncio.TimeoutError]
    )
    svc, _ = _monitor_for_health_test(monkeypatch, _health_client(get_me))
    await svc._watchdog_tick()
    await svc._watchdog_tick()
    now[0] = 104.0
    await svc._watchdog_tick()

    assert get_me.await_count == 3
    svc.restart_from_tasks.assert_not_awaited()
