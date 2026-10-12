from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Monkeypatch sqlite3.connect to increase default timeout
_original_sqlite3_connect = sqlite3.connect


def _patched_sqlite3_connect(*args, **kwargs):
    # Force timeout to be at least 10 seconds, even if Pyrogram sets it to 1
    if "timeout" in kwargs:
        kwargs["timeout"] = max(kwargs["timeout"], 10)
    else:
        kwargs["timeout"] = 30
    return _original_sqlite3_connect(*args, **kwargs)


sqlite3.connect = _patched_sqlite3_connect

from backend.api import router as api_router
from backend.core.config import get_settings
from backend.core.database import (
    Base,
    get_engine,
    get_session_local,
    init_engine,
)
from backend.scheduler import (
    init_scheduler,
    shutdown_scheduler,
    sync_jobs,
)
from backend.services.users import ensure_admin
from backend.utils.paths import ensure_data_dirs

# ---------------------------------------------------------------------------
# Unified logging configuration
# ---------------------------------------------------------------------------
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"
_LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    datefmt=_LOG_DATE_FMT,
)

# Silence noisy third-party loggers
for _noisy in ("pyrogram", "apscheduler", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/health" not in msg and "/healthz" not in msg and "/readyz" not in msg


logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

settings = get_settings()

app = FastAPI(title=settings.app_name, version="0.1.0")
app.state.ready = False

app.add_middleware(GZipMiddleware, minimum_size=1000)

# 面板与 API 同源部署，默认不开放跨域；需要时用 APP_CORS_ORIGINS 配置逗号分隔的白名单
_cors_origins = [
    origin.strip()
    for origin in os.getenv("APP_CORS_ORIGINS", "").split(",")
    if origin.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# API 路由必须在静态文件挂载之前注册，并使用 /api 前缀
app.include_router(api_router, prefix="/api")


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz")
def health_checkz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/version")
def get_version() -> dict[str, str]:
    from tg_signer import __version__

    # BUILD_DATE and BUILD_SHA are injected at Docker build time via ARG→ENV.
    # Falls back to git log for local dev (when .git is present).
    built_at = os.environ.get("BUILD_DATE", "")

    if not built_at:
        import shutil
        import subprocess

        if shutil.which("git"):
            try:
                built_at = subprocess.check_output(
                    ["git", "log", "-1", "--format=%ci"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip()[:16]
            except Exception:
                pass

    return {"version": __version__, "built_at": built_at}


@app.get("/readyz")
def ready_check(response: Response) -> dict[str, str]:
    if app.state.ready:
        return {"status": "ready"}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "starting"}


# 静态前端托管（Mode A: 单容器，FastAPI 提供静态文件）
# 仅在 /web/_next 目录存在时挂载（Docker 环境），本地开发时跳过
WEB_DIR = Path("/web").resolve()


class _ImmutableStaticFiles(StaticFiles):
    """Next.js 构建产物文件名带内容哈希，可安全地长期缓存"""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


if (WEB_DIR / "_next").exists():
    app.mount(
        "/_next",
        _ImmutableStaticFiles(directory=str(WEB_DIR / "_next")),
        name="nextjs_static",
    )


def _safe_web_file(relative: str) -> Path | None:
    """解析 /web 下的文件，拒绝越出 /web 目录的路径"""
    try:
        candidate = (WEB_DIR / relative).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(WEB_DIR) or not candidate.is_file():
        return None
    return candidate


def _html_response(path: Path) -> FileResponse:
    # HTML 入口不缓存，确保发版后浏览器能拿到新的资源引用
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


# Catch-all 路由：处理所有前端路由，返回 index.html
@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """SPA fallback: 对于所有非 API 路由，返回对应静态文件或 index.html"""
    if full_path.startswith("api/"):
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    file_path = _safe_web_file(full_path) if full_path else None
    if file_path is not None:
        if file_path.suffix == ".html":
            return _html_response(file_path)
        return FileResponse(file_path)

    # Next.js 静态导出会生成 xxx.html
    html_path = _safe_web_file(f"{full_path}.html") if full_path else None
    if html_path is not None:
        return _html_response(html_path)

    index_path = _safe_web_file("index.html")
    if index_path is not None:
        return _html_response(index_path)

    return {"detail": "Frontend not built"}


@app.on_event("startup")
async def on_startup() -> None:
    ensure_data_dirs(settings)
    init_engine()
    Base.metadata.create_all(bind=get_engine())
    with get_session_local()() as db:
        ensure_admin(db)
    await init_scheduler(sync_on_startup=False)

    async def _post_startup() -> None:
        try:
            await sync_jobs()
            from backend.services.keyword_monitor import get_keyword_monitor_service

            await get_keyword_monitor_service().restart_from_tasks()
        except Exception as exc:
            logging.getLogger("backend.startup").error(
                f"Delayed scheduler sync failed: {exc}"
            )
        finally:
            app.state.ready = True

    asyncio.create_task(_post_startup())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    shutdown_scheduler()
    try:
        from backend.services.keyword_monitor import get_keyword_monitor_service

        await get_keyword_monitor_service().stop()
    except Exception:
        pass
