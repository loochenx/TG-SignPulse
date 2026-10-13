"""Channel difference synchronization regression tests."""

import asyncio
from types import SimpleNamespace

import pytest
from pyrogram import Client as BaseClient
from pyrogram import errors, raw

from tg_signer import core


class _Client:
    def __init__(self):
        self.session = object()


def _query(channel_id: int, pts: int = 10):
    return raw.functions.updates.GetChannelDifference(
        channel=raw.types.InputChannel(channel_id=channel_id, access_hash=1),
        filter=raw.types.ChannelMessagesFilterEmpty(),
        pts=pts,
        limit=100,
        force=False,
    )


def _empty(pts: int = 10):
    return raw.types.updates.ChannelDifferenceEmpty(
        pts=pts,
        timeout=0,
        final=True,
    )


@pytest.fixture(autouse=True)
def _reset_difference_state(monkeypatch):
    core._channel_diff_states.clear()
    monkeypatch.setattr(core, "_channel_diff_active", 0)
    monkeypatch.setattr(core, "_channel_diff_next_cleanup", 0.0)
    monkeypatch.setattr(core, "random", SimpleNamespace(uniform=lambda *_: 0.0))
    yield
    core._channel_diff_states.clear()


@pytest.mark.asyncio
async def test_same_channel_is_not_queued_twice_and_success_allows_next_request(
    monkeypatch,
):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def invoke(_client, query, *args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _empty(query.pts)

    monkeypatch.setattr(core, "_original_invoke", invoke)
    client = _Client()

    first = asyncio.create_task(core._patched_invoke(client, _query(1, pts=11)))
    await asyncio.wait_for(started.wait(), timeout=0.5)

    duplicate = await asyncio.wait_for(
        core._patched_invoke(client, _query(1, pts=12)), timeout=0.1
    )
    assert calls == 1
    assert duplicate.pts == 12
    assert duplicate.final is None

    release.set()
    assert (await first).pts == 11

    assert (await core._patched_invoke(client, _query(1, pts=13))).pts == 13
    assert calls == 2


@pytest.mark.asyncio
async def test_global_limit_rejects_overflow_without_queueing(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0
    calls = 0

    async def invoke(_client, query, *args, **kwargs):
        nonlocal active, max_active, calls
        calls += 1
        active += 1
        max_active = max(max_active, active)
        if active == core._CHANNEL_DIFF_CONCURRENCY:
            started.set()
        try:
            await release.wait()
            return _empty(query.pts)
        finally:
            active -= 1

    monkeypatch.setattr(core, "_original_invoke", invoke)
    client = _Client()
    running = [
        asyncio.create_task(core._patched_invoke(client, _query(channel_id)))
        for channel_id in range(1, core._CHANNEL_DIFF_CONCURRENCY + 1)
    ]
    await asyncio.wait_for(started.wait(), timeout=0.5)

    overflow = await asyncio.wait_for(
        core._patched_invoke(client, _query(999, pts=99)), timeout=0.1
    )
    assert overflow.pts == 99
    assert calls == core._CHANNEL_DIFF_CONCURRENCY
    assert max_active == core._CHANNEL_DIFF_CONCURRENCY

    release.set()
    await asyncio.gather(*running)


@pytest.mark.asyncio
async def test_timeout_has_one_attempt_and_cooldown_resets_after_success(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(core, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []
    fail = [True]

    async def invoke(_client, query, *args, **kwargs):
        calls.append(kwargs)
        if fail[0]:
            clock[0] += 20.0
            raise TimeoutError("Request timed out")
        return _empty(query.pts)

    monkeypatch.setattr(core, "_original_invoke", invoke)
    client = _Client()

    first = await core._patched_invoke(client, _query(1))
    assert first.pts == 10
    assert len(calls) == 1
    assert calls[0]["timeout"] >= 15
    assert calls[0]["retries"] == 0
    assert calls[0]["sleep_threshold"] == 0

    await core._patched_invoke(client, _query(1))
    assert len(calls) == 1

    clock[0] += core._CHANNEL_DIFF_BASE_COOLDOWN
    fail[0] = False
    await core._patched_invoke(client, _query(1))
    await core._patched_invoke(client, _query(1))
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_flood_wait_uses_complete_server_interval(monkeypatch):
    clock = [200.0]
    monkeypatch.setattr(core, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = 0
    fail = [True]

    async def invoke(_client, query, *args, **kwargs):
        nonlocal calls
        calls += 1
        if fail[0]:
            raise errors.FloodWait(4)
        return _empty(query.pts)

    monkeypatch.setattr(core, "_original_invoke", invoke)
    client = _Client()

    await core._patched_invoke(client, _query(1))
    clock[0] += 3.99
    await core._patched_invoke(client, _query(1))
    assert calls == 1

    clock[0] += 0.01
    fail[0] = False
    await core._patched_invoke(client, _query(1))
    assert calls == 2


@pytest.mark.asyncio
async def test_state_is_bounded_and_failure_log_is_rate_limited(
    monkeypatch, caplog
):
    clock = [300.0]
    monkeypatch.setattr(core, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(core, "_CHANNEL_DIFF_MAX_STATES", 3)
    calls = 0

    async def invoke(_client, query, *args, **kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("Request timed out")

    monkeypatch.setattr(core, "_original_invoke", invoke)
    client = _Client()

    with caplog.at_level("WARNING", logger="tg-signer"):
        await core._patched_invoke(client, _query(1))
        await core._patched_invoke(client, _query(1))
        for channel_id in range(2, 8):
            await core._patched_invoke(client, _query(channel_id))

    assert len(core._channel_diff_states) <= 3
    assert calls == 3
    failure_logs = [
        record
        for record in caplog.records
        if record.message.startswith("Temporarily suppress GetChannelDifference")
    ]
    # Channel 1 was attempted twice but logged only once during its window.
    assert len(failure_logs) == 3

    clock[0] += core._CHANNEL_DIFF_STATE_TTL + 1.0
    await core._patched_invoke(client, _query(8))
    assert calls == 4
    assert len(core._channel_diff_states) == 1


@pytest.mark.asyncio
async def test_get_difference_and_regular_rpc_keep_sdk_behavior(monkeypatch):
    calls = []
    marker = object()

    async def invoke(client, query, *args, **kwargs):
        calls.append((client, query, args, kwargs))
        return marker

    monkeypatch.setattr(core, "_original_invoke", invoke)
    client = _Client()
    difference = raw.functions.updates.GetDifference(pts=1, date=2, qts=3)
    regular = raw.functions.help.GetConfig()

    assert await core._patched_invoke(client, difference, "arg", custom=True) is marker
    assert await core._patched_invoke(client, regular) is marker
    assert calls == [
        (client, difference, ("arg",), {"custom": True}),
        (client, regular, (), {}),
    ]


@pytest.mark.asyncio
async def test_kurigram_handle_updates_enqueues_original_update_during_cooldown(
    monkeypatch,
):
    channel_id = 123
    original_calls = 0

    async def invoke(_client, query, *args, **kwargs):
        nonlocal original_calls
        original_calls += 1
        raise TimeoutError("Request timed out")

    monkeypatch.setattr(core, "_original_invoke", invoke)

    class Client:
        def __init__(self):
            self.session = object()
            self.skip_updates = True
            self.dispatcher = SimpleNamespace(updates_queue=asyncio.Queue())
            self.last_update_time = None

        async def fetch_peers(self, peers):
            return any(getattr(peer, "min", False) for peer in peers)

        async def resolve_peer(self, _peer_id):
            return raw.types.InputChannel(channel_id=channel_id, access_hash=1)

        async def invoke(self, query, *args, **kwargs):
            return await core._patched_invoke(self, query, *args, **kwargs)

    client = Client()
    # Establish a cooldown for this exact client session and channel.
    await core._patched_invoke(client, _query(channel_id))
    assert original_calls == 1

    message = raw.types.Message(
        id=77,
        peer_id=raw.types.PeerChannel(channel_id=channel_id),
        date=1,
        message="current update",
    )
    update = raw.types.UpdateNewChannelMessage(
        message=message,
        pts=20,
        pts_count=1,
    )
    min_channel = raw.types.Channel(
        id=channel_id,
        title="min peer",
        photo=raw.types.ChatPhotoEmpty(),
        date=1,
        min=True,
        broadcast=True,
        access_hash=1,
    )
    updates = raw.types.Updates(
        updates=[update],
        users=[],
        chats=[min_channel],
        date=1,
        seq=1,
    )

    await BaseClient.handle_updates(client, updates)

    queued_update, users, chats = client.dispatcher.updates_queue.get_nowait()
    assert queued_update is update
    assert users == {}
    assert chats[channel_id] is min_channel
    # The cooldown short-circuited peer enrichment without another RPC.
    assert original_calls == 1


@pytest.mark.asyncio
async def test_cancellation_releases_capacity_and_channel_in_flight(monkeypatch):
    started = asyncio.Event()
    calls = 0

    async def invoke(_client, query, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()
        return _empty(query.pts)

    monkeypatch.setattr(core, "_original_invoke", invoke)
    client = _Client()
    query = _query(321)

    pending = asyncio.create_task(core._patched_invoke(client, query))
    await asyncio.wait_for(started.wait(), timeout=0.5)
    key = core._channel_difference_key(client, query)
    state = core._channel_diff_states[key]
    assert core._channel_diff_active == 1
    assert state.in_flight is True

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert core._channel_diff_active == 0
    assert state.in_flight is False
    assert (await core._patched_invoke(client, query)).pts == query.pts
    assert calls == 2
