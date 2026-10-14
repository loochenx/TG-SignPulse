from __future__ import annotations

import fcntl
import logging
import os
import secrets
import tempfile
from functools import lru_cache
from pathlib import Path

from backend.utils.storage import get_initial_data_dir, get_writable_base_dir

try:
    from pydantic.v1 import BaseSettings
except ImportError:
    from pydantic import BaseSettings


SECRET_KEY_FILENAME = ".secret_key"
MIN_SECRET_KEY_LENGTH = 16

logger = logging.getLogger("backend.config")


def load_or_create_secret_key(base_dir: Path) -> str:
    """读取持久密钥；缺失或过短时安全生成，进程间共享同一结果。"""
    path = base_dir / SECRET_KEY_FILENAME

    def read_existing() -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    existing = read_existing()
    if len(existing) >= MIN_SECRET_KEY_LENGTH:
        path.chmod(0o600)
        return existing

    path.parent.mkdir(parents=True, exist_ok=True)
    # 锁单独的稳定文件：替换密钥文件后，并发启动进程仍锁同一 inode。
    lock_fd = os.open(path.with_name(".secret_key.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing = read_existing()
        if len(existing) >= MIN_SECRET_KEY_LENGTH:
            path.chmod(0o600)
            return existing

        key = secrets.token_urlsafe(48)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".secret_key.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                fp.write(key)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    logger.warning(
        "已生成随机登录密钥并保存到 %s；替换旧密钥后需重新登录面板",
        path,
    )
    return key


class Settings(BaseSettings):
    app_name: str = "tg-signer-panel"
    host: str = os.getenv("APP_HOST", "127.0.0.1")
    port: int = 3000

    # 留空时由 get_settings() 从数据目录读取或生成
    secret_key: str = ""
    access_token_expire_hours: int = 12

    timezone: str = os.getenv("TZ", "Asia/Hong_Kong")
    data_dir: Path = get_initial_data_dir()
    db_path: Path | None = None
    signer_workdir: Path | None = None
    session_dir: Path | None = None
    logs_dir: Path | None = None

    class Config:
        env_file = ".env"
        env_prefix = "APP_"
        case_sensitive = False

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.resolve_db_path()}?check_same_thread=False"

    def resolve_db_path(self) -> Path:
        return self.db_path or self.resolve_base_dir() / "db.sqlite"

    def resolve_workdir(self) -> Path:
        return self.signer_workdir or self.resolve_base_dir() / ".signer"

    def resolve_session_dir(self) -> Path:
        return self.session_dir or self.resolve_base_dir() / "sessions"

    def resolve_logs_dir(self) -> Path:
        return self.logs_dir or self.resolve_base_dir() / "logs"

    def resolve_base_dir(self) -> Path:
        if self.data_dir and str(self.data_dir) != "/data":
            return self.data_dir
        return get_writable_base_dir()


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.secret_key = settings.secret_key.strip()
    if len(settings.secret_key) < MIN_SECRET_KEY_LENGTH:
        if settings.secret_key:
            logger.warning("APP_SECRET_KEY 过短，改用数据目录中的随机登录密钥")
        settings.secret_key = load_or_create_secret_key(settings.resolve_base_dir())
    return settings
