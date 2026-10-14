"""Integration coverage for a signer's managed client lifecycle."""

from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace

import pytest
import pytest_asyncio
from pyrogram import raw

from tg_signer import core, update_guard


class _Storage:
    conn = None

    async def update_peers(self, _peers):
        return None

    async def update_usernames(self, _usernames):
        return None

    async def update_state(self, _value):
        return None


@pytest_asyncio.fixture(autouse=True)
async def _clean_shared_clients():
    core._CLIENT_INSTANCES.clear()
    core._CLIENT_REFS.clear()
    core._CLIENT_ASYNC_LOCKS.clear()
    core._CLIENT_CLOSING.clear()
    update_guard.reset_update_guard_state()
    yield
    for client in list(core._CLIENT_INSTANCES.values()):
        if getattr(client, "_tg_signpulse_refs", 0) > 0:
            await client.__aexit__(None, None, None)
    core._CLIENT_INSTANCES.clear()
    core._CLIENT_REFS.clear()
    core._CLIENT_ASYNC_LOCKS.clear()
    core._CLIENT_CLOSING.clear()
    update_guard.reset_update_guard_state()
    await asyncio.sleep(0)


def _client(tmp_path):
    client = core.get_client(
        "acc",
        workdir=tmp_path,
        api_id=1,
        api_hash="hash",
        no_updates=False,
    )
    client.storage = _Storage()
    return client


def _install_sdk_runtime(client):
    calls = {"start": 0, "stop": 0}

    async def start(self):
        calls["start"] += 1
        self.is_connected = True
        self.is_initialized = True
        lock = asyncio.Lock()
        self.dispatcher.locks_list.append(lock)
        worker = self.loop.create_task(self.dispatcher.handler_worker(lock))
        self.dispatcher.handler_worker_tasks.append(worker)
        return self

    async def stop(self):
        calls["stop"] += 1
        # add_handler/remove_handler update Dispatcher groups asynchronously.
        await asyncio.sleep(0)
        workers = list(self.dispatcher.handler_worker_tasks)
        for _ in workers:
            self.dispatcher.updates_queue.put_nowait(None)
        if workers:
            await asyncio.gather(*workers)
        self.dispatcher.handler_worker_tasks.clear()
        self.dispatcher.locks_list.clear()
        self.is_initialized = False
        self.is_connected = False

    client.start = MethodType(start, client)
    client.stop = MethodType(stop, client)
    return calls


def _signer(tmp_path):
    signer = core.UserSigner(
        task_name="daily",
        session_dir=str(tmp_path),
        account="acc",
        workdir=tmp_path,
        api_id=1,
        api_hash="hash",
        no_updates=False,
    )
    signer.user = SimpleNamespace(id=1)
    signer.log = lambda *_args, **_kwargs: None
    signer.load_sign_record = dict
    return signer


def _config(chat):
    return SimpleNamespace(
        requires_ai=False,
        requires_updates=True,
        chats=[chat],
        sign_interval=0,
        random_seconds=0,
        sign_at="0 8 * * *",
    )


def _group_packet(chat_id: int, message_id: int):
    user = raw.types.User(
        id=7,
        first_name="bot",
        access_hash=7,
        bot=True,
        usernames=[],
        restriction_reason=[],
    )
    chat = raw.types.Chat(
        id=chat_id,
        title="sign-group",
        photo=raw.types.ChatPhotoEmpty(),
        participants_count=2,
        date=1,
        version=1,
    )
    message = raw.types.Message(
        id=message_id,
        peer_id=raw.types.PeerChat(chat_id=chat_id),
        from_id=raw.types.PeerUser(user_id=user.id),
        date=1,
        message="signed",
        entities=[],
        restriction_reason=[],
    )
    update = raw.types.UpdateNewMessage(message=message, pts=1, pts_count=1)
    return raw.types.Updates(
        updates=[update], users=[user], chats=[chat], date=1, seq=1
    )


async def _wait_until(predicate, timeout=1.0):
    async def waiter():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(waiter(), timeout)


@pytest.mark.asyncio
async def test_precheck_last_exit_then_run_once_reactivates_guard_and_delivers_message(
    tmp_path,
):
    client = _client(tmp_path)
    calls = _install_sdk_runtime(client)

    # This mirrors the account precheck when no persistent monitor owns a ref.
    async with client:
        assert update_guard._client_state(client).closed is False
    assert update_guard._client_state(client).closed is True

    signer = _signer(tmp_path)
    assert signer.app is client
    chat = SimpleNamespace(chat_id=-10, message_thread_id=None)
    signer.load_config = lambda *_args: _config(chat)
    observed = []

    async def sign_a_chat(self, _chat):
        await _wait_until(
            lambda: any(
                handler.callback == self.on_message
                for handlers in self.app.dispatcher.groups.values()
                for handler in handlers
            )
        )
        assert update_guard._client_state(self.app).closed is False
        await self.app.handle_updates(_group_packet(10, 77))
        await _wait_until(lambda: 77 in self.context.chat_messages[-10])
        observed.append(self.context.chat_messages[-10][77].text)

    signer.sign_a_chat = MethodType(sign_a_chat, signer)

    await signer.run_once(20)

    assert observed == ["signed"]
    assert calls == {"start": 2, "stop": 2}
    assert client._tg_signpulse_refs == 0
    assert client.is_connected is False
    assert client.is_initialized is False
    assert client._tg_signpulse_clean is True
    assert update_guard._client_state(client).closed is True


@pytest.mark.asyncio
async def test_run_once_failure_releases_client_and_closes_guard(tmp_path):
    client = _client(tmp_path)
    calls = _install_sdk_runtime(client)
    signer = _signer(tmp_path)
    chat = SimpleNamespace(chat_id=-10, message_thread_id=None)
    signer.load_config = lambda *_args: _config(chat)

    async def fail(_self, _chat):
        raise RuntimeError("business failure")

    signer.sign_a_chat = MethodType(fail, signer)

    with pytest.raises(RuntimeError, match="1/1.*business failure"):
        await signer.run_once(20)

    assert calls == {"start": 1, "stop": 1}
    assert client._tg_signpulse_refs == 0
    assert client._tg_signpulse_clean is True
    assert update_guard._client_state(client).closed is True


@pytest.mark.asyncio
async def test_run_once_reuses_existing_client_without_stopping_owner(tmp_path):
    client = _client(tmp_path)
    calls = _install_sdk_runtime(client)
    await client.__aenter__()  # Persistent monitor owns the first reference.

    try:
        signer = _signer(tmp_path)
        chat = SimpleNamespace(chat_id=-10, message_thread_id=None)
        config = _config(chat)
        config.requires_updates = False
        signer.load_config = lambda *_args: config

        async def succeed(_self, _chat):
            return None

        signer.sign_a_chat = MethodType(succeed, signer)
        await signer.run_once(20)

        assert calls == {"start": 1, "stop": 0}
        assert client._tg_signpulse_refs == 1
        assert client.is_connected is True
        assert update_guard._client_state(client).closed is False
    finally:
        await client.__aexit__(None, None, None)

    assert calls == {"start": 1, "stop": 1}
    assert update_guard._client_state(client).closed is True


@pytest.mark.asyncio
async def test_run_once_cancellation_releases_client_and_closes_guard(tmp_path):
    client = _client(tmp_path)
    calls = _install_sdk_runtime(client)
    signer = _signer(tmp_path)
    chat = SimpleNamespace(chat_id=-10, message_thread_id=None)
    signer.load_config = lambda *_args: _config(chat)
    entered = asyncio.Event()

    async def hang(_self, _chat):
        entered.set()
        await asyncio.Event().wait()

    signer.sign_a_chat = MethodType(hang, signer)
    task = asyncio.create_task(signer.run_once(20))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == {"start": 1, "stop": 1}
    assert client._tg_signpulse_refs == 0
    assert client._tg_signpulse_clean is True
    assert update_guard._client_state(client).closed is True
