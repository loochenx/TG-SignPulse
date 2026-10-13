"""Regression tests for account-scoped keyword monitor recovery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services import config, keyword_monitor
from tg_signer import core


class _Client:
    def __init__(self, *, start_error=None, health_errors=None):
        self.is_connected = True
        self.is_initialized = True
        self._tg_signpulse_no_updates = False
        self.start_error = start_error
        self.health_errors = list(health_errors or [])
        self.add_calls = 0
        self.remove_calls = 0
        self.enter_calls = 0
        self.exit_calls = 0

    def add_handler(self, _handler):
        self.add_calls += 1
        return (object(), 0)

    def remove_handler(self, *_handler_ref):
        self.remove_calls += 1

    async def __aenter__(self):
        self.enter_calls += 1
        if self.start_error is not None:
            raise self.start_error
        return self

    async def __aexit__(self, *_args):
        self.exit_calls += 1

    async def get_me(self):
        if self.health_errors:
            error = self.health_errors.pop(0)
            if error is not None:
                raise error
        return SimpleNamespace(id=1)


def _rule(account_name: str, chat_id: int):
    return keyword_monitor.KeywordMonitorRule(
        account_name=account_name,
        task_name=f"task-{account_name}",
        chat_id=chat_id,
        chat_name=f"chat-{account_name}",
        message_thread_id=None,
        action={"action": 8, "keywords": "ping"},
    )


def _prepare_service(monkeypatch, tmp_path, rules, clients):
    service = keyword_monitor.KeywordMonitorService()
    service._rules = rules
    service._load_rules = lambda: rules
    service._start_watchdog = lambda: None

    locks = {}

    def account_lock(name):
        if name not in locks:
            locks[name] = asyncio.Lock()
        return locks[name]

    monkeypatch.setattr(keyword_monitor, "get_account_lock", account_lock)
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
    client_cache = {}
    monkeypatch.setattr(core, "_CLIENT_INSTANCES", client_cache)

    async def close_cached_client(name, workdir):
        client_cache.pop(str(workdir.joinpath(name).resolve()), None)

    close_client = AsyncMock(side_effect=close_cached_client)
    monkeypatch.setattr(core, "close_client_by_name", close_client)

    requested = []

    def get_client(name, **_kwargs):
        requested.append(name)
        key = str(tmp_path.joinpath(name).resolve())
        if key in client_cache:
            return client_cache[key]
        queue = clients[name]
        return queue.pop(0)

    monkeypatch.setattr(core, "get_client", get_client)
    return service, requested, close_client, client_cache


@pytest.mark.asyncio
async def test_partial_start_retries_only_missing_account(monkeypatch, tmp_path):
    rules = [_rule("healthy", 101), _rule("missing", 202)]
    healthy = _Client()
    failed = _Client(start_error=ConnectionError("offline"))
    recovered = _Client()
    service, requested, close_client, _client_cache = _prepare_service(
        monkeypatch,
        tmp_path,
        rules,
        {"missing": [failed, recovered]},
    )
    healthy_ref = ("healthy", healthy, (object(), 0))
    service._handler_refs = [healthy_ref]

    await service.restart_from_tasks()

    assert service._handler_refs == [healthy_ref]
    assert healthy.remove_calls == 0
    assert healthy.exit_calls == 0
    assert failed.enter_calls == 1
    assert failed.remove_calls == 1
    assert service._active_key == ""
    close_client.assert_not_awaited()

    await service.restart_from_tasks()

    assert requested == ["missing", "missing"]
    assert healthy.remove_calls == 0
    assert healthy.exit_calls == 0
    assert recovered.enter_calls == 1
    assert {name for name, _client, _ref in service._handler_refs} == {
        "healthy",
        "missing",
    }
    assert service._active_key == service._rules_key(rules)


@pytest.mark.asyncio
async def test_health_failure_rebuilds_only_unresponsive_account(
    monkeypatch, tmp_path
):
    rules = [_rule("healthy", 101), _rule("broken", 202)]
    healthy = _Client()
    broken = _Client(
        health_errors=[asyncio.TimeoutError(), asyncio.TimeoutError()]
    )
    replacement = _Client()
    service, requested, close_client, client_cache = _prepare_service(
        monkeypatch,
        tmp_path,
        rules,
        {"broken": [replacement]},
    )
    healthy_ref = ("healthy", healthy, (object(), 0))
    broken_ref = ("broken", broken, (object(), 0))
    service._handler_refs = [healthy_ref, broken_ref]
    service._active_key = service._rules_key(rules)
    client_cache[str(tmp_path.joinpath("broken").resolve())] = broken

    await service._watchdog_tick()
    assert requested == []
    await service._watchdog_tick()

    assert requested == ["broken"]
    assert healthy.remove_calls == 0
    assert healthy.exit_calls == 0
    assert broken.remove_calls == 1
    assert broken.exit_calls == 1
    close_client.assert_awaited_once_with("broken", workdir=tmp_path)
    assert replacement.enter_calls == 1
    assert {name for name, _client, _ref in service._handler_refs} == {
        "healthy",
        "broken",
    }
    assert next(
        client for name, client, _ref in service._handler_refs if name == "healthy"
    ) is healthy
    assert next(
        client for name, client, _ref in service._handler_refs if name == "broken"
    ) is replacement
    assert service._active_key == service._rules_key(rules)



@pytest.mark.asyncio
async def test_watchdog_backs_off_failed_start_without_disrupting_healthy_account(
    monkeypatch, tmp_path
):
    now = [100.0]
    monkeypatch.setattr(keyword_monitor.time, "monotonic", lambda: now[0])
    rules = [_rule("healthy", 101), _rule("missing", 202)]
    healthy = _Client()
    service, requested, _close, _cache = _prepare_service(
        monkeypatch, tmp_path, rules,
        {"missing": [_Client(start_error=OSError("offline")),
                     _Client(start_error=OSError("offline")), _Client()]},
    )
    service._handler_refs = [("healthy", healthy, (object(), 0))]
    await service.restart_from_tasks()
    assert service._restart_retry_at["missing"] == 130.0
    await service._watchdog_tick()
    assert requested == ["missing"]
    now[0] = 130.0
    await service._watchdog_tick()
    assert requested == ["missing", "missing"]
    assert service._restart_retry_at["missing"] == 190.0
    now[0] = 160.0
    await service._watchdog_tick()
    assert requested == ["missing", "missing"]
    now[0] = 190.0
    await service._watchdog_tick()
    assert requested == ["missing", "missing", "missing"]
    assert not service._restart_retry_at
    assert not service._restart_failures
    assert healthy.remove_calls == healthy.exit_calls == 0


@pytest.mark.asyncio
async def test_watchdog_start_backoff_respects_full_flood_wait(monkeypatch, tmp_path):
    from pyrogram import errors

    now = [100.0]
    monkeypatch.setattr(keyword_monitor.time, "monotonic", lambda: now[0])
    rules = [_rule("limited", 101)]
    service, requested, _close, _cache = _prepare_service(
        monkeypatch, tmp_path, rules,
        {"limited": [_Client(start_error=errors.FloodWait(450)), _Client()]},
    )
    await service.restart_from_tasks()
    assert service._restart_retry_at["limited"] == 550.0
    now[0] = 400.0
    await service._watchdog_tick()
    assert requested == ["limited"]
    now[0] = 550.0
    await service._watchdog_tick()
    assert requested == ["limited", "limited"]
    assert not service._restart_retry_at


@pytest.mark.asyncio
async def test_connected_but_uninitialized_monitor_is_recreated(monkeypatch, tmp_path):
    rules = [_rule("broken", 101)]
    broken = _Client()
    broken.is_initialized = False
    broken.get_me = AsyncMock()
    replacement = _Client()
    service, requested, close, cache = _prepare_service(
        monkeypatch, tmp_path, rules, {"broken": [replacement]},
    )
    service._handler_refs = [("broken", broken, (object(), 0))]
    cache[str(tmp_path.joinpath("broken").resolve())] = broken
    assert not service._handlers_are_active_for(rules)
    await service._watchdog_tick()
    assert requested == ["broken"]
    close.assert_awaited_once_with("broken", workdir=tmp_path)
    broken.get_me.assert_not_awaited()
    assert service._handlers_are_active_for(rules)



@pytest.mark.asyncio
async def test_monitor_start_has_deadline_and_preserves_healthy_listener(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(keyword_monitor, "MONITOR_START_TIMEOUT", 0.01)
    rules = [_rule("healthy", 101), _rule("stuck", 202)]
    healthy = _Client()
    stuck = _Client()
    cancelled = asyncio.Event()

    async def start_forever():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    stuck.__aenter__ = start_forever
    service, requested, _close, _cache = _prepare_service(
        monkeypatch, tmp_path, rules, {"stuck": [stuck]},
    )
    healthy_ref = ("healthy", healthy, (object(), 0))
    service._handler_refs = [healthy_ref]
    await asyncio.wait_for(service.restart_from_tasks(), timeout=0.2)
    assert cancelled.is_set()
    assert requested == ["stuck"]
    assert service._handler_refs == [healthy_ref]
    assert healthy.exit_calls == healthy.remove_calls == 0
    assert stuck.remove_calls == 1
    assert service._restart_failures["stuck"] == 1
