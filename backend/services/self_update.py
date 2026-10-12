"""页面内检查更新与一键更新。

- 检查更新：挂载了 Docker socket 时，比较镜像仓库里同一标签的摘要与本地镜像；
  否则比较 GitHub 上的最新版本标签与当前版本。
- 一键更新：面板拉取同一标签的新镜像，再用新镜像启动一次性的助手容器
  （backend.update_helper）替自己重建容器。只作用于面板自己的容器和镜像标签。
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from backend.utils.docker_api import (
    DockerClient,
    DockerError,
    docker_host,
    image_defaults,
    split_image_ref,
)

logger = logging.getLogger("backend.self_update")

CHECK_TTL_SECONDS = 6 * 3600
DEFAULT_REPO = "loochenx/TG-SignPulse"
UPDATER_LABEL = "io.tg-signpulse.updater-for"
_CONTAINER_ID_RE = re.compile(r"/containers/([0-9a-f]{64})/")
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
# 镜像仓库拒绝：镜像不存在（本地构建）或需要登录（私有镜像），面板都没法自己拉取
_REGISTRY_REFUSED = (401, 403, 404)


def _env_off(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("0", "false", "no", "off")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_version(value: str | None) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match((value or "").strip())
    return tuple(int(x) for x in match.groups()) if match else None  # type: ignore[return-value]


def latest_version_from_tags(tags: list[str]) -> str | None:
    """忽略预发布标签，取最大的 x.y.z"""
    versions = [(parsed, tag) for tag in tags if (parsed := parse_version(tag))]
    if not versions:
        return None
    return ".".join(str(x) for x in max(versions)[0])


def current_build() -> dict[str, str]:
    from tg_signer import __version__

    return {
        "version": __version__,
        "build_sha": os.getenv("BUILD_SHA", ""),
        "built_at": os.getenv("BUILD_DATE", ""),
    }


def guess_self_container_refs() -> list[str]:
    """面板所在容器的候选 ID：环境变量 > mountinfo 里的容器目录 > 主机名"""
    override = os.getenv("APP_UPDATE_CONTAINER", "").strip()
    if override:
        return [override]
    refs: list[str] = []
    try:
        for match in _CONTAINER_ID_RE.finditer(
            Path("/proc/self/mountinfo").read_text()
        ):
            if match.group(1) not in refs:
                refs.append(match.group(1))
    except OSError:
        pass
    hostname = socket.gethostname()
    if hostname and hostname not in refs:
        refs.append(hostname)
    return refs


def _mount_source(container: dict, destination: str) -> str | None:
    for mount in container.get("Mounts") or []:
        if mount.get("Destination") == destination:
            return mount.get("Source")
    return None


class UpdateUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class UpdateInProgress(Exception):
    pass


class SelfUpdateService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: dict[str, Any] = {"state": "idle", "message": None}
        self._check: dict[str, Any] | None = None
        self._checked_at_mono = 0.0
        self._last_result: dict[str, Any] | None = None
        # 已知镜像仓库里拉不到的镜像标签
        self._unpullable: str | None = None
        # (镜像 ID, 继承配置)：趁旧镜像还查得到时记下，重建容器时要用
        self._image_defaults: tuple[str, dict] | None = None

    # ── Docker 环境 ──

    def _docker(self) -> tuple[DockerClient | None, dict | None, str | None]:
        """返回 (客户端, 面板容器, 不可用的原因)；可用时调用方负责关闭客户端"""
        if _env_off("APP_SELF_UPDATE"):
            return None, None, "disabled"
        host = docker_host()
        sock = host[len("unix://") :] if host.startswith("unix://") else None
        if sock:
            if not os.path.exists(sock):
                return None, None, "no_socket"
            if not os.access(sock, os.R_OK | os.W_OK):
                return None, None, "permission"
        try:
            client = DockerClient(host)
        except ValueError:
            return None, None, "unreachable"
        try:
            client.ping()
            container = None
            for ref in guess_self_container_refs():
                try:
                    container = client.inspect_container(ref)
                    break
                except DockerError:
                    continue
        except (httpx.HTTPError, DockerError) as exc:
            logger.warning("连接 Docker 失败：%s", exc)
            client.close()
            return None, None, "unreachable"
        if container is None:
            client.close()
            return None, None, "not_container"
        image_ref = (container.get("Config") or {}).get("Image") or ""
        if not image_ref or image_ref.startswith("sha256:") or "@" in image_ref:
            client.close()
            return None, None, "pinned"
        return client, container, None

    # ── 检查更新 ──

    def _latest_github_version(self) -> str | None:
        repo = os.getenv("APP_UPDATE_REPO", DEFAULT_REPO)
        resp = httpx.get(
            f"https://api.github.com/repos/{repo}/tags",
            params={"per_page": 50},
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "tg-signpulse",
            },
            timeout=8,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return latest_version_from_tags([item.get("name", "") for item in resp.json()])

    @staticmethod
    def _release_image_ref(image_ref: str, version: str | None) -> str:
        """latest 优先解析为数字版本标签，避开镜像加速服务的旧 latest 缓存。"""
        repo, tag, digest = split_image_ref(image_ref)
        parsed = parse_version(version)
        if tag == "latest" and not digest and parsed:
            return f"{repo}:{'.'.join(str(part) for part in parsed)}"
        return image_ref

    def _run_check(self, client: DockerClient | None, container: dict | None) -> dict:
        current = current_build()
        result: dict[str, Any] = {
            "latest_version": None,
            "update_available": None,
            "local_build": False,
            "error": None,
            "checked_at": _now(),
        }
        try:
            result["latest_version"] = self._latest_github_version()
        except Exception as exc:
            logger.info("获取最新版本失败：%s", exc)

        newer = None
        latest = parse_version(result["latest_version"])
        installed = parse_version(current["version"])
        if latest and installed:
            newer = latest > installed

        if client and container:
            image_ref = container["Config"]["Image"]
            # 当前版本更高或暂时读不到 GitHub 时，用当前数字版本核对，避免
            # 旧 latest 缓存被误认为新镜像，导致再次更新甚至降级。
            release_version = result["latest_version"]
            if not latest or (installed and latest < installed):
                release_version = current["version"]
            result["pull_ref"] = self._release_image_ref(image_ref, release_version)
            try:
                try:
                    try:
                        remote = client.remote_digest(result["pull_ref"])
                    except DockerError as exc:
                        if exc.status != 404 or result["pull_ref"] == image_ref:
                            raise
                        # 自定义镜像可能没有数字标签；发布标签先于镜像就绪时，
                        # 同样回退到原标签，以实际镜像摘要决定是否可以安装。
                        result["pull_ref"] = image_ref
                        remote = client.remote_digest(image_ref)
                    self._unpullable = None
                except DockerError as exc:
                    if exc.status in _REGISTRY_REFUSED:
                        # 本地构建的镜像在新版 Docker 里也可能带摘要，要以仓库的回应为准
                        self._unpullable = image_ref
                    raise
                image = self._inspect_own_image(client, container)
                local_digests = {
                    d.split("@", 1)[1]
                    for d in (image or {}).get("RepoDigests") or []
                    if "@" in d
                }
                if image is None:
                    # 标签已经指向别的镜像（新镜像已下载，容器还没重建）
                    result["update_available"] = True
                elif local_digests:
                    result["update_available"] = remote not in local_digests
                else:
                    # 本地构建的镜像没有仓库摘要，只能按版本号判断
                    result["local_build"] = True
                    result["update_available"] = newer
            except (httpx.HTTPError, DockerError, KeyError) as exc:
                result["error"] = str(exc) or exc.__class__.__name__
                result["update_available"] = newer
        else:
            result["update_available"] = newer
            if result["latest_version"] is None:
                result["error"] = "version_unknown"
        return result

    def _inspect_own_image(self, client: DockerClient, container: dict) -> dict | None:
        """查询面板容器正在用的镜像，并记下它的继承配置；镜像已查不到时返回 None"""
        image_id = container["Image"]
        try:
            image = client.inspect_image(image_id)
        except DockerError as exc:
            if exc.status == 404:
                return None
            raise
        with self._lock:
            self._image_defaults = (image_id, image_defaults(image))
        return image

    def _old_image_defaults(self, client: DockerClient, container: dict) -> dict | None:
        try:
            self._inspect_own_image(client, container)
        except (httpx.HTTPError, DockerError):
            pass
        with self._lock:
            saved = self._image_defaults
        return saved[1] if saved and saved[0] == container["Image"] else None

    def _collect_helper_result(self, client: DockerClient, container: dict) -> None:
        """读取上一次助手容器的结果，然后删掉它"""
        name = container["Name"].lstrip("/")
        try:
            helpers = client.list_containers(f"{UPDATER_LABEL}={name}")
        except (httpx.HTTPError, DockerError):
            return
        for helper in helpers:
            if helper.get("State") in ("created", "running", "restarting"):
                continue
            try:
                detail = client.inspect_container(helper["Id"])
                logs = client.logs(helper["Id"], tail=12).strip().splitlines()
                client.remove_container(helper["Id"], force=True)
            except (httpx.HTTPError, DockerError):
                continue
            state = detail.get("State") or {}
            with self._lock:
                self._last_result = {
                    "ok": state.get("ExitCode") == 0,
                    "finished_at": state.get("FinishedAt"),
                    "message": logs[-1] if logs else None,
                    "log": logs,
                }

    def status(self, refresh: bool = False) -> dict[str, Any]:
        client, container, reason = self._docker()
        try:
            if client and container:
                self._collect_helper_result(client, container)
                if not self._image_defaults:
                    # 进入面板时就记下当前镜像的配置，免得之后标签被移走
                    self._old_image_defaults(client, container)
            stale = time.monotonic() - self._checked_at_mono > CHECK_TTL_SECONDS
            if _env_off("APP_UPDATE_CHECK"):
                check = None
            elif refresh or self._check is None or stale:
                check = self._run_check(client, container)
                with self._lock:
                    self._check = check
                    self._checked_at_mono = time.monotonic()
            else:
                check = self._check
        finally:
            if client:
                client.close()

        image_ref = container["Config"]["Image"] if container else None
        if not reason and image_ref and self._unpullable == image_ref:
            reason = "not_pullable"
        with self._lock:
            job = dict(self._job)
            last_result = self._last_result
        repo = os.getenv("APP_UPDATE_REPO", DEFAULT_REPO)
        return {
            "current": current_build(),
            "check_enabled": not _env_off("APP_UPDATE_CHECK"),
            "latest_version": check and check["latest_version"],
            "update_available": check and check["update_available"],
            "local_build": bool(check and check["local_build"]),
            "checked_at": check and check["checked_at"],
            "check_error": check and check["error"],
            "changes_url": f"https://github.com/{repo}/commits/main",
            "docker": {
                "enabled": reason is None,
                "reason": reason,
                "image": image_ref,
                "container": container["Name"].lstrip("/") if container else None,
            },
            "job": job,
            "last_result": last_result,
        }

    # ── 一键更新 ──

    def apply(self) -> dict[str, Any]:
        with self._lock:
            if self._job["state"] in ("pulling", "restarting"):
                raise UpdateInProgress()
            self._job = {
                "state": "pulling",
                "message": None,
                "code": None,
                "started_at": _now(),
            }
        client, container, reason = self._docker()
        if not reason and self._unpullable == container["Config"]["Image"]:
            client.close()
            reason = "not_pullable"
        if reason:
            with self._lock:
                self._job = {"state": "idle", "message": None}
            raise UpdateUnavailable(reason)
        threading.Thread(
            target=self._run_apply,
            args=(client, container),
            name="self-update",
            daemon=True,
        ).start()
        with self._lock:
            return dict(self._job)

    def _set_job(
        self, state: str, message: str | None = None, code: str | None = None
    ) -> None:
        with self._lock:
            self._job = {**self._job, "state": state, "message": message, "code": code}
            if state not in ("pulling", "restarting"):
                self._job["finished_at"] = _now()

    def _run_apply(self, client: DockerClient, container: dict) -> None:
        image_ref = container["Config"]["Image"]
        try:
            old_image = self._old_image_defaults(client, container)
            with self._lock:
                check = self._check
                stale = time.monotonic() - self._checked_at_mono > CHECK_TTL_SECONDS
            if not _env_off("APP_UPDATE_CHECK") and (check is None or stale):
                check = self._run_check(client, container)
            pull_ref = (check or {}).get("pull_ref") or image_ref
            logger.info("一键更新：拉取 %s", pull_ref)
            try:
                client.pull(pull_ref)
            except DockerError as exc:
                if exc.status not in _REGISTRY_REFUSED:
                    raise
                logger.warning("一键更新：镜像仓库拒绝拉取 %s：%s", image_ref, exc)
                self._unpullable = image_ref
                self._set_job("failed", str(exc), code="not_pullable")
                return
            new_image = client.inspect_image(pull_ref)
            labels = (new_image.get("Config") or {}).get("Labels") or {}
            downloaded = parse_version(labels.get("org.opencontainers.image.version"))
            installed = parse_version(current_build()["version"])
            if downloaded and installed and downloaded < installed:
                # 禁用检查更新或仓库回退时，也不能把旧缓存安装为新版本。
                client.tag_image(container["Image"], image_ref)
                self._set_job(
                    "failed",
                    "镜像仓库返回了较旧版本，请稍后重新检查",
                    code="older_version",
                )
                return
            if pull_ref != image_ref:
                # 更新助手与 compose 都继续使用原标签，确保重启仍指向新镜像，
                # 后续更新也能继续选择新的数字版本。
                client.tag_image(new_image["Id"], image_ref)
            if new_image["Id"] == container["Image"]:
                logger.info("一键更新：镜像没有变化")
                with self._lock:
                    if self._check:
                        self._check = {**self._check, "update_available": False}
                self._set_job("up_to_date")
                return
            self._set_job("restarting")
            helper_id = self._spawn_helper(client, container, image_ref, old_image)
            logger.info("一键更新：助手容器 %s 已启动，面板即将重启", helper_id[:12])
        except Exception as exc:
            logger.exception("一键更新失败")
            self._set_job("failed", str(exc) or exc.__class__.__name__)
        finally:
            client.close()

    def _spawn_helper(
        self,
        client: DockerClient,
        container: dict,
        image_ref: str,
        old_image: dict | None = None,
    ) -> str:
        name = container["Name"].lstrip("/")
        helper_name = f"{name}-updater"
        try:
            client.remove_container(helper_name, force=True)
        except DockerError as exc:
            if exc.status != 404:
                raise

        env = [
            "PYTHONUNBUFFERED=1",
            f"APP_UPDATE_CLEANUP={'0' if _env_off('APP_UPDATE_CLEANUP') else '1'}",
        ]
        if old_image:
            env.append(f"UPDATE_OLD_IMAGE_CONFIG={json.dumps(old_image)}")
        host_config: dict[str, Any] = {
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m"}},
            # 面板能访问 socket 的身份，助手也用同一个
            "GroupAdd": [str(gid) for gid in os.getgroups()],
        }
        if client.socket_path:
            inner = "/var/run/docker.sock"
            source = _mount_source(container, client.socket_path) or client.socket_path
            host_config["Binds"] = [f"{source}:{inner}"]
            host_config["NetworkMode"] = "none"
            env.append(f"DOCKER_HOST=unix://{inner}")
        else:
            # 通过 socket 代理访问 Docker 时，助手要和面板在同一网络里
            host_config["NetworkMode"] = (container.get("HostConfig") or {}).get(
                "NetworkMode", "default"
            )
            env.append(f"DOCKER_HOST={client.host}")

        body = {
            "Image": image_ref,
            "Entrypoint": ["python", "-m", "backend.update_helper"],
            "Cmd": [container["Id"], image_ref],
            "User": f"{os.getuid()}:{os.getgid()}",
            "WorkingDir": "/app",
            "Env": env,
            "Labels": {UPDATER_LABEL: name},
            # 镜像自带的健康检查是给面板用的，助手不需要
            "Healthcheck": {"Test": ["NONE"]},
            "HostConfig": host_config,
        }
        helper_id = client.create_container(body, helper_name)
        client.start(helper_id)
        return helper_id


_service: SelfUpdateService | None = None


def get_self_update_service() -> SelfUpdateService:
    global _service
    if _service is None:
        _service = SelfUpdateService()
    return _service
