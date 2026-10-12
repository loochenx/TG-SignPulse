"""最小化的 Docker Engine API 客户端。

只依赖 httpx 与标准库：面板进程和一次性的更新助手容器（backend.update_helper）共用。
请求不带 API 版本前缀，由 Docker 使用自己支持的最新版本；这里用到的字段在各版本间保持一致。
"""

from __future__ import annotations

import copy
import json
import os
import struct
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_SOCKET = "/var/run/docker.sock"

# 这些字段在容器配置里通常是从镜像继承来的；与旧镜像相同就丢掉，让新镜像的值生效
_IMAGE_DEFAULT_KEYS = (
    "Cmd",
    "Entrypoint",
    "WorkingDir",
    "User",
    "Healthcheck",
    "StopSignal",
    "Shell",
    "ExposedPorts",
    "Volumes",
    "OnBuild",
)
_BUILTIN_NETWORKS = ("bridge", "host", "none")


class DockerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def docker_host() -> str:
    return os.getenv("DOCKER_HOST") or f"unix://{DEFAULT_SOCKET}"


def socket_path_of(host: str) -> str | None:
    return host[len("unix://") :] if host.startswith("unix://") else None


def split_image_ref(ref: str) -> tuple[str, str | None, str | None]:
    """拆出 (仓库, 标签, 摘要)；没写标签也没写摘要时标签视为 latest"""
    name, _, digest = ref.partition("@")
    tag = None
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        name, tag = name[:colon], name[colon + 1 :]
    if tag is None and not digest:
        tag = "latest"
    return name, tag, digest or None


def image_defaults(image: dict) -> dict:
    """只保留镜像里会被容器继承的配置，结构与镜像详情一致，可直接交给 build_recreate_spec"""
    config = (image or {}).get("Config") or {}
    keys = ("Env", "Labels", *_IMAGE_DEFAULT_KEYS)
    return {"Config": {k: config[k] for k in keys if config.get(k) is not None}}


def _same(a: Any, b: Any) -> bool:
    return (a or None) == (b or None)


def _endpoint_config(endpoint: dict, short_id: str, user_defined: bool) -> dict:
    out: dict[str, Any] = {}
    if user_defined:
        # Docker 会自动把容器短 ID 加进别名，不能原样带过去
        aliases = [a for a in endpoint.get("Aliases") or [] if a != short_id]
        if aliases:
            out["Aliases"] = aliases
    for key in ("IPAMConfig", "Links", "DriverOpts"):
        if endpoint.get(key):
            out[key] = endpoint[key]
    return out


def build_recreate_spec(
    container: dict, old_image: dict, image_ref: str
) -> tuple[dict, dict[str, dict]]:
    """根据旧容器生成新容器的创建参数。

    返回 (创建请求体, 创建后还要连接的其它网络)。
    """
    config = copy.deepcopy(container.get("Config") or {})
    image_config = (old_image or {}).get("Config") or {}
    short_id = container["Id"][:12]

    config["Image"] = image_ref
    if config.get("Hostname") == short_id:
        config.pop("Hostname", None)

    image_env = set(image_config.get("Env") or [])
    config["Env"] = [e for e in config.get("Env") or [] if e not in image_env]
    image_labels = image_config.get("Labels") or {}
    config["Labels"] = {
        k: v
        for k, v in (config.get("Labels") or {}).items()
        if image_labels.get(k) != v
    }
    for key in _IMAGE_DEFAULT_KEYS:
        if key in config and _same(config.get(key), image_config.get(key)):
            config.pop(key)

    host_config = copy.deepcopy(container.get("HostConfig") or {})
    mode = host_config.get("NetworkMode") or "default"
    if mode == "default":
        mode = "bridge"

    body: dict[str, Any] = {**config, "HostConfig": host_config}
    if mode in ("host", "none") or mode.startswith("container:"):
        return body, {}

    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
    endpoints = {
        name: _endpoint_config(ep or {}, short_id, name not in _BUILTIN_NETWORKS)
        for name, ep in networks.items()
    }
    # 老版本 Docker 创建时只接受一个网络，其余的创建后再连
    if mode in endpoints:
        primary = endpoints.pop(mode)
        if primary:
            body["NetworkingConfig"] = {"EndpointsConfig": {mode: primary}}
    return body, endpoints


def demux_logs(raw: bytes) -> str:
    """非 TTY 容器的日志带 8 字节帧头，拆掉后拼成文本"""
    out = bytearray()
    pos = 0
    while pos + 8 <= len(raw):
        size = struct.unpack(">I", raw[pos + 4 : pos + 8])[0]
        out += raw[pos + 8 : pos + 8 + size]
        pos += 8 + size
    if pos == 0:
        return raw.decode("utf-8", "replace")
    return out.decode("utf-8", "replace")


class DockerClient:
    def __init__(self, host: str | None = None, timeout: float = 30):
        self.host = host or docker_host()
        self.socket_path = socket_path_of(self.host)
        if self.socket_path:
            transport = httpx.HTTPTransport(uds=self.socket_path)
            base_url = "http://docker"
        elif self.host.startswith(("tcp://", "http://")):
            transport = None
            base_url = "http://" + self.host.split("://", 1)[1]
        else:
            raise ValueError(f"不支持的 DOCKER_HOST：{self.host}")
        self._client = httpx.Client(
            base_url=base_url, transport=transport, timeout=timeout, trust_env=False
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        resp = self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message") or resp.text
            except ValueError:
                message = resp.text
            raise DockerError(resp.status_code, message.strip() or resp.reason_phrase)
        return resp

    # ── 查询 ──

    def ping(self) -> None:
        self._request("GET", "/_ping", timeout=5)

    def inspect_container(self, ref: str) -> dict:
        return self._request("GET", f"/containers/{quote(ref, safe='')}/json").json()

    def inspect_image(self, ref: str) -> dict:
        return self._request("GET", f"/images/{quote(ref, safe='/:@')}/json").json()

    def remote_digest(self, ref: str) -> str:
        """镜像仓库里这个标签当前指向的摘要（由 Docker 守护进程代为查询）"""
        data = self._request(
            "GET", f"/distribution/{quote(ref, safe='/:@')}/json", timeout=20
        ).json()
        return data["Descriptor"]["digest"]

    def list_containers(self, label: str) -> list[dict]:
        filters = json.dumps({"label": [label]})
        return self._request(
            "GET", "/containers/json", params={"all": "true", "filters": filters}
        ).json()

    def logs(self, ref: str, tail: int = 20) -> str:
        resp = self._request(
            "GET",
            f"/containers/{quote(ref, safe='')}/logs",
            params={"stdout": "true", "stderr": "true", "tail": str(tail)},
        )
        return demux_logs(resp.content)

    # ── 镜像 ──

    def pull(self, ref: str, on_progress: Callable[[dict], None] | None = None) -> None:
        repo, tag, digest = split_image_ref(ref)
        params = {"fromImage": repo, "tag": digest or tag}
        timeout = httpx.Timeout(20, read=900)
        with self._client.stream(
            "POST", "/images/create", params=params, timeout=timeout
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise DockerError(resp.status_code, resp.text.strip())
            for line in resp.iter_lines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("error"):
                    raise DockerError(500, str(event["error"]))
                if on_progress:
                    on_progress(event)

    def tag_image(self, source: str, target: str) -> None:
        repo, tag, digest = split_image_ref(target)
        if digest or tag is None:
            raise ValueError("镜像别名必须是标签，不能是固定摘要")
        self._request(
            "POST",
            f"/images/{quote(source, safe='/:@')}/tag",
            params={"repo": repo, "tag": tag},
        )

    def remove_image(self, ref: str) -> None:
        self._request("DELETE", f"/images/{quote(ref, safe='/:@')}")

    # ── 容器 ──

    def create_container(self, body: dict, name: str) -> str:
        return self._request(
            "POST", "/containers/create", params={"name": name}, json=body
        ).json()["Id"]

    def start(self, ref: str) -> None:
        self._request("POST", f"/containers/{quote(ref, safe='')}/start")

    def stop(self, ref: str, timeout: int = 30) -> None:
        self._request(
            "POST",
            f"/containers/{quote(ref, safe='')}/stop",
            params={"t": str(timeout)},
            timeout=timeout + 30,
        )

    def rename(self, ref: str, name: str) -> None:
        self._request(
            "POST", f"/containers/{quote(ref, safe='')}/rename", params={"name": name}
        )

    def remove_container(self, ref: str, force: bool = False) -> None:
        self._request(
            "DELETE",
            f"/containers/{quote(ref, safe='')}",
            params={"force": "true" if force else "false"},
        )

    def connect_network(self, network: str, container: str, endpoint: dict) -> None:
        self._request(
            "POST",
            f"/networks/{quote(network, safe='')}/connect",
            json={"Container": container, "EndpointConfig": endpoint},
        )
