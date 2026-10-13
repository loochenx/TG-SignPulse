"""Regression tests for shared Kurigram client lifecycle cleanup."""

from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tg_signer import core


@pytest.fixture(autouse=True)
def _clean_client_cache():
    core._CLIENT_INSTANCES.clear()
    core._CLIENT_REFS.clear()
    core._CLIENT_ASYNC_LOCKS.clear()
    core._CLIENT_CLOSING.clear()
    yield
    core._CLIENT_INSTANCES.clear()
    core._CLIENT_REFS.clear()
    core._CLIENT_ASYNC_LOCKS.clear()
    core._CLIENT_CLOSING.clear()


def _client(tmp_path, name="acc", **kwargs):
    return core.get_client(
        name,
        workdir=tmp_path,
        api_id=1,
        api_hash="hash",
        **kwargs,
    )


async def _lock_storage(client):
    await client.storage.open()
    client.storage.conn.execute("CREATE TABLE IF NOT EXISTS probe (value INTEGER)")
    client.storage.conn.commit()
    client.storage.conn.execute("BEGIN EXCLUSIVE")


def _assert_database_writable(path):
    conn = core._original_sqlite3_connect(str(path), timeout=0.05)
    try:
        conn.execute("INSERT INTO probe VALUES (1)")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_enter_uses_real_sdk_start_without_double_connect(tmp_path):
    client = _client(tmp_path)
    calls = {"connect": 0, "initialize": 0, "terminate": 0, "disconnect": 0}

    async def connect(self):
        calls["connect"] += 1
        if self.is_connected:
            raise ConnectionError("Client is already connected")
        self.is_connected = True
        return True

    async def invoke(self, _query, *args, **kwargs):
        return SimpleNamespace()

    async def get_me(self):
        return SimpleNamespace(id=1)

    async def initialize(self):
        calls["initialize"] += 1
        self.is_initialized = True

    async def terminate(self):
        calls["terminate"] += 1
        self.is_initialized = False

    async def disconnect(self):
        calls["disconnect"] += 1
        self.is_connected = False

    client.connect = MethodType(connect, client)
    client.invoke = MethodType(invoke, client)
    client.get_me = MethodType(get_me, client)
    client.initialize = MethodType(initialize, client)
    client.terminate = MethodType(terminate, client)
    client.disconnect = MethodType(disconnect, client)

    async with client:
        assert calls == {
            "connect": 1,
            "initialize": 1,
            "terminate": 0,
            "disconnect": 0,
        }
        assert client.is_connected is True
        assert client.is_initialized is True

    assert calls["terminate"] == 1
    assert calls["disconnect"] == 1
    assert client.is_connected is False
    assert client.is_initialized is False


@pytest.mark.asyncio
async def test_exit_closes_partial_client_sqlite_lock(tmp_path):
    client = _client(tmp_path)
    await _lock_storage(client)
    client.session = SimpleNamespace(stop=AsyncMock())
    client.is_connected = True
    client.is_initialized = False
    client._tg_signpulse_refs = 1
    client._sync_shared_state()

    await client.__aexit__(None, None, None)

    assert client.is_connected is False
    assert core._CLIENT_REFS[client.key] == 0
    _assert_database_writable(tmp_path / "acc.session")


@pytest.mark.asyncio
async def test_force_close_releases_sqlite_before_replacement(tmp_path):
    client = _client(tmp_path)
    await _lock_storage(client)
    client.session = SimpleNamespace(stop=AsyncMock())
    client.is_connected = True
    client.is_initialized = False

    await core.close_client_by_name("acc", workdir=tmp_path)

    assert client.key not in core._CLIENT_INSTANCES
    _assert_database_writable(tmp_path / "acc.session")
    replacement = _client(tmp_path)
    assert replacement is not client


@pytest.mark.asyncio
async def test_failed_start_retires_client_and_releases_sqlite(tmp_path):
    client = _client(tmp_path)

    async def fail_start(self):
        await _lock_storage(self)
        raise RuntimeError("start failed after storage open")

    client.start = MethodType(fail_start, client)

    with pytest.raises(RuntimeError, match="start failed"):
        await client.__aenter__()

    assert client._tg_signpulse_retired is True
    assert client.key not in core._CLIENT_INSTANCES
    _assert_database_writable(tmp_path / "acc.session")


@pytest.mark.asyncio
async def test_cancelled_start_releases_sqlite_and_cache(tmp_path):
    client = _client(tmp_path)
    opened = asyncio.Event()

    async def hanging_start(self):
        await _lock_storage(self)
        opened.set()
        await asyncio.Event().wait()

    client.start = MethodType(hanging_start, client)
    entering = asyncio.create_task(client.__aenter__())
    await opened.wait()
    entering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entering

    assert client._tg_signpulse_retired is True
    assert client.key not in core._CLIENT_INSTANCES
    _assert_database_writable(tmp_path / "acc.session")


@pytest.mark.asyncio
async def test_old_exit_cannot_decrement_replacement_reference(tmp_path):
    old = _client(tmp_path)
    old._tg_signpulse_refs = 1
    old._sync_shared_state()
    await core.close_client_by_name("acc", workdir=tmp_path)

    replacement = _client(tmp_path)
    replacement._tg_signpulse_refs = 1
    replacement._sync_shared_state()

    await old.__aexit__(None, None, None)

    assert core._CLIENT_INSTANCES[replacement.key] is replacement
    assert core._CLIENT_REFS[replacement.key] == 1
    assert replacement._tg_signpulse_refs == 1


@pytest.mark.asyncio
async def test_replacement_is_not_published_until_close_finishes(monkeypatch, tmp_path):
    client = _client(tmp_path)
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def blocking_cleanup(target):
        assert target is client
        cleanup_started.set()
        await allow_cleanup.wait()
        target.is_connected = False
        target.is_initialized = False
        target._tg_signpulse_clean = True

    monkeypatch.setattr(core, "_finalize_client_resources", blocking_cleanup)
    closing = asyncio.create_task(core.close_client_by_name("acc", workdir=tmp_path))
    await cleanup_started.wait()

    with pytest.raises(RuntimeError, match="being closed"):
        _client(tmp_path)

    allow_cleanup.set()
    await closing
    assert _client(tmp_path) is not client


@pytest.mark.asyncio
async def test_force_cleanup_cancels_worker_blocked_on_account_lock(
    monkeypatch, tmp_path
):
    client = _client(tmp_path, no_updates=False)
    await client.storage.open()
    account_lock = asyncio.Lock()
    await account_lock.acquire()
    worker_started = asyncio.Event()

    async def blocked_worker():
        worker_started.set()
        async with account_lock:
            pass

    worker = asyncio.create_task(blocked_worker())
    watchdog = asyncio.create_task(asyncio.Event().wait())
    await worker_started.wait()
    client.dispatcher.handler_worker_tasks = [worker]
    client.updates_watchdog_task = watchdog
    client.session = SimpleNamespace(stop=AsyncMock())
    client.is_connected = True
    client.is_initialized = True
    monkeypatch.setattr(core, "_CLIENT_STOP_TIMEOUT", 0.01)

    try:
        await asyncio.wait_for(
            core.close_client_by_name("acc", workdir=tmp_path), timeout=0.2
        )
    finally:
        account_lock.release()

    assert worker.done()
    assert watchdog.done()
    assert client.key not in core._CLIENT_INSTANCES
    conn = core._original_sqlite3_connect(str(tmp_path / "acc.session"), timeout=0.05)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS after_close (value INTEGER)")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_partial_initialize_cancels_workers_even_when_disconnect_succeeds(
    tmp_path,
):
    client = _client(tmp_path, no_updates=False)
    await client.storage.open()
    worker_started = asyncio.Event()

    async def partial_worker():
        worker_started.set()
        await asyncio.Event().wait()

    worker = asyncio.create_task(partial_worker())
    watchdog = asyncio.create_task(asyncio.Event().wait())
    await worker_started.wait()
    client.dispatcher.handler_worker_tasks = [worker]
    client.updates_watchdog_task = watchdog
    client.session = SimpleNamespace(stop=AsyncMock())
    client.is_connected = True
    client.is_initialized = False

    await core.close_client_by_name("acc", workdir=tmp_path)

    assert worker.done()
    assert watchdog.done()
    assert client.session.stop.await_count == 1
    assert client.is_connected is False
    conn = core._original_sqlite3_connect(str(tmp_path / "acc.session"), timeout=0.05)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS after_partial (value INTEGER)")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_cancelled_session_start_tasks_are_closed_before_storage(
    monkeypatch, tmp_path
):
    client = _client(tmp_path)
    await _lock_storage(client)
    ping_task = asyncio.create_task(asyncio.Event().wait())
    recv_task = asyncio.create_task(asyncio.Event().wait())
    connection = SimpleNamespace(close=AsyncMock())

    async def hanging_stop():
        await asyncio.Event().wait()

    client.session = SimpleNamespace(
        stop=hanging_stop,
        ping_task=ping_task,
        recv_task=recv_task,
        connection=connection,
    )
    client.is_connected = False
    client.is_initialized = False
    monkeypatch.setattr(core, "_CLIENT_STOP_TIMEOUT", 0.01)

    await asyncio.wait_for(
        core.close_client_by_name("acc", workdir=tmp_path), timeout=0.2
    )

    assert ping_task.done()
    assert recv_task.done()
    connection.close.assert_awaited_once()
    _assert_database_writable(tmp_path / "acc.session")


@pytest.mark.asyncio
async def test_background_client_never_enters_interactive_authorize(
    monkeypatch, tmp_path
):
    client = _client(tmp_path)
    base_authorize = AsyncMock(
        side_effect=AssertionError("interactive authorize must not run")
    )
    monkeypatch.setattr(core.BaseClient, "authorize", base_authorize)

    async def unauthorized_connect(self):
        self.is_connected = True
        return False

    async def disconnect(self):
        self.is_connected = False

    client.connect = MethodType(unauthorized_connect, client)
    client.disconnect = MethodType(disconnect, client)

    with pytest.raises(ConnectionError, match="authorization required"):
        await client.__aenter__()

    base_authorize.assert_not_awaited()
    assert client.is_connected is False
    assert client.key not in core._CLIENT_INSTANCES


@pytest.mark.asyncio
async def test_signer_retry_refreshes_retired_client_after_transient_start_failure(
    monkeypatch, tmp_path
):
    signer = core.UserSigner(
        task_name="daily",
        session_dir=str(tmp_path),
        account="acc",
        workdir=tmp_path,
        api_id=1,
        api_hash="hash",
        no_updates=True,
    )
    first_client = signer.app
    calls = {"connect": 0, "initialize": 0, "terminate": 0, "disconnect": 0}

    async def connect(self):
        calls["connect"] += 1
        if calls["connect"] == 1:
            raise OSError("temporary network failure")
        self.is_connected = True
        return True

    async def invoke(self, _query, *args, **kwargs):
        return SimpleNamespace()

    async def get_me(self):
        return SimpleNamespace(id=1)

    async def initialize(self):
        calls["initialize"] += 1
        self.is_initialized = True

    async def terminate(self):
        calls["terminate"] += 1
        self.is_initialized = False

    async def disconnect(self):
        calls["disconnect"] += 1
        self.is_connected = False

    monkeypatch.setattr(core.Client, "connect", connect)
    monkeypatch.setattr(core.Client, "invoke", invoke)
    monkeypatch.setattr(core.Client, "get_me", get_me)
    monkeypatch.setattr(core.Client, "initialize", initialize)
    monkeypatch.setattr(core.Client, "terminate", terminate)
    monkeypatch.setattr(core.Client, "disconnect", disconnect)

    async def one_attempt(*_args, **_kwargs):
        async with signer.app:
            return "ok"

    signer.normal_run = one_attempt

    with pytest.raises(OSError, match="temporary network failure"):
        await signer.run_once(num_of_dialogs=20)
    assert first_client._tg_signpulse_retired is True

    # Mirrors SignTaskService's network-error branch before its next attempt.
    await core.close_client_by_name("acc", workdir=tmp_path)
    result = await signer.run_once(num_of_dialogs=20)

    assert result == "ok"
    assert signer.app is not first_client
    assert calls == {
        "connect": 2,
        "initialize": 1,
        "terminate": 1,
        "disconnect": 1,
    }
