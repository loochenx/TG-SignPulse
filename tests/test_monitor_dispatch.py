"""Real Dispatcher coverage for keyword-monitor handler isolation."""

import asyncio
from collections import defaultdict
from types import SimpleNamespace

import pytest
from pyrogram import Client as BaseClient
from pyrogram import filters, raw
from pyrogram.handlers import MessageHandler

from backend.services import config, keyword_monitor
from tg_signer import core


class _OfflineClient(BaseClient):
    def __init__(self):
        super().__init__("monitor-dispatch", api_id=1, api_hash="x", in_memory=True)
        self._tg_signpulse_no_updates = False
        self.exit_calls = 0

    async def __aenter__(self):
        self.is_connected = True
        self.is_initialized = True
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *_args):
        self.exit_calls += 1
        await asyncio.sleep(0)


def _rule(account_name: str, task_name: str, chat_id: int):
    return keyword_monitor.KeywordMonitorRule(
        account_name=account_name,
        task_name=task_name,
        chat_id=chat_id,
        chat_name=task_name,
        message_thread_id=None,
        action={"action": 8, "keywords": "ping"},
    )


def _raw_user(user_id: int, *, bot: bool = False):
    return raw.types.User(
        id=user_id,
        first_name=f"user-{user_id}",
        access_hash=user_id,
        bot=bot,
        usernames=[],
        restriction_reason=[],
    )


def _private_message(user_id: int, message_id: int):
    return raw.types.Message(
        id=message_id,
        peer_id=raw.types.PeerUser(user_id=user_id),
        from_id=raw.types.PeerUser(user_id=user_id),
        date=1,
        message="ping private",
        entities=[],
        restriction_reason=[],
    )


def _group_message(chat_id: int, bot_id: int, message_id: int):
    return raw.types.Message(
        id=message_id,
        peer_id=raw.types.PeerChat(chat_id=chat_id),
        from_id=raw.types.PeerUser(user_id=bot_id),
        date=1,
        message="ping group bot",
        entities=[],
        restriction_reason=[],
    )


def _raw_group(chat_id: int):
    return raw.types.Chat(
        id=chat_id,
        title=f"group-{chat_id}",
        photo=raw.types.ChatPhotoEmpty(),
        participants_count=2,
        date=1,
        version=1,
    )


async def _wait_until(predicate, timeout: float = 1.0):
    async def waiter():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(waiter(), timeout)


@pytest.mark.asyncio
async def test_monitor_and_signer_handlers_receive_same_messages_once(
    monkeypatch, tmp_path
):
    account_name = "shared"
    private_id = 42
    group_id = 100
    group_chat_id = -group_id
    bot_id = 77
    rules = [
        _rule(account_name, "private", private_id),
        _rule(account_name, "group", group_chat_id),
    ]
    client = _OfflineClient()
    service = keyword_monitor.KeywordMonitorService()
    service._load_rules = lambda: rules
    service._start_watchdog = lambda: None
    monitor_seen = []

    async def on_monitor(name, callback_client, message):
        assert name == account_name
        assert callback_client is client
        monitor_seen.append((message.chat.id, message.id))

    service._on_message = on_monitor
    monkeypatch.setattr(keyword_monitor, "get_session_mode", lambda: "file")
    monkeypatch.setattr(keyword_monitor, "get_account_proxy", lambda _name: None)
    monkeypatch.setattr(
        keyword_monitor,
        "settings",
        SimpleNamespace(resolve_session_dir=lambda: tmp_path),
    )
    monkeypatch.setattr(
        config,
        "get_config_service",
        lambda: SimpleNamespace(
            get_global_settings=dict,
            get_telegram_config=lambda: {"api_id": 1, "api_hash": "hash"},
        ),
    )
    monkeypatch.setattr(core, "_CLIENT_INSTANCES", {})
    monkeypatch.setattr(core, "get_client", lambda *_args, **_kwargs: client)

    dispatcher_lock = asyncio.Lock()
    client.dispatcher.locks_list.append(dispatcher_lock)
    dispatcher_worker = asyncio.create_task(
        client.dispatcher.handler_worker(dispatcher_lock)
    )
    signer_ref = None
    try:
        await service.restart_from_tasks()
        assert len(service._handler_refs) == 1
        monitor_ref = service._handler_refs[0][2]
        assert monitor_ref[1] == keyword_monitor.MONITOR_HANDLER_GROUP

        signer = object.__new__(core.UserSigner)
        signer.context = SimpleNamespace(
            sign_chats={
                private_id: [SimpleNamespace(message_thread_id=None)],
                group_chat_id: [SimpleNamespace(message_thread_id=None)],
            },
            chat_messages=defaultdict(dict),
        )
        signer.log = lambda *args, **kwargs: None
        signer_ref = client.add_handler(
            MessageHandler(
                signer.on_message, filters.chat([private_id, group_chat_id])
            ),
            group=0,
        )
        await asyncio.sleep(0)

        private_update = raw.types.UpdateNewMessage(
            message=_private_message(private_id, 1), pts=1, pts_count=1
        )
        group_update = raw.types.UpdateNewMessage(
            message=_group_message(group_id, bot_id, 2), pts=2, pts_count=1
        )
        client.dispatcher.updates_queue.put_nowait(
            (private_update, {private_id: _raw_user(private_id)}, {})
        )
        client.dispatcher.updates_queue.put_nowait(
            (
                group_update,
                {bot_id: _raw_user(bot_id, bot=True)},
                {group_id: _raw_group(group_id)},
            )
        )
        await _wait_until(
            lambda: (
                len(monitor_seen) == 2
                and 1 in signer.context.chat_messages[private_id]
                and 2 in signer.context.chat_messages[group_chat_id]
            )
        )
        assert monitor_seen == [(private_id, 1), (group_chat_id, 2)]

        await service.stop()
        await asyncio.sleep(0)
        assert service._handler_refs == []
        assert monitor_ref[0] not in client.dispatcher.groups[
            keyword_monitor.MONITOR_HANDLER_GROUP
        ]

        client.dispatcher.updates_queue.put_nowait(
            (
                raw.types.UpdateNewMessage(
                    message=_private_message(private_id, 3), pts=3, pts_count=1
                ),
                {private_id: _raw_user(private_id)},
                {},
            )
        )
        await _wait_until(lambda: 3 in signer.context.chat_messages[private_id])
        assert monitor_seen == [(private_id, 1), (group_chat_id, 2)]
    finally:
        if signer_ref is not None:
            client.remove_handler(*signer_ref)
            await asyncio.sleep(0)
        client.dispatcher.updates_queue.put_nowait(None)
        await dispatcher_worker
        client.dispatcher.locks_list.clear()
