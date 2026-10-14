import asyncio
from collections import OrderedDict, defaultdict
from types import MethodType, SimpleNamespace

import pytest
import pytest_asyncio
from pyrogram import Client as BaseClient
from pyrogram import filters, raw, utils
from pyrogram.handlers import EditedMessageHandler, MessageHandler

from tg_signer import core, update_guard

_NO_VALUE = object()


class Storage:
    def __init__(self, peers=None):
        self.peers = peers or {}
        self.states = []
        self.state_rows = {}

    async def get_peer_by_id(self, peer_id):
        try:
            return self.peers[peer_id]
        except KeyError:
            raise KeyError(peer_id) from None

    async def update_peers(self, peers):
        return None

    async def update_usernames(self, usernames):
        return None

    async def update_state(self, value=_NO_VALUE):
        if value is _NO_VALUE:
            return list(self.state_rows.values())
        if isinstance(value, int):
            self.state_rows.pop(value, None)
            return None
        self.states.append(value)
        self.state_rows[value[0]] = value


def make_client(storage=None):
    client = BaseClient("update-guard", api_id=1, api_hash="x", in_memory=True)
    client.storage = storage or Storage()
    client.session = object()
    client.is_connected = True
    client.skip_updates = False
    client.no_updates = False
    client.takeout_id = None
    return client


def raw_channel(channel_id, *, min_peer=True):
    return raw.types.Channel(
        id=channel_id,
        title=f"channel-{channel_id}",
        photo=raw.types.ChatPhotoEmpty(),
        date=1,
        min=min_peer,
        broadcast=True,
        access_hash=channel_id,
        usernames=[],
        restriction_reason=[],
    )


def raw_group(chat_id):
    return raw.types.Chat(
        id=chat_id,
        title=f"group-{chat_id}",
        photo=raw.types.ChatPhotoEmpty(),
        participants_count=2,
        date=1,
        version=1,
    )


def raw_user(user_id):
    return raw.types.User(
        id=user_id,
        first_name=f"user-{user_id}",
        access_hash=user_id,
        bot=True,
        usernames=[],
        restriction_reason=[],
    )


def channel_message(channel_id, message_id, text="signed"):
    return raw.types.Message(
        id=message_id,
        peer_id=raw.types.PeerChannel(channel_id=channel_id),
        date=1,
        message=text,
        entities=[],
        restriction_reason=[],
    )


def private_message(user_id, message_id, text="signed"):
    return raw.types.Message(
        id=message_id,
        peer_id=raw.types.PeerUser(user_id=user_id),
        from_id=raw.types.PeerUser(user_id=user_id),
        date=1,
        message=text,
        entities=[],
        restriction_reason=[],
    )


def group_message(chat_id, from_id, message_id, text="signed"):
    return raw.types.Message(
        id=message_id,
        peer_id=raw.types.PeerChat(chat_id=chat_id),
        from_id=raw.types.PeerUser(user_id=from_id),
        date=1,
        message=text,
        entities=[],
        restriction_reason=[],
    )


def state(pts):
    return raw.types.updates.State(pts=pts, qts=0, date=pts, seq=pts, unread_count=0)


async def start_dispatcher(client, chat_ids, callback):
    client.dispatcher.groups = OrderedDict(
        {0: [MessageHandler(callback, filters.chat(chat_ids))]}
    )
    return asyncio.create_task(client.dispatcher.handler_worker(asyncio.Lock()))


async def stop_dispatcher(client, worker):
    client.dispatcher.updates_queue.put_nowait(None)
    await worker


async def wait_until(predicate, timeout=1.0):
    async def waiter():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(waiter(), timeout)


@pytest_asyncio.fixture(autouse=True)
async def guard_state():
    update_guard.install_update_guard()
    update_guard.reset_update_guard_state()
    yield
    update_guard.reset_update_guard_state()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_min_channel_cold_cache_reaches_real_signer_handler_without_rpc():
    channel_id = 123
    chat_id = utils.get_channel_id(channel_id)
    client = make_client()
    rpc_calls = []

    async def invoke(_self, query, *args, **kwargs):
        rpc_calls.append(type(query).__name__)
        raise AssertionError("cold min peer must not start enrichment RPC")

    client.invoke = MethodType(invoke, client)
    signer = object.__new__(core.UserSigner)
    signer.context = SimpleNamespace(
        sign_chats={chat_id: [SimpleNamespace(message_thread_id=None)]},
        chat_messages=defaultdict(dict),
    )
    signer.log = lambda *args, **kwargs: None
    worker = await start_dispatcher(client, [chat_id], signer.on_message)

    update = raw.types.UpdateNewChannelMessage(
        message=channel_message(channel_id, 77), pts=20, pts_count=1
    )
    await client.handle_updates(
        raw.types.Updates(
            updates=[update],
            users=[],
            chats=[raw_channel(channel_id)],
            date=1,
            seq=1,
        )
    )
    await wait_until(lambda: 77 in signer.context.chat_messages[chat_id])
    await stop_dispatcher(client, worker)

    parsed = signer.context.chat_messages[chat_id][77]
    assert (parsed.chat.id, parsed.chat.is_min, parsed.text) == (
        chat_id,
        True,
        "signed",
    )
    assert rpc_calls == []
    assert client.storage.states[-1][1] == 20


@pytest.mark.asyncio
async def test_cached_min_channel_enrichment_does_not_block_current_packet():
    channel_id = 124
    group_id = 125
    channel_chat_id = utils.get_channel_id(channel_id)
    group_chat_id = -group_id
    client = make_client(
        Storage(
            {
                channel_chat_id: raw.types.InputPeerChannel(
                    channel_id=channel_id, access_hash=channel_id
                )
            }
        )
    )
    enrichment_started = asyncio.Event()
    enrichment_cancelled = asyncio.Event()

    async def invoke(_self, query, *args, **kwargs):
        assert isinstance(query, raw.functions.updates.GetChannelDifference)
        enrichment_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            enrichment_cancelled.set()
            raise

    client.invoke = MethodType(invoke, client)
    signer = object.__new__(core.UserSigner)
    signer.context = SimpleNamespace(
        sign_chats={
            channel_chat_id: [SimpleNamespace(message_thread_id=None)],
            group_chat_id: [SimpleNamespace(message_thread_id=None)],
        },
        chat_messages=defaultdict(dict),
    )
    signer.log = lambda *args, **kwargs: None
    worker = await start_dispatcher(
        client, [channel_chat_id, group_chat_id], signer.on_message
    )

    await client.handle_updates(
        raw.types.Updates(
            updates=[
                raw.types.UpdateNewChannelMessage(
                    message=channel_message(channel_id, 77), pts=20, pts_count=1
                ),
                raw.types.UpdateNewMessage(
                    message=group_message(group_id, 42, 78), pts=21, pts_count=1
                ),
            ],
            users=[raw_user(42)],
            chats=[raw_channel(channel_id), raw_group(group_id)],
            date=1,
            seq=1,
        )
    )
    await asyncio.wait_for(enrichment_started.wait(), 0.5)
    await wait_until(
        lambda: (
            77 in signer.context.chat_messages[channel_chat_id]
            and 78 in signer.context.chat_messages[group_chat_id]
        )
    )

    guard = update_guard._client_state(client)
    assert len(guard.channel_tasks) == 1
    assert update_guard._update_rpc_active == 1
    await update_guard.close_update_guard(client)
    await asyncio.wait_for(enrichment_cancelled.wait(), 0.5)
    await stop_dispatcher(client, worker)
    assert guard.channel_tasks == set()
    assert update_guard._update_rpc_active == 0


@pytest.mark.asyncio
async def test_prestart_channel_close_releases_capacity_for_short_recovery():
    channel_calls = 0

    for channel_id in range(400, 404):
        client = make_client(
            Storage(
                {
                    utils.get_channel_id(channel_id): raw.types.InputPeerChannel(
                        channel_id=channel_id, access_hash=channel_id
                    )
                }
            )
        )

        async def channel_invoke(_self, query, *args, **kwargs):
            nonlocal channel_calls
            channel_calls += 1
            raise AssertionError("pre-start channel worker must not run")

        client.invoke = MethodType(channel_invoke, client)
        await client.handle_updates(
            raw.types.Updates(
                updates=[
                    raw.types.UpdateNewChannelMessage(
                        message=channel_message(channel_id, 1),
                        pts=1,
                        pts_count=1,
                    )
                ],
                users=[],
                chats=[raw_channel(channel_id)],
                date=1,
                seq=1,
            )
        )
        assert update_guard._update_rpc_active == 1
        await update_guard.close_update_guard(client)
        assert update_guard._update_rpc_active == 0

    assert channel_calls == 0

    short_client = make_client()
    short_started = asyncio.Event()

    async def short_invoke(_self, query, *args, **kwargs):
        assert isinstance(query, raw.functions.updates.GetDifference)
        short_started.set()
        return raw.types.updates.Difference(
            new_messages=[],
            new_encrypted_messages=[],
            other_updates=[],
            chats=[],
            users=[],
            state=state(1),
        )

    short_client.invoke = MethodType(short_invoke, short_client)
    await short_client.handle_updates(
        raw.types.UpdateShortMessage(
            id=1,
            user_id=1,
            message="short",
            pts=1,
            pts_count=1,
            date=1,
            entities=[],
        )
    )
    await asyncio.wait_for(short_started.wait(), 0.5)
    await wait_until(
        lambda: (
            update_guard._client_state(short_client).short_worker is None
            and update_guard._update_rpc_active == 0
        )
    )
    await update_guard.close_update_guard(short_client)


@pytest.mark.asyncio
async def test_reset_releases_old_prestart_lease_without_decrementing_new_slot():
    async def schedule_channel(channel_id):
        client = make_client(
            Storage(
                {
                    utils.get_channel_id(channel_id): raw.types.InputPeerChannel(
                        channel_id=channel_id, access_hash=channel_id
                    )
                }
            )
        )

        async def invoke(_self, query, *args, **kwargs):
            await asyncio.Event().wait()

        client.invoke = MethodType(invoke, client)
        await client.handle_updates(
            raw.types.Updates(
                updates=[
                    raw.types.UpdateNewChannelMessage(
                        message=channel_message(channel_id, 1),
                        pts=1,
                        pts_count=1,
                    )
                ],
                users=[],
                chats=[raw_channel(channel_id)],
                date=1,
                seq=1,
            )
        )
        return client

    old_client = await schedule_channel(410)
    old_state = update_guard._client_state(old_client)
    old_task = next(iter(old_state.channel_tasks))
    old_lease = old_state.channel_leases[old_task]
    assert update_guard._update_rpc_active == 1

    update_guard.reset_update_guard_state()
    assert old_lease.released is True
    assert next(iter(old_state.channels.values())).in_flight is False
    assert update_guard._update_rpc_active == 0

    new_client = await schedule_channel(411)
    assert update_guard._update_rpc_active == 1
    await asyncio.gather(old_task, return_exceptions=True)
    old_lease.release()
    assert update_guard._update_rpc_active == 1

    await update_guard.close_update_guard(new_client)
    assert update_guard._update_rpc_active == 0


@pytest.mark.asyncio
async def test_same_packet_timeout_keeps_all_updates_and_same_channel_singleflight():
    channel_id = 200
    group_id = 300
    channel_chat_id = utils.get_channel_id(channel_id)
    group_chat_id = -group_id
    storage = Storage(
        {
            channel_chat_id: raw.types.InputPeerChannel(
                channel_id=channel_id, access_hash=1
            )
        }
    )
    client = make_client(storage)
    calls = 0

    async def invoke(_self, query, *args, **kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("Request timed out")

    client.invoke = MethodType(invoke, client)
    seen = []

    async def callback(_client, message):
        seen.append((message.chat.id, message.id))

    worker = await start_dispatcher(client, [channel_chat_id, group_chat_id], callback)
    updates = raw.types.Updates(
        updates=[
            raw.types.UpdateNewChannelMessage(
                message=channel_message(channel_id, 1), pts=10, pts_count=1
            ),
            raw.types.UpdateNewChannelMessage(
                message=channel_message(channel_id, 2), pts=11, pts_count=1
            ),
            raw.types.UpdateNewMessage(
                message=group_message(group_id, 42, 3), pts=12, pts_count=1
            ),
        ],
        users=[raw_user(42)],
        chats=[raw_channel(channel_id), raw_group(group_id)],
        date=1,
        seq=1,
    )
    await client.handle_updates(updates)
    await wait_until(lambda: len(seen) == 3)
    await stop_dispatcher(client, worker)

    assert seen == [(channel_chat_id, 1), (channel_chat_id, 2), (group_chat_id, 3)]
    assert calls == 1
    assert [item[1] for item in storage.states] == [10, 11, 12]


@pytest.mark.asyncio
async def test_short_updates_coalesce_and_follow_up_when_response_does_not_cover_new_pts():
    user_id = 42
    chat_id = 77
    client = make_client()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = []

    async def invoke(_self, query, *args, **kwargs):
        calls.append(query.pts)
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
            return raw.types.updates.Difference(
                new_messages=[private_message(user_id, 1, "private")],
                new_encrypted_messages=[],
                other_updates=[],
                chats=[],
                users=[raw_user(user_id)],
                state=state(10),
            )
        return raw.types.updates.Difference(
            new_messages=[group_message(chat_id, user_id, 2, "group")],
            new_encrypted_messages=[],
            other_updates=[],
            chats=[raw_group(chat_id)],
            users=[raw_user(user_id)],
            state=state(20),
        )

    client.invoke = MethodType(invoke, client)
    seen = []

    async def callback(_client, message):
        seen.append((message.chat.id, message.id, message.text))

    worker = await start_dispatcher(client, [user_id, -chat_id], callback)
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=1,
            user_id=user_id,
            message="private",
            pts=10,
            pts_count=1,
            date=10,
            entities=[],
        )
    )
    await asyncio.wait_for(first_started.wait(), 0.5)
    await client.handle_updates(
        raw.types.UpdateShortChatMessage(
            id=2,
            from_id=user_id,
            chat_id=chat_id,
            message="group",
            pts=20,
            pts_count=1,
            date=20,
            entities=[],
        )
    )
    release_first.set()
    await wait_until(lambda: len(seen) == 2)
    await stop_dispatcher(client, worker)

    assert seen == [(user_id, 1, "private"), (-chat_id, 2, "group")]
    assert calls == [9, 10]
    assert [item[1] for item in client.storage.states] == [9, 10, 20]


@pytest.mark.asyncio
async def test_full_global_update_during_short_recovery_extends_gap(monkeypatch):
    user_id = 43
    client = make_client()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = []
    monkeypatch.setattr(update_guard, "_cooldown", lambda *args, **kwargs: 0.0)

    async def invoke(_self, query, *args, **kwargs):
        calls.append(query.pts)
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
            return raw.types.updates.Difference(
                new_messages=[private_message(user_id, 10, "short-10")],
                new_encrypted_messages=[],
                other_updates=[],
                chats=[],
                users=[raw_user(user_id)],
                state=state(10),
            )
        if len(calls) == 2:
            # A concurrent Session task delivered pts=20 locally, but this
            # response still confirms only pts=10. The target must remain pending.
            return raw.types.updates.Difference(
                new_messages=[],
                new_encrypted_messages=[],
                other_updates=[],
                chats=[],
                users=[],
                state=state(10),
            )
        return raw.types.updates.Difference(
            new_messages=[
                private_message(user_id, 11, "recovered-11"),
                private_message(user_id, 20, "full-20"),
            ],
            new_encrypted_messages=[],
            other_updates=[],
            chats=[],
            users=[raw_user(user_id)],
            state=state(20),
        )

    client.invoke = MethodType(invoke, client)
    seen = []

    async def callback(_client, message):
        seen.append(message.id)

    worker = await start_dispatcher(client, [user_id], callback)
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=10,
            user_id=user_id,
            message="short-10",
            pts=10,
            pts_count=1,
            date=10,
            entities=[],
        )
    )
    await asyncio.wait_for(first_started.wait(), 0.5)
    await client.handle_updates(
        raw.types.Updates(
            updates=[
                raw.types.UpdateNewMessage(
                    message=private_message(user_id, 20, "full-20"),
                    pts=20,
                    pts_count=1,
                )
            ],
            users=[raw_user(user_id)],
            chats=[],
            date=20,
            seq=20,
        )
    )
    release_first.set()
    await wait_until(
        lambda: (
            calls == [9, 10, 10]
            and update_guard._client_state(client).short_worker is None
        )
    )
    assert client.storage.state_rows[0][1] == 20

    await client.handle_updates(
        raw.types.Updates(
            updates=[
                raw.types.UpdateNewMessage(
                    message=private_message(user_id, 21, "full-21"),
                    pts=21,
                    pts_count=1,
                )
            ],
            users=[raw_user(user_id)],
            chats=[],
            date=21,
            seq=21,
        )
    )
    await wait_until(lambda: len(seen) == 4)
    await stop_dispatcher(client, worker)

    assert seen == [20, 10, 11, 21]
    assert client.storage.state_rows[0][1] == 21


@pytest.mark.asyncio
async def test_difference_empty_and_slice_retry_without_fake_success(monkeypatch):
    user_id = 55
    client = make_client()
    monkeypatch.setattr(update_guard, "_cooldown", lambda *args, **kwargs: 0.0)
    calls = 0

    async def invoke(_self, query, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return raw.types.updates.DifferenceTooLong(pts=9)
        if calls == 2:
            return raw.types.updates.DifferenceEmpty(date=1, seq=1)
        if calls == 3:
            return raw.types.updates.DifferenceSlice(
                new_messages=[private_message(user_id, 1, "slice")],
                new_encrypted_messages=[],
                other_updates=[],
                chats=[],
                users=[raw_user(user_id)],
                intermediate_state=state(10),
            )
        return raw.types.updates.Difference(
            new_messages=[private_message(user_id, 2, "final")],
            new_encrypted_messages=[],
            other_updates=[],
            chats=[],
            users=[raw_user(user_id)],
            state=state(11),
        )

    client.invoke = MethodType(invoke, client)
    seen = []

    async def callback(_client, message):
        seen.append((message.id, message.text))

    worker = await start_dispatcher(client, [user_id], callback)
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=1,
            user_id=user_id,
            message="short",
            pts=10,
            pts_count=1,
            date=10,
            entities=[],
        )
    )
    await wait_until(lambda: len(seen) == 2)
    await stop_dispatcher(client, worker)

    assert calls == 4
    assert seen == [(1, "slice"), (2, "final")]
    assert [item[1] for item in client.storage.states] == [9, 10, 11]


@pytest.mark.asyncio
async def test_close_cancels_worker_releases_capacity_and_blocks_late_rpc():
    client = make_client()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    async def invoke(_self, query, *args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    client.invoke = MethodType(invoke, client)
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=1,
            user_id=1,
            message="x",
            pts=1,
            pts_count=1,
            date=1,
            entities=[],
        )
    )
    await asyncio.wait_for(started.wait(), 0.5)
    await update_guard.close_update_guard(client)
    await asyncio.wait_for(cancelled.wait(), 0.5)

    assert update_guard._update_rpc_active == 0
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=2,
            user_id=1,
            message="late",
            pts=2,
            pts_count=1,
            date=2,
            entities=[],
        )
    )
    await asyncio.sleep(0)
    assert calls == 1


def test_install_does_not_patch_or_gate_normal_invoke():
    invoke = BaseClient.invoke
    update_guard.install_update_guard()
    assert BaseClient.invoke is invoke


@pytest.mark.asyncio
async def test_new_and_edit_of_same_message_id_are_both_dispatched():
    chat_id = 19
    client = make_client()
    seen = []

    async def on_new(_client, message):
        seen.append(("new", message.id, message.text))

    async def on_edit(_client, message):
        seen.append(("edit", message.id, message.text))

    client.dispatcher.groups = OrderedDict(
        {
            0: [
                MessageHandler(on_new, filters.chat([-chat_id])),
                EditedMessageHandler(on_edit, filters.chat([-chat_id])),
            ]
        }
    )
    worker = asyncio.create_task(client.dispatcher.handler_worker(asyncio.Lock()))
    await client.handle_updates(
        raw.types.Updates(
            updates=[
                raw.types.UpdateNewMessage(
                    message=group_message(chat_id, 1, 7, "before"),
                    pts=1,
                    pts_count=1,
                ),
                raw.types.UpdateEditMessage(
                    message=group_message(chat_id, 1, 7, "after"),
                    pts=2,
                    pts_count=1,
                ),
            ],
            users=[raw_user(1)],
            chats=[raw_group(chat_id)],
            date=1,
            seq=1,
        )
    )
    await wait_until(lambda: len(seen) == 2)
    await stop_dispatcher(client, worker)
    assert seen == [("new", 7, "before"), ("edit", 7, "after")]


@pytest.mark.asyncio
async def test_state_write_failure_does_not_drop_later_updates_in_same_packet():
    class FailingStorage(Storage):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def update_state(self, value=_NO_VALUE):
            if value is _NO_VALUE:
                return await super().update_state()
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("database is locked")
            await super().update_state(value)

    storage = FailingStorage()
    client = make_client(storage)
    seen = []

    async def callback(_client, message):
        seen.append(message.id)

    worker = await start_dispatcher(client, [-10], callback)
    await client.handle_updates(
        raw.types.Updates(
            updates=[
                raw.types.UpdateNewMessage(
                    message=group_message(10, 1, 1), pts=1, pts_count=1
                ),
                raw.types.UpdateNewMessage(
                    message=group_message(10, 1, 2), pts=2, pts_count=1
                ),
            ],
            users=[raw_user(1)],
            chats=[raw_group(10)],
            date=1,
            seq=1,
        )
    )
    await wait_until(lambda: seen == [1, 2])
    await stop_dispatcher(client, worker)

    assert storage.attempts == 2
    assert [item[1] for item in storage.states] == [2]


@pytest.mark.asyncio
async def test_pending_short_recovery_survives_disconnect_without_new_update():
    user_id = 88
    client = make_client()
    client.is_connected = False
    calls = 0

    async def invoke(_self, query, *args, **kwargs):
        nonlocal calls
        calls += 1
        return raw.types.updates.Difference(
            new_messages=[private_message(user_id, 1, "resumed")],
            new_encrypted_messages=[],
            other_updates=[],
            chats=[],
            users=[raw_user(user_id)],
            state=state(1),
        )

    client.invoke = MethodType(invoke, client)
    seen = []

    async def callback(_client, message):
        seen.append(message.text)

    worker = await start_dispatcher(client, [user_id], callback)
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=1,
            user_id=user_id,
            message="resumed",
            pts=1,
            pts_count=1,
            date=1,
            entities=[],
        )
    )
    await asyncio.sleep(0.02)
    assert calls == 0
    client.is_connected = True
    await wait_until(lambda: seen == ["resumed"], timeout=1.5)
    await stop_dispatcher(client, worker)
    assert calls == 1


@pytest.mark.asyncio
async def test_late_lower_pts_rolls_back_checkpoint_and_replays_without_duplicates():
    user_id = 91
    client = make_client()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = []
    stored_at_second_call = []

    async def invoke(_self, query, *args, **kwargs):
        calls.append(query.pts)
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
            return raw.types.updates.DifferenceSlice(
                new_messages=[private_message(user_id, 1, "one")],
                new_encrypted_messages=[],
                other_updates=[],
                chats=[],
                users=[raw_user(user_id)],
                intermediate_state=state(10),
            )
        stored_at_second_call.append(client.storage.state_rows[0][1])
        return raw.types.updates.Difference(
            new_messages=[
                private_message(user_id, 1, "one"),
                private_message(user_id, 2, "two"),
            ],
            new_encrypted_messages=[],
            other_updates=[],
            chats=[],
            users=[raw_user(user_id)],
            state=state(20),
        )

    client.invoke = MethodType(invoke, client)
    seen = []

    async def callback(_client, message):
        seen.append(message.id)

    worker = await start_dispatcher(client, [user_id], callback)
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=1,
            user_id=user_id,
            message="one",
            pts=10,
            pts_count=1,
            date=10,
            entities=[],
        )
    )
    await asyncio.wait_for(first_started.wait(), 0.5)
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=2,
            user_id=user_id,
            message="older",
            pts=5,
            pts_count=1,
            date=5,
            entities=[],
        )
    )
    release_first.set()
    await wait_until(lambda: seen == [1, 2])
    await stop_dispatcher(client, worker)

    assert calls == [9, 4]
    assert stored_at_second_call == [4]
    assert client.storage.state_rows[0][1] == 20


@pytest.mark.asyncio
async def test_slice_then_real_empty_finishes_at_confirmed_checkpoint():
    user_id = 92
    client = make_client()
    calls = []

    async def invoke(_self, query, *args, **kwargs):
        calls.append(query.pts)
        if len(calls) == 1:
            return raw.types.updates.DifferenceSlice(
                new_messages=[private_message(user_id, 1, "slice")],
                new_encrypted_messages=[],
                other_updates=[],
                chats=[],
                users=[raw_user(user_id)],
                intermediate_state=state(10),
            )
        return raw.types.updates.DifferenceEmpty(date=11, seq=11)

    client.invoke = MethodType(invoke, client)
    seen = []

    async def callback(_client, message):
        seen.append(message.id)

    worker = await start_dispatcher(client, [user_id], callback)
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=1,
            user_id=user_id,
            message="slice",
            pts=10,
            pts_count=1,
            date=10,
            entities=[],
        )
    )
    await wait_until(
        lambda: (
            calls == [9, 10] and update_guard._client_state(client).short_worker is None
        )
    )
    await wait_until(lambda: seen == [1])
    await stop_dispatcher(client, worker)

    guard = update_guard._client_state(client)
    assert guard.short_pending is None
    assert guard.short_in_flight is None
    assert client.storage.state_rows[0][1] == 10


@pytest.mark.asyncio
async def test_too_long_rebases_durable_cursor_and_empty_finishes(monkeypatch):
    client = make_client()
    calls = []
    monkeypatch.setattr(update_guard, "_cooldown", lambda *args, **kwargs: 0.0)

    async def invoke(_self, query, *args, **kwargs):
        calls.append(query.pts)
        if len(calls) == 1:
            return raw.types.updates.DifferenceTooLong(pts=20)
        return raw.types.updates.DifferenceEmpty(date=20, seq=20)

    client.invoke = MethodType(invoke, client)
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=1, user_id=1, message="old", pts=10, pts_count=1, date=10, entities=[]
        )
    )
    await wait_until(
        lambda: (
            calls == [9, 20] and update_guard._client_state(client).short_worker is None
        )
    )

    assert update_guard._client_state(client).short_pending is None
    assert client.storage.state_rows[0][1] == 20


@pytest.mark.asyncio
async def test_skip_updates_keeps_slice_checkpoint_in_memory_without_storage():
    class NoStateStorage(Storage):
        async def update_state(self, value=_NO_VALUE):
            raise AssertionError("skip_updates must not read or write update state")

    client = make_client(NoStateStorage())
    client.skip_updates = True
    calls = []

    async def invoke(_self, query, *args, **kwargs):
        calls.append(query.pts)
        if len(calls) == 1:
            return raw.types.updates.DifferenceSlice(
                new_messages=[private_message(96, 1, "slice")],
                new_encrypted_messages=[],
                other_updates=[],
                chats=[],
                users=[raw_user(96)],
                intermediate_state=state(10),
            )
        return raw.types.updates.DifferenceEmpty(date=10, seq=10)

    client.invoke = MethodType(invoke, client)
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=1, user_id=96, message="slice", pts=10, pts_count=1, date=10, entities=[]
        )
    )
    await wait_until(
        lambda: (
            calls == [9, 10] and update_guard._client_state(client).short_worker is None
        )
    )

    guard = update_guard._client_state(client)
    assert guard.short_pending is None
    assert guard.short_in_flight is None


@pytest.mark.asyncio
async def test_state_commit_failure_retries_and_deduplicates_delivered_message(
    monkeypatch,
):
    class CommitFailStorage(Storage):
        def __init__(self):
            super().__init__()
            self.failed = False

        async def update_state(self, value=_NO_VALUE):
            if (
                value is not _NO_VALUE
                and not isinstance(value, int)
                and value[0] == 0
                and value[1] == 10
                and not self.failed
            ):
                self.failed = True
                raise OSError("database is locked")
            return await super().update_state(value)

    monkeypatch.setattr(update_guard, "_cooldown", lambda *args, **kwargs: 0.0)
    storage = CommitFailStorage()
    client = make_client(storage)
    calls = 0

    async def invoke(_self, query, *args, **kwargs):
        nonlocal calls
        calls += 1
        return raw.types.updates.Difference(
            new_messages=[private_message(93, 1, "once")],
            new_encrypted_messages=[],
            other_updates=[],
            chats=[],
            users=[raw_user(93)],
            state=state(10),
        )

    client.invoke = MethodType(invoke, client)
    seen = []

    async def callback(_client, message):
        seen.append(message.id)

    worker = await start_dispatcher(client, [93], callback)
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=1, user_id=93, message="once", pts=10, pts_count=1, date=10, entities=[]
        )
    )
    await wait_until(lambda: calls == 2 and seen == [1])
    await stop_dispatcher(client, worker)

    assert storage.failed is True
    assert storage.state_rows[0][1] == 10


@pytest.mark.asyncio
async def test_full_and_difference_new_overlap_is_deduped_but_edit_is_kept():
    chat_id = 94
    client = make_client()
    seen = []

    async def on_new(_client, message):
        seen.append(("new", message.id, message.text))

    async def on_edit(_client, message):
        seen.append(("edit", message.id, message.text))

    client.dispatcher.groups = OrderedDict(
        {
            0: [
                MessageHandler(on_new, filters.chat([-chat_id])),
                EditedMessageHandler(on_edit, filters.chat([-chat_id])),
            ]
        }
    )
    worker = asyncio.create_task(client.dispatcher.handler_worker(asyncio.Lock()))
    original = group_message(chat_id, 1, 7, "before")
    await client.handle_updates(
        raw.types.Updates(
            updates=[raw.types.UpdateNewMessage(message=original, pts=1, pts_count=1)],
            users=[raw_user(1)],
            chats=[raw_group(chat_id)],
            date=1,
            seq=1,
        )
    )

    async def invoke(_self, query, *args, **kwargs):
        return raw.types.updates.Difference(
            new_messages=[group_message(chat_id, 1, 7, "before")],
            new_encrypted_messages=[],
            other_updates=[
                raw.types.UpdateEditMessage(
                    message=group_message(chat_id, 1, 7, "after"),
                    pts=2,
                    pts_count=1,
                )
            ],
            chats=[raw_group(chat_id)],
            users=[raw_user(1)],
            state=state(2),
        )

    client.invoke = MethodType(invoke, client)
    await client.handle_updates(
        raw.types.UpdateShortMessage(
            id=7, user_id=1, message="trigger", pts=2, pts_count=1, date=2, entities=[]
        )
    )
    await wait_until(lambda: len(seen) == 2)
    await stop_dispatcher(client, worker)
    assert seen == [("new", 7, "before"), ("edit", 7, "after")]


@pytest.mark.asyncio
async def test_update_rpc_cap_does_not_block_normal_invoke():
    release = asyncio.Event()
    active = 0
    maximum = 0
    update_calls = 0
    clients = []

    for channel_id in range(1000, 1006):
        storage = Storage(
            {
                utils.get_channel_id(channel_id): raw.types.InputPeerChannel(
                    channel_id=channel_id, access_hash=channel_id
                )
            }
        )
        client = make_client(storage)

        async def invoke(_self, query, *args, **kwargs):
            nonlocal active, maximum, update_calls
            if not isinstance(query, raw.functions.updates.GetChannelDifference):
                return "normal"
            update_calls += 1
            active += 1
            maximum = max(maximum, active)
            try:
                await release.wait()
                return raw.types.updates.ChannelDifferenceEmpty(
                    final=True, pts=1, timeout=None
                )
            finally:
                active -= 1

        client.invoke = MethodType(invoke, client)
        clients.append((client, channel_id))

    tasks = [
        asyncio.create_task(
            client.handle_updates(
                raw.types.Updates(
                    updates=[
                        raw.types.UpdateNewChannelMessage(
                            message=channel_message(channel_id, 1), pts=1, pts_count=1
                        )
                    ],
                    users=[],
                    chats=[raw_channel(channel_id)],
                    date=1,
                    seq=1,
                )
            )
        )
        for client, channel_id in clients
    ]
    await wait_until(lambda: active == update_guard._UPDATE_RPC_CONCURRENCY)
    assert await clients[0][0].invoke(object()) == "normal"
    release.set()
    await asyncio.gather(*tasks)
    await wait_until(lambda: update_guard._update_rpc_active == 0)

    assert maximum == update_guard._UPDATE_RPC_CONCURRENCY
    assert update_calls == update_guard._UPDATE_RPC_CONCURRENCY
    assert update_guard._update_rpc_active == 0


def test_channel_state_is_bounded_expires_and_floodwait_is_fully_respected(monkeypatch):
    state_obj = update_guard._ClientState()
    for channel_id in range(update_guard._MAX_CHANNEL_STATES + 20):
        update_guard._channel_state(state_obj, channel_id, float(channel_id))
    assert len(state_obj.channels) == update_guard._MAX_CHANNEL_STATES

    now = 10_000 + update_guard._STATE_TTL
    update_guard._channel_state(state_obj, 99999, now)
    assert len(state_obj.channels) == 1
    assert 99999 in state_obj.channels

    monkeypatch.setattr(update_guard.random, "uniform", lambda *_args: 0.0)
    assert update_guard._cooldown(1, update_guard.errors.FloodWait(37)) == 37.0


@pytest.mark.asyncio
async def test_persisted_baseline_recovers_on_replacement_client():
    storage = Storage()
    old_client = make_client(storage)
    old_client.is_connected = False
    await old_client.handle_updates(
        raw.types.UpdateShortMessage(
            id=1,
            user_id=95,
            message="recover",
            pts=10,
            pts_count=1,
            date=10,
            entities=[],
        )
    )
    assert storage.state_rows[0][1] == 9
    await update_guard.close_update_guard(old_client)

    new_client = make_client(storage)
    seen = []

    async def invoke(_self, query, *args, **kwargs):
        assert isinstance(query, raw.functions.updates.GetDifference)
        assert query.pts == 9
        return raw.types.updates.Difference(
            new_messages=[private_message(95, 1, "recover")],
            new_encrypted_messages=[],
            other_updates=[],
            chats=[],
            users=[raw_user(95)],
            state=state(10),
        )

    async def callback(_client, message):
        seen.append(message.text)

    new_client.invoke = MethodType(invoke, new_client)
    worker = await start_dispatcher(new_client, [95], callback)
    counts = await new_client.recover_gaps()
    await wait_until(lambda: seen == ["recover"])
    await stop_dispatcher(new_client, worker)

    assert counts == (1, 0)
    assert storage.state_rows[0][1] == 10
