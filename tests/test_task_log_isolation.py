"""签到任务日志按账号、任务和单次运行隔离的回归测试。"""

import asyncio
import logging
from collections import OrderedDict
from types import SimpleNamespace

import pytest
import pytest_asyncio


def test_log_prefix_fallback_requires_exact_account_and_task():
    from backend.services import sign_tasks

    buffer = sign_tasks.LogBuffer()
    handler = sign_tasks.TaskLogHandler(
        buffer, account_name="acc", task_name="daily"
    )

    def emit(message):
        record = logging.LogRecord(
            "tg-signer", logging.INFO, __file__, 1, message, (), None
        )
        handler.handle(record)

    emit("账户「acc」- 任务「daily」: fallback-current")
    emit("账户「acc」- 任务「other」: fallback-other-task")
    emit("账户「other」- 任务「daily」: fallback-other-account")
    emit("unscoped-network-log")

    assert list(buffer) == ["账户「acc」- 任务「daily」: fallback-current"]


def test_explicit_signer_run_identity_overrides_worker_context():
    from backend.services import sign_tasks

    buffer = sign_tasks.LogBuffer()
    handler = sign_tasks.TaskLogHandler(
        buffer, account_name="acc", task_name="daily"
    )
    current_run = ("acc", "daily", buffer.run_id)

    def emit(message, run):
        record = logging.LogRecord(
            "tg-signer", logging.INFO, __file__, 1, message, (), None
        )
        record._sign_task_run = run
        handler.handle(record)

    token = sign_tasks._current_task_run.set(("acc", "daily", buffer.run_id - 1))
    try:
        emit("账户「acc」- 任务「daily」: current-signer", current_run)
        emit(
            "账户「acc」- 任务「daily」: retired-signer",
            ("acc", "daily", buffer.run_id - 1),
        )
        emit(
            "账户「acc」- 任务「other」: other-task-signer",
            ("acc", "other", buffer.run_id),
        )
    finally:
        sign_tasks._current_task_run.reset(token)

    assert list(buffer) == ["账户「acc」- 任务「daily」: current-signer"]


@pytest_asyncio.fixture
async def sign_service(tmp_path, monkeypatch):
    from backend.services import config, sign_tasks

    real_sleep = asyncio.sleep

    async def fast_service_sleep(delay):
        if delay == 2:
            return
        await real_sleep(delay)

    fake_settings = SimpleNamespace(
        resolve_workdir=lambda: tmp_path,
        resolve_session_dir=lambda: tmp_path,
        resolve_logs_dir=lambda: tmp_path,
    )
    monkeypatch.setattr("backend.core.config.get_settings", lambda: fake_settings)
    monkeypatch.setattr(sign_tasks, "settings", fake_settings)
    monkeypatch.setattr(sign_tasks.asyncio, "sleep", fast_service_sleep)
    monkeypatch.setattr(sign_tasks, "get_session_mode", lambda: "file")
    monkeypatch.setattr(
        config,
        "get_config_service",
        lambda: SimpleNamespace(
            get_telegram_config=lambda: {"api_id": 1, "api_hash": "hash"}
        ),
    )
    monkeypatch.setattr(
        sign_tasks, "get_global_semaphore", lambda: asyncio.Semaphore(8)
    )
    account_locks = {}
    monkeypatch.setattr(
        sign_tasks,
        "get_account_lock",
        lambda account: account_locks.setdefault(account, asyncio.Lock()),
    )

    service = sign_tasks.SignTaskService()
    service._account_cooldown_seconds = 0
    service.get_task = lambda *args, **kwargs: {"chats": []}

    async def valid_account(*args, **kwargs):
        return "ok", None

    service._check_account_before_task = valid_account
    saved_runs = []
    service._save_run_info = lambda *args, **kwargs: saved_runs.append(
        (args, kwargs)
    )
    service._spawn_background = lambda coro: coro.close()

    tg_logger = logging.getLogger("tg-signer")
    old_level = tg_logger.level
    tg_logger.setLevel(logging.INFO)
    try:
        yield service, sign_tasks, saved_runs, real_sleep
    finally:
        for task in list(service._cleanup_tasks.values()):
            task.cancel()
        if service._cleanup_tasks:
            await asyncio.gather(
                *service._cleanup_tasks.values(), return_exceptions=True
            )
        tg_logger.setLevel(old_level)


@pytest.mark.asyncio
async def test_same_account_queued_failure_does_not_flip_next_task(
    sign_service, monkeypatch
):
    service, sign_tasks, _, real_sleep = sign_service
    a_started = asyncio.Event()
    release_a = asyncio.Event()

    class Signer:
        def __init__(self, task_name, **kwargs):
            self.task_name = task_name

        async def run_once(self, **kwargs):
            if self.task_name == "A":
                a_started.set()
                await release_a.wait()
                logging.getLogger("tg-signer").info(
                    "账户「same」- 任务「A」: 收到来自「bot」的消息: "
                    "Message: text: 签到失败"
                )
                raise RuntimeError("A failed")
            logging.getLogger("tg-signer").info("B network diagnostic")

    monkeypatch.setattr(sign_tasks, "BackendUserSigner", Signer)
    run_a = asyncio.create_task(service.run_task_with_logs("same", "A"))
    await a_started.wait()
    run_b = asyncio.create_task(service.run_task_with_logs("same", "B"))
    while not service.is_task_running("B", "same"):
        await real_sleep(0)
    release_a.set()
    result_a, result_b = await asyncio.gather(run_a, run_b)

    assert result_a["success"] is False
    assert result_b["success"] is True
    assert "B network diagnostic" in result_b["output"]
    assert "任务「A」" not in result_b["output"]
    assert "签到失败" not in result_b["output"]


@pytest.mark.asyncio
async def test_different_accounts_keep_prefixed_and_network_logs_isolated(
    sign_service, monkeypatch
):
    service, sign_tasks, _, real_sleep = sign_service
    both_started = asyncio.Event()
    started = set()

    class Signer:
        def __init__(self, task_name, account, **kwargs):
            self.task_name = task_name
            self.account = account

        async def run_once(self, **kwargs):
            started.add(self.account)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            task = self.task_name
            logging.getLogger("tg-signer").info("network-%s", self.account)
            logging.getLogger("tg-signer").info(
                "账户「%s」- 任务「%s」: account-%s", self.account, task, self.account
            )
            await real_sleep(0)

    monkeypatch.setattr(sign_tasks, "BackendUserSigner", Signer)
    result_one, result_two = await asyncio.gather(
        service.run_task_with_logs("one", "daily"),
        service.run_task_with_logs("two", "daily"),
    )

    assert "network-one" in result_one["output"]
    assert "account-one" in result_one["output"]
    assert "network-two" not in result_one["output"]
    assert "account-two" not in result_one["output"]
    assert "network-two" in result_two["output"]
    assert "account-two" in result_two["output"]
    assert "network-one" not in result_two["output"]
    assert "account-one" not in result_two["output"]


@pytest.mark.asyncio
async def test_inherited_callback_from_other_task_cannot_replace_last_reply(
    sign_service, monkeypatch
):
    service, sign_tasks, saved_runs, real_sleep = sign_service
    release_late_log = asyncio.Event()
    late_job = None

    class Signer:
        def __init__(self, task_name, **kwargs):
            self.task_name = task_name

        async def run_once(self, **kwargs):
            nonlocal late_job
            log = logging.getLogger("tg-signer")
            if self.task_name == "A":

                async def late_callback():
                    await release_late_log.wait()
                    log.info(
                        "账户「same」- 任务「A」: 收到来自「bot」的消息: "
                        "Message: text: A迟到回复"
                    )

                late_job = asyncio.create_task(late_callback())
                return
            log.info(
                "账户「same」- 任务「B」: 收到来自「bot」的消息: "
                "Message: text: B本次回复"
            )
            release_late_log.set()
            await real_sleep(0)

    monkeypatch.setattr(sign_tasks, "BackendUserSigner", Signer)
    await service.run_task_with_logs("same", "A")
    result_b = await service.run_task_with_logs("same", "B")
    await late_job

    b_runs = [entry for entry in saved_runs if entry[0][0] == "B"]
    assert b_runs[-1][0][2] == "B本次回复"
    assert "B本次回复" in result_b["output"]
    assert "A迟到回复" not in result_b["output"]


@pytest.mark.asyncio
async def test_new_run_rejects_old_same_task_callback_and_cleans_state(
    sign_service, monkeypatch
):
    service, sign_tasks, saved_runs, real_sleep = sign_service
    tg_logger = logging.getLogger("tg-signer")
    initial_handlers = list(tg_logger.handlers)
    initial_context = sign_tasks._current_task_run.get()
    release_old_log = asyncio.Event()
    late_job = None
    run_number = 0

    class Signer:
        def __init__(self, **kwargs):
            pass

        async def run_once(self, **kwargs):
            nonlocal late_job, run_number
            run_number += 1
            log = logging.getLogger("tg-signer")
            if run_number == 1:

                async def old_callback():
                    await release_old_log.wait()
                    log.info(
                        "账户「same」- 任务「daily」: 收到来自「bot」的消息: "
                        "Message: text: 第一轮迟到回复"
                    )

                late_job = asyncio.create_task(old_callback())
                return
            log.info(
                "账户「same」- 任务「daily」: 收到来自「bot」的消息: "
                "Message: text: 第二轮当前回复"
            )
            release_old_log.set()
            await real_sleep(0)

    monkeypatch.setattr(sign_tasks, "BackendUserSigner", Signer)
    await service.run_task_with_logs("same", "daily")
    result = await service.run_task_with_logs("same", "daily")
    await late_job

    daily_runs = [entry for entry in saved_runs if entry[0][0] == "daily"]
    assert daily_runs[-1][0][2] == "第二轮当前回复"
    assert "第二轮当前回复" in result["output"]
    assert "第一轮迟到回复" not in result["output"]
    assert sign_tasks._current_task_run.get() == initial_context
    assert list(tg_logger.handlers) == initial_handlers


@pytest.mark.asyncio
async def test_reused_dispatcher_old_context_accepts_current_signer_reply(
    sign_service, monkeypatch
):
    from pyrogram import Client as BaseClient
    from pyrogram import raw
    from pyrogram.handlers import MessageHandler

    from tg_signer import core

    service, sign_tasks, saved_runs, real_sleep = sign_service
    service.get_task = lambda *args, **kwargs: {
        "chats": [{"actions": [{"action": 3}]}]
    }
    client = BaseClient(
        "old-dispatcher-context", api_id=1, api_hash="hash", in_memory=True
    )
    old_context = ("same", "daily", 999_999)
    token = sign_tasks._current_task_run.set(old_context)
    try:
        worker = asyncio.create_task(
            client.dispatcher.handler_worker(asyncio.Lock())
        )
    finally:
        sign_tasks._current_task_run.reset(token)

    monkeypatch.setattr(core, "get_client", lambda *args, **kwargs: client)
    chat_id = 42
    message_id = 7

    async def run_once(signer, **kwargs):
        signer.context.sign_chats[chat_id].append(
            SimpleNamespace(message_thread_id=None)
        )
        message = SimpleNamespace(
            id=message_id,
            text="签到失败",
            photo=None,
            caption=None,
            reply_markup=None,
            from_user=SimpleNamespace(username="bot", id=1),
            chat=SimpleNamespace(id=chat_id),
            message_thread_id=None,
            reply_to_top_message_id=None,
        )

        async def parse_update(update, users, chats):
            return message, MessageHandler

        client.dispatcher.update_parsers[raw.types.UpdateNewMessage] = parse_update
        client.dispatcher.groups = OrderedDict(
            {0: [MessageHandler(signer.on_message)]}
        )
        client.dispatcher.updates_queue.put_nowait(
            (
                raw.types.UpdateNewMessage(
                    message=raw.types.Message(
                        id=message_id,
                        peer_id=raw.types.PeerUser(user_id=chat_id),
                        from_id=raw.types.PeerUser(user_id=1),
                        date=1,
                        message="签到失败",
                        entities=[],
                        restriction_reason=[],
                    ),
                    pts=1,
                    pts_count=1,
                ),
                {},
                {},
            )
        )

        async def delivered():
            while message_id not in signer.context.chat_messages[chat_id]:
                await real_sleep(0)

        await asyncio.wait_for(delivered(), timeout=0.5)

    monkeypatch.setattr(sign_tasks.BackendUserSigner, "run_once", run_once)
    try:
        result = await service.run_task_with_logs("same", "daily")
    finally:
        client.dispatcher.updates_queue.put_nowait(None)
        await worker

    assert result["success"] is False
    assert "收到来自「bot」的消息" in result["output"]
    assert "签到失败" in result["output"]
    assert "机器人回复疑似失败: 签到失败" in result["error"]
    assert saved_runs[-1][0][2] == "机器人回复疑似失败: 签到失败"
