"""Bound and recover Kurigram update enrichment without throttling business RPCs."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pyrogram import Client as BaseClient
from pyrogram import errors, raw, utils
from pyrogram.session import Session

logger = logging.getLogger("tg-signer")

_ORIGINAL_HANDLE_UPDATES = BaseClient.handle_updates
_INSTALLED = False

_UPDATE_RPC_CONCURRENCY = 4
_BASE_COOLDOWN = 2.0
_MAX_COOLDOWN = 60.0
_STATE_TTL = 60 * 60.0
_MAX_CHANNEL_STATES = 512
_SHORT_SLOT_RETRY = 0.25

_update_rpc_active = 0


@dataclass
class _ChannelState:
    in_flight: bool = False
    failures: int = 0
    retry_after: float = 0.0
    last_seen: float = 0.0
    log_after: float = 0.0


@dataclass
class _ShortSnapshot:
    pts: int
    date: int
    latest_pts: int
    latest_date: int


@dataclass
class _ClientState:
    channels: OrderedDict[int, _ChannelState] = field(default_factory=OrderedDict)
    short_pending: _ShortSnapshot | None = None
    short_in_flight: _ShortSnapshot | None = None
    short_worker: asyncio.Task | None = None
    short_failures: int = 0
    short_retry_after: float = 0.0
    short_log_after: float = 0.0
    baseline_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    update_seen: OrderedDict[tuple[str, int, int], None] = field(
        default_factory=OrderedDict
    )
    closed: bool = False
    active_tasks: set[asyncio.Task] = field(default_factory=set)


_client_states: weakref.WeakKeyDictionary[Any, _ClientState] = (
    weakref.WeakKeyDictionary()
)


def _client_state(client) -> _ClientState:
    state = _client_states.get(client)
    if state is None:
        state = _ClientState()
        _client_states[client] = state
    return state


def _transient(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            TimeoutError,
            OSError,
            ConnectionError,
            errors.FloodWait,
            errors.FloodPremiumWait,
            errors.InternalServerError,
            errors.ServiceUnavailable,
        ),
    )


def _cooldown(failures: int, exc: BaseException | None = None) -> float:
    delay = min(_MAX_COOLDOWN, _BASE_COOLDOWN * (2 ** min(failures - 1, 5)))
    if isinstance(exc, (errors.FloodWait, errors.FloodPremiumWait)):
        delay = max(delay, float(exc.value or 0))
    return delay + random.uniform(0.0, 1.0)


def _acquire_update_slot() -> bool:
    global _update_rpc_active
    if _update_rpc_active >= _UPDATE_RPC_CONCURRENCY:
        return False
    _update_rpc_active += 1
    return True


def _release_update_slot() -> None:
    global _update_rpc_active
    _update_rpc_active = max(0, _update_rpc_active - 1)


def _channel_state(state: _ClientState, channel_id: int, now: float) -> _ChannelState:
    for key, item in list(state.channels.items()):
        if (
            not item.in_flight
            and now >= item.retry_after
            and now - item.last_seen >= _STATE_TTL
        ):
            state.channels.pop(key, None)

    current = state.channels.get(channel_id)
    if current is not None:
        current.last_seen = now
        state.channels.move_to_end(channel_id)
        return current

    while len(state.channels) >= _MAX_CHANNEL_STATES:
        removable = next(
            (
                key
                for key, item in state.channels.items()
                if not item.in_flight and now >= item.retry_after
            ),
            None,
        )
        if removable is None:
            # All retained channels are active. This temporary state is not
            # inserted; the caller will deliver the raw update immediately.
            return _ChannelState(last_seen=now, retry_after=now + 1.0)
        state.channels.pop(removable, None)

    current = _ChannelState(last_seen=now)
    state.channels[channel_id] = current
    return current


async def _cached_channel_peer(client, channel_id: int):
    """Return a cached InputPeer without letting resolve_peer start another RPC."""
    return await client.storage.get_peer_by_id(utils.get_channel_id(channel_id))


async def _enrich_channel_update(client, update, users: dict, chats: dict) -> None:
    channel_id = getattr(getattr(update.message, "peer_id", None), "channel_id", None)
    if not channel_id:
        return

    client_state = _client_state(client)
    now = time.monotonic()
    state = _channel_state(client_state, int(channel_id), now)
    if state.in_flight or now < state.retry_after or not _acquire_update_slot():
        return

    state.in_flight = True
    try:
        try:
            peer = await _cached_channel_peer(client, int(channel_id))
        except KeyError:
            # A min peer is deliberately not persisted by Kurigram. Starting
            # GetChannels here recreates the unbounded task/RPC storm this
            # guard is intended to prevent. The raw packet is sufficient for
            # MessageHandler parsing and is delivered by the caller.
            state.retry_after = time.monotonic() + _BASE_COOLDOWN
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.failures = min(state.failures + 1, 8)
            state.retry_after = time.monotonic() + _cooldown(state.failures, exc)
            if time.monotonic() >= state.log_after:
                logger.warning(
                    "Skipping channel peer enrichment for %s after %s",
                    channel_id,
                    type(exc).__name__,
                )
                state.log_after = time.monotonic() + 60.0
            return

        query = raw.functions.updates.GetChannelDifference(
            channel=peer,
            filter=raw.types.ChannelMessagesFilter(
                ranges=[
                    raw.types.MessageRange(
                        min_id=update.message.id,
                        max_id=update.message.id,
                    )
                ]
            ),
            pts=update.pts - update.pts_count,
            limit=max(int(update.pts or 1), 1),
            force=False,
        )
        try:
            diff = await client.invoke(
                query,
                retries=0,
                timeout=Session.WAIT_TIMEOUT,
                sleep_threshold=0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.failures = min(state.failures + 1, 8)
            state.retry_after = time.monotonic() + _cooldown(state.failures, exc)
            if time.monotonic() >= state.log_after:
                log = logger.warning if _transient(exc) else logger.exception
                log(
                    "Channel update enrichment failed for %s: %s",
                    channel_id,
                    type(exc).__name__,
                )
                state.log_after = time.monotonic() + 60.0
            return

        state.failures = 0
        state.retry_after = 0.0
        state.log_after = 0.0
        if not isinstance(diff, raw.types.updates.ChannelDifferenceEmpty):
            users.update({item.id: item for item in getattr(diff, "users", [])})
            chats.update({item.id: item for item in getattr(diff, "chats", [])})
    finally:
        state.in_flight = False
        state.last_seen = time.monotonic()
        _release_update_slot()


async def _fetch_packet_peers(client, updates) -> bool:
    is_min = any(
        getattr(peer, "min", False) for peer in (*updates.users, *updates.chats)
    )
    try:
        await client.fetch_peers(updates.users)
        await client.fetch_peers(updates.chats)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Parsing uses the raw packet dictionaries below. A storage write
        # failure must not discard the message that could unblock a sign flow.
        logger.warning("Failed to persist update peers: %s", type(exc).__name__)
    return is_min


def _message_key(message) -> tuple[str, int, int]:
    peer = getattr(message, "peer_id", None)
    if isinstance(peer, raw.types.PeerChannel):
        peer_kind, peer_id = "channel", peer.channel_id
    elif isinstance(peer, raw.types.PeerChat):
        peer_kind, peer_id = "chat", peer.chat_id
    else:
        peer_kind, peer_id = "user", getattr(peer, "user_id", 0)
    return peer_kind, int(peer_id or 0), int(message.id)


def _accept_message(client, message) -> bool:
    state = _client_state(client)
    key = _message_key(message)
    if key in state.update_seen:
        state.update_seen.move_to_end(key)
        return False
    state.update_seen[key] = None
    while len(state.update_seen) > 4096:
        state.update_seen.popitem(last=False)
    return True


def _queue_update(client, update, users: dict, chats: dict) -> bool:
    """Queue one raw update, deduplicating only new-message deliveries."""
    if isinstance(
        update, (raw.types.UpdateNewMessage, raw.types.UpdateNewChannelMessage)
    ):
        message = getattr(update, "message", None)
        if message is not None and not _accept_message(client, message):
            return False
    client.dispatcher.updates_queue.put_nowait((update, users, chats))
    return True


async def _handle_full_updates(client, updates) -> None:
    client.last_update_time = datetime.now()
    is_min = await _fetch_packet_peers(client, updates)
    users = {item.id: item for item in updates.users}
    chats = {item.id: item for item in updates.chats}

    for update in updates.updates:
        channel_id = getattr(
            getattr(getattr(update, "message", None), "peer_id", None),
            "channel_id",
            None,
        ) or getattr(update, "channel_id", None)
        pts = getattr(update, "pts", None)

        if isinstance(update, raw.types.UpdateChannelTooLong):
            logger.info(update)

        if (
            isinstance(update, raw.types.UpdateNewChannelMessage)
            and is_min
            and not isinstance(update.message, raw.types.MessageEmpty)
        ):
            await _enrich_channel_update(client, update, users, chats)

        # Accept the message before advancing the persisted cursor. Queue.put_nowait
        # is atomic and the SDK dispatcher queue is intentionally unbounded.
        _queue_update(client, update, users, chats)
        client_state = _client_state(client)
        unresolved_global_gap = (
            client_state.short_pending is not None
            or client_state.short_in_flight is not None
        )
        if (
            pts
            and not client.skip_updates
            and not (channel_id is None and unresolved_global_gap)
        ):
            try:
                value = (
                    utils.get_channel_id(channel_id) if channel_id else 0,
                    pts,
                    None,
                    updates.date,
                    updates.seq,
                )
                if channel_id is None:
                    async with client_state.baseline_lock:
                        unresolved_global_gap = (
                            client_state.short_pending is not None
                            or client_state.short_in_flight is not None
                        )
                        if not unresolved_global_gap:
                            await client.storage.update_state(value)
                else:
                    await client.storage.update_state(value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Keep processing the packet. The old persisted cursor remains
                # the recovery baseline, so this failure must not drop later
                # messages from the same Updates container.
                logger.warning(
                    "Failed to persist update state at pts=%s: %s",
                    pts,
                    type(exc).__name__,
                )


def _merge_short(
    current: _ShortSnapshot | None, incoming: _ShortSnapshot
) -> _ShortSnapshot:
    if current is None:
        return incoming
    if incoming.pts < current.pts:
        pts, date = incoming.pts, incoming.date
    else:
        pts, date = current.pts, current.date
    if incoming.latest_pts > current.latest_pts:
        latest_pts, latest_date = incoming.latest_pts, incoming.latest_date
    else:
        latest_pts, latest_date = current.latest_pts, current.latest_date
    return _ShortSnapshot(pts, date, latest_pts, latest_date)


def _take_short(state: _ClientState) -> _ShortSnapshot | None:
    snapshot = state.short_pending
    state.short_pending = None
    return snapshot


def _put_short(state: _ClientState, snapshot: _ShortSnapshot) -> None:
    state.short_pending = _merge_short(state.short_pending, snapshot)


async def _global_storage_state(client):
    rows = await client.storage.update_state()
    return next((row for row in rows if row[0] == 0), None)


async def _persist_unresolved_baseline_locked(client, state: _ClientState) -> None:
    if client.skip_updates:
        return
    pending = state.short_pending
    in_flight = state.short_in_flight
    if pending is None and in_flight is None:
        return

    stored = await _global_storage_state(client)
    candidates = [item for item in (pending, in_flight) if item is not None]
    baseline = min(candidates, key=lambda item: item.pts)
    if stored is not None and int(stored[1]) < baseline.pts:
        stored_snapshot = _ShortSnapshot(
            pts=int(stored[1]),
            date=int(stored[3] or baseline.date),
            latest_pts=baseline.latest_pts,
            latest_date=baseline.latest_date,
        )
        if state.short_pending is None:
            state.short_pending = stored_snapshot
        else:
            state.short_pending = _merge_short(state.short_pending, stored_snapshot)
        baseline = stored_snapshot

    if stored is None or int(stored[1]) > baseline.pts:
        await client.storage.update_state((0, baseline.pts, None, baseline.date, None))


async def _persist_unresolved_baseline(client, state: _ClientState) -> None:
    """Persist the oldest global gap before any recovery RPC can run.

    Rolling a cursor backwards is intentional: a short update may arrive after
    a newer full packet was stored, and the older baseline is the only durable
    point from which a replacement client can recover the missing message.
    """
    async with state.baseline_lock:
        await _persist_unresolved_baseline_locked(client, state)


async def _commit_global_checkpoint(
    client,
    state: _ClientState,
    snapshot: _ShortSnapshot,
    pts: int,
    qts: int | None,
    date: int,
    seq: int | None,
) -> None:
    """Commit a server checkpoint without crossing an older unresolved gap."""
    if client.skip_updates:
        return
    async with state.baseline_lock:
        stored = await _global_storage_state(client)
        target_pts = int(pts)
        target_date = int(date)
        target_qts = qts
        target_seq = seq

        pending = state.short_pending
        if pending is not None and pending.pts < snapshot.pts:
            target_pts = pending.pts
            target_date = pending.date
            target_qts = None
            target_seq = None
        if stored is not None and int(stored[1]) < snapshot.pts:
            target_pts = min(target_pts, int(stored[1]))
            if target_pts == int(stored[1]):
                target_qts = stored[2]
                target_date = int(stored[3] or target_date)
                target_seq = stored[4]

        await client.storage.update_state(
            (0, target_pts, target_qts, target_date, target_seq)
        )


async def _commit_too_long_checkpoint(
    client,
    state: _ClientState,
    snapshot: _ShortSnapshot,
    server_pts: int,
) -> None:
    """Accept Telegram's mandatory rebase unless an older packet arrived."""
    if client.skip_updates:
        return
    async with state.baseline_lock:
        pending = state.short_pending
        if pending is not None and pending.pts < snapshot.pts:
            value = (0, pending.pts, None, pending.date, None)
        else:
            value = (0, int(server_pts), None, snapshot.latest_date, None)
        stored = await _global_storage_state(client)
        if stored is None or int(stored[1]) != value[1]:
            await client.storage.update_state(value)


def _difference_state(diff):
    if isinstance(diff, raw.types.updates.Difference):
        return diff.state
    if isinstance(diff, raw.types.updates.DifferenceSlice):
        return diff.intermediate_state
    return None


async def _dispatch_difference(client, diff, latest_pts: int) -> None:
    users = {item.id: item for item in getattr(diff, "users", [])}
    chats = {item.id: item for item in getattr(diff, "chats", [])}
    for message in getattr(diff, "new_messages", []):
        update_cls = (
            raw.types.UpdateNewChannelMessage
            if isinstance(getattr(message, "peer_id", None), raw.types.PeerChannel)
            else raw.types.UpdateNewMessage
        )
        _queue_update(
            client,
            update_cls(message=message, pts=latest_pts, pts_count=-1),
            users,
            chats,
        )
    for update in getattr(diff, "other_updates", []):
        _queue_update(client, update, users, chats)


async def _short_worker(client, state: _ClientState) -> None:
    try:
        while state.short_pending is not None:
            if not getattr(client, "is_connected", False):
                if state.closed:
                    return
                # Keep one bounded worker alive so the final pending sign reply
                # resumes after an SDK reconnect even if no newer update arrives.
                await asyncio.sleep(1.0)
                continue

            now = time.monotonic()
            if now < state.short_retry_after:
                await asyncio.sleep(min(state.short_retry_after - now, 1.0))
                continue

            try:
                await _persist_unresolved_baseline(client, state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.short_failures = min(state.short_failures + 1, 8)
                state.short_retry_after = time.monotonic() + _cooldown(
                    state.short_failures, exc
                )
                if time.monotonic() >= state.short_log_after:
                    logger.warning(
                        "Could not persist short update baseline; retrying after %s",
                        type(exc).__name__,
                    )
                    state.short_log_after = time.monotonic() + 60.0
                continue
            if not _acquire_update_slot():
                await asyncio.sleep(_SHORT_SLOT_RETRY)
                continue

            try:
                async with state.baseline_lock:
                    snapshot = _take_short(state)
                    if snapshot is not None:
                        state.short_in_flight = snapshot
            except BaseException:
                _release_update_slot()
                raise
            if snapshot is None:
                _release_update_slot()
                continue

            try:
                diff = await client.invoke(
                    raw.functions.updates.GetDifference(
                        pts=snapshot.pts,
                        date=snapshot.date,
                        qts=-1,
                    ),
                    retries=0,
                    timeout=Session.WAIT_TIMEOUT,
                    sleep_threshold=0,
                )
            except asyncio.CancelledError:
                _put_short(state, snapshot)
                state.short_in_flight = None
                raise
            except Exception as exc:
                _put_short(state, snapshot)
                state.short_in_flight = None
                state.short_failures = min(state.short_failures + 1, 8)
                state.short_retry_after = time.monotonic() + _cooldown(
                    state.short_failures, exc
                )
                if time.monotonic() >= state.short_log_after:
                    logger.warning(
                        "Short update recovery failed; retrying in %.1fs after %s",
                        max(0.0, state.short_retry_after - time.monotonic()),
                        type(exc).__name__,
                    )
                    state.short_log_after = time.monotonic() + 60.0
                continue
            finally:
                _release_update_slot()

            if isinstance(
                diff,
                (raw.types.updates.Difference, raw.types.updates.DifferenceSlice),
            ):
                try:
                    await _dispatch_difference(client, diff, snapshot.latest_pts)
                    response_state = _difference_state(diff)
                    await _commit_global_checkpoint(
                        client,
                        state,
                        snapshot,
                        response_state.pts,
                        response_state.qts,
                        response_state.date,
                        response_state.seq,
                    )
                except asyncio.CancelledError:
                    _put_short(state, snapshot)
                    state.short_in_flight = None
                    raise
                except Exception as exc:
                    _put_short(state, snapshot)
                    state.short_in_flight = None
                    state.short_failures = min(state.short_failures + 1, 8)
                    state.short_retry_after = time.monotonic() + _cooldown(
                        state.short_failures, exc
                    )
                    logger.warning(
                        "Short update dispatch/state commit failed after %s; retaining pts=%s",
                        type(exc).__name__,
                        snapshot.pts,
                    )
                    continue
                state.short_in_flight = None
                state.short_failures = 0
                state.short_retry_after = 0.0
                state.short_log_after = 0.0

                if state.short_pending is not None and response_state is not None:
                    if (
                        state.short_pending.pts >= snapshot.pts
                        and state.short_pending.latest_pts <= response_state.pts
                    ):
                        state.short_pending = None
                    elif state.short_pending.pts >= snapshot.pts:
                        # GetDifference must resume from the last confirmed
                        # server checkpoint. Jumping to the new short packet's
                        # pts-count could skip unseen updates in between.
                        state.short_pending.pts = response_state.pts
                        state.short_pending.date = response_state.date

                if isinstance(diff, raw.types.updates.DifferenceSlice):
                    _put_short(
                        state,
                        _ShortSnapshot(
                            response_state.pts,
                            response_state.date,
                            max(snapshot.latest_pts, response_state.pts),
                            max(snapshot.latest_date, response_state.date),
                        ),
                    )
                continue

            if isinstance(diff, raw.types.updates.DifferenceTooLong):
                try:
                    await _commit_too_long_checkpoint(client, state, snapshot, diff.pts)
                except asyncio.CancelledError:
                    _put_short(state, snapshot)
                    state.short_in_flight = None
                    raise
                except Exception as exc:
                    _put_short(state, snapshot)
                    state.short_in_flight = None
                    state.short_failures = min(state.short_failures + 1, 8)
                    state.short_retry_after = time.monotonic() + _cooldown(
                        state.short_failures, exc
                    )
                    logger.warning(
                        "Could not persist DifferenceTooLong checkpoint after %s",
                        type(exc).__name__,
                    )
                    continue
                state.short_in_flight = None
                logger.error(
                    "Short update gap is too long at pts=%s; continuing from server pts=%s",
                    snapshot.pts,
                    diff.pts,
                )
                _put_short(
                    state,
                    _ShortSnapshot(
                        diff.pts,
                        snapshot.latest_date,
                        max(snapshot.latest_pts, diff.pts),
                        snapshot.latest_date,
                    ),
                )
                continue

            if (
                isinstance(diff, raw.types.updates.DifferenceEmpty)
                and snapshot.pts >= snapshot.latest_pts
            ):
                try:
                    await _commit_global_checkpoint(
                        client,
                        state,
                        snapshot,
                        snapshot.pts,
                        None,
                        diff.date,
                        diff.seq,
                    )
                except asyncio.CancelledError:
                    _put_short(state, snapshot)
                    state.short_in_flight = None
                    raise
                except Exception as exc:
                    _put_short(state, snapshot)
                    state.short_in_flight = None
                    state.short_failures = min(state.short_failures + 1, 8)
                    state.short_retry_after = time.monotonic() + _cooldown(
                        state.short_failures, exc
                    )
                    continue

                state.short_in_flight = None
                state.short_failures = 0
                state.short_retry_after = 0.0
                if state.short_pending is not None:
                    if (
                        state.short_pending.pts >= snapshot.pts
                        and state.short_pending.latest_pts <= snapshot.pts
                    ):
                        state.short_pending = None
                    elif state.short_pending.pts >= snapshot.pts:
                        state.short_pending.pts = snapshot.pts
                        state.short_pending.date = int(diff.date)
                continue

            # An initial Empty cannot represent the short update that triggered
            # recovery. Retain the cursor instead of fabricating full success.
            _put_short(state, snapshot)
            state.short_in_flight = None
            state.short_failures = min(state.short_failures + 1, 8)
            state.short_retry_after = time.monotonic() + _cooldown(state.short_failures)
            if time.monotonic() >= state.short_log_after:
                logger.error(
                    "Short update recovery returned %s; retaining pts=%s for retry",
                    type(diff).__name__,
                    snapshot.pts,
                )
                state.short_log_after = time.monotonic() + 60.0
    finally:
        state.short_worker = None


def _worker_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.exception("Short update recovery worker crashed")


async def _queue_short_update(client, updates) -> None:
    state = _client_state(client)
    if state.closed:
        return
    snapshot = _ShortSnapshot(
        pts=int(updates.pts - updates.pts_count),
        date=int(updates.date),
        latest_pts=int(updates.pts),
        latest_date=int(updates.date),
    )
    try:
        async with state.baseline_lock:
            _put_short(state, snapshot)
            await _persist_unresolved_baseline_locked(client, state)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The worker retries persistence before it is allowed to issue an RPC.
        logger.warning(
            "Could not persist incoming short update baseline after %s",
            type(exc).__name__,
        )
    if state.short_worker is None or state.short_worker.done():
        state.short_worker = client.loop.create_task(_short_worker(client, state))
        state.short_worker.add_done_callback(_worker_done)


async def guarded_handle_updates(client, updates) -> None:
    state = _client_state(client)
    if state.closed:
        return
    client.last_update_time = datetime.now()
    task = asyncio.current_task()
    if task is not None:
        state.active_tasks.add(task)
    try:
        if isinstance(updates, (raw.types.Updates, raw.types.UpdatesCombined)):
            await _handle_full_updates(client, updates)
            return
        if isinstance(
            updates, (raw.types.UpdateShortMessage, raw.types.UpdateShortChatMessage)
        ):
            await _queue_short_update(client, updates)
            return
        await _ORIGINAL_HANDLE_UPDATES(client, updates)
    finally:
        if task is not None:
            state.active_tasks.discard(task)


def install_update_guard() -> None:
    """Install the update-only guard once for BaseClient and its subclasses."""
    global _INSTALLED
    if _INSTALLED:
        return
    BaseClient.handle_updates = guarded_handle_updates
    _INSTALLED = True


def uninstall_update_guard() -> None:
    """Restore the SDK method. Intended for isolated tests and clean shutdowns."""
    global _INSTALLED
    if not _INSTALLED:
        return
    BaseClient.handle_updates = _ORIGINAL_HANDLE_UPDATES
    _INSTALLED = False


def activate_update_guard(client) -> None:
    """Allow a reused client to receive updates after it has started again."""
    state = _client_state(client)
    state.closed = False
    if state.short_pending is not None and (
        state.short_worker is None or state.short_worker.done()
    ):
        state.short_worker = client.loop.create_task(_short_worker(client, state))
        state.short_worker.add_done_callback(_worker_done)


async def close_update_guard(client) -> None:
    """Stop and await all guard work owned by one client.

    Lifecycle code should call this before stopping the shared SDK client and
    call :func:`activate_update_guard` after a later successful start.
    """
    state = _client_state(client)
    state.closed = True
    current = asyncio.current_task()
    tasks = [task for task in state.active_tasks if task is not current]
    if state.short_worker is not None and state.short_worker is not current:
        tasks.append(state.short_worker)
    unique_tasks = list(dict.fromkeys(tasks))
    for task in unique_tasks:
        if not task.done():
            task.cancel()
    if unique_tasks:
        await asyncio.gather(*unique_tasks, return_exceptions=True)
    state.active_tasks.clear()
    state.short_worker = None
    try:
        await _persist_unresolved_baseline(client, state)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Could not persist pending update baseline during close after %s",
            type(exc).__name__,
        )
    state.channels.clear()


def reset_update_guard_state() -> None:
    """Cancel guard-owned workers and clear bounded state (test helper)."""
    global _update_rpc_active
    for state in list(_client_states.values()):
        if state.short_worker is not None and not state.short_worker.done():
            state.short_worker.cancel()
    _client_states.clear()
    _update_rpc_active = 0
