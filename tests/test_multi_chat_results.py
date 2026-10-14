"""Regression tests for multi-chat task result aggregation."""

from __future__ import annotations

import json
from types import MethodType, SimpleNamespace

import pytest

from backend.services.sign_tasks import SignTaskService
from tg_signer import core


class _ManagedApp:
    _tg_signpulse_retired = False

    def __init__(self):
        self.refs = 0

    async def __aenter__(self):
        self.refs += 1
        return self

    async def __aexit__(self, *_args):
        self.refs -= 1


class _Signer(core.UserSigner):
    @property
    def sign_record_file(self):
        return self._record_file


def _signer(tmp_path, chats):
    signer = object.__new__(_Signer)
    signer._record_file = tmp_path / "sign_record.json"
    signer.app = _ManagedApp()
    signer.user = SimpleNamespace(id=1)
    signer.context = signer.ensure_ctx()
    signer.log = lambda *_args, **_kwargs: None
    signer.load_config = lambda *_args: SimpleNamespace(
        requires_ai=False,
        requires_updates=False,
        chats=chats,
        sign_interval=0,
        random_seconds=0,
        sign_at="0 8 * * *",
    )
    return signer


@pytest.mark.asyncio
async def test_partial_failure_is_retryable_and_does_not_write_daily_success(tmp_path):
    chats = [
        SimpleNamespace(chat_id=1, message_thread_id=None),
        SimpleNamespace(chat_id=2, message_thread_id=None),
    ]
    signer = _signer(tmp_path, chats)
    attempts = []
    second_chat_fails = True

    async def sign_a_chat(_self, chat):
        nonlocal second_chat_fails
        attempts.append(chat.chat_id)
        if chat.chat_id == 2 and second_chat_fails:
            raise OSError("request timed out")

    signer.sign_a_chat = MethodType(sign_a_chat, signer)

    with pytest.raises(OSError, match=r"1/2.*chat_id=2.*request timed out") as exc:
        await signer.run_once(20)

    assert signer.app.refs == 0
    assert json.loads(signer.sign_record_file.read_text(encoding="utf-8")) == {}
    assert SignTaskService._is_transient_network_error(exc.value) is True

    second_chat_fails = False
    await signer.run_once(20)

    assert attempts == [1, 2, 1, 2]
    assert signer.app.refs == 0
    record = json.loads(signer.sign_record_file.read_text(encoding="utf-8"))
    assert list(record) == [str(core.get_now().date())]


@pytest.mark.asyncio
async def test_all_chats_are_attempted_and_all_failures_are_reported(tmp_path):
    chats = [
        SimpleNamespace(chat_id=1, message_thread_id=None),
        SimpleNamespace(chat_id=2, message_thread_id=None),
        SimpleNamespace(chat_id=3, message_thread_id=None),
    ]
    signer = _signer(tmp_path, chats)
    attempts = []

    async def sign_a_chat(_self, chat):
        attempts.append(chat.chat_id)
        if chat.chat_id != 2:
            raise RuntimeError(f"chat {chat.chat_id} failed")

    signer.sign_a_chat = MethodType(sign_a_chat, signer)

    with pytest.raises(RuntimeError) as exc:
        await signer.run_once(20)

    assert attempts == [1, 2, 3]
    assert "2/3 个会话执行失败" in str(exc.value)
    assert "chat_id=1" in str(exc.value)
    assert "chat_id=3" in str(exc.value)
    assert json.loads(signer.sign_record_file.read_text(encoding="utf-8")) == {}
