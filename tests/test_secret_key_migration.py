"""Persistent JWT key migration and simultaneous startup regression tests."""

import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from jose import JWTError, jwt

from backend.core import config


def test_weak_file_migrates_once_and_invalidates_old_tokens(tmp_path, caplog):
    path = tmp_path / config.SECRET_KEY_FILENAME
    old = "short-secret"
    path.write_text(old)
    old_token = jwt.encode({"sub": "admin"}, old, algorithm="HS256")
    new = config.load_or_create_secret_key(tmp_path)
    assert len(new) >= 32
    assert new != old
    assert config.load_or_create_secret_key(tmp_path) == new
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert old not in caplog.text and new not in caplog.text
    with pytest.raises(JWTError):
        jwt.decode(old_token, new, algorithms=["HS256"])


@pytest.mark.parametrize("initial", [None, "weak"])
def test_parallel_processes_share_one_persistent_key(tmp_path, initial):
    path = tmp_path / config.SECRET_KEY_FILENAME
    if initial is not None:
        path.write_text(initial)
    program = (
        "from pathlib import Path; from backend.core.config import load_or_create_secret_key; "
        "import sys; print(load_or_create_secret_key(Path(sys.argv[1])))"
    )
    root = Path(__file__).resolve().parents[1]

    def start_process(_):
        result = subprocess.run(
            [sys.executable, "-c", program, str(tmp_path)],
            cwd=root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        return result.stdout.strip()

    with ThreadPoolExecutor(max_workers=6) as executor:
        keys = list(executor.map(start_process, range(6)))
    assert len(set(keys)) == 1
    assert len(keys[0]) >= 32
    assert path.read_text() == keys[0]
    assert {item.name for item in tmp_path.iterdir()} == {
        config.SECRET_KEY_FILENAME,
        ".secret_key.lock",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("supplied", ["", "short-secret", "  short-secret  "])
def test_missing_or_weak_environment_key_uses_persistent_key(
    monkeypatch, tmp_path, supplied
):
    monkeypatch.setattr(config, "Settings", lambda: config_settings)
    config_settings = type(
        "Config",
        (),
        {
            "secret_key": supplied,
            "resolve_base_dir": lambda self: tmp_path,
        },
    )()
    config.get_settings.cache_clear()
    try:
        first = config.get_settings().secret_key
        config_settings.secret_key = supplied
        config.get_settings.cache_clear()
        second = config.get_settings().secret_key
        assert first == second
        assert len(first) >= 32
        assert first == (tmp_path / config.SECRET_KEY_FILENAME).read_text()
    finally:
        config.get_settings.cache_clear()


def test_explicit_strong_environment_key_is_preserved(monkeypatch, tmp_path):
    value = "strong-environment-key-for-test"
    config_settings = type(
        "Config",
        (),
        {
            "secret_key": f" {value} ",
            "resolve_base_dir": lambda self: tmp_path,
        },
    )()
    monkeypatch.setattr(config, "Settings", lambda: config_settings)
    config.get_settings.cache_clear()
    try:
        assert config.get_settings().secret_key == value
        assert not list(tmp_path.iterdir())
    finally:
        config.get_settings.cache_clear()
