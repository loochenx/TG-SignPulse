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
