"""
页面内更新：
- 镜像引用解析、版本比较、日志解帧
- 重建容器时去掉旧镜像带来的默认值，保留用户配置与网络
- 助手重建流程：成功、无需更新、新容器起不来时回滚、旧镜像已查不到
- 面板侧：没有 socket 时按版本号判断；拉到新镜像才启动助手；接口鉴权与冲突
"""

import json
import struct
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.utils.docker_api import (
    DockerError,
    build_recreate_spec,
    demux_logs,
    image_defaults,
    split_image_ref,
)

OLD_ID = "a" * 64
NEW_IMAGE_ID = "sha256:" + "2" * 64
OLD_IMAGE_ID = "sha256:" + "1" * 64


def make_container(**overrides):
    container = {
        "Id": OLD_ID,
        "Name": "/tg-signpulse",
        "Image": OLD_IMAGE_ID,
        "Config": {
            "Hostname": OLD_ID[:12],
            "Image": "luochend/tg-signpulse:latest",
            "Env": [
                "PATH=/venv/bin:/usr/bin",
                "BUILD_SHA=old",
                "APP_SECRET_KEY=keep-me",
            ],
            "Labels": {
                "org.opencontainers.image.revision": "old",
                "com.docker.compose.project": "tg",
            },
            "Entrypoint": ["/entrypoint.sh"],
            "Cmd": None,
            "WorkingDir": "/app",
            "User": "",
            "ExposedPorts": {"8080/tcp": {}},
            "Healthcheck": {"Test": ["CMD", "true"]},
            "StopTimeout": 30,
        },
        "HostConfig": {
            "NetworkMode": "tg_default",
            "Binds": [
                "/srv/tg/data:/data",
                "/var/run/docker.sock:/var/run/docker.sock",
            ],
            "RestartPolicy": {"Name": "unless-stopped"},
            "ReadonlyRootfs": True,
        },
        "Mounts": [
            {"Destination": "/data", "Source": "/srv/tg/data"},
            {"Destination": "/var/run/docker.sock", "Source": "/run/docker.sock"},
        ],
        "NetworkSettings": {
            "Networks": {
                "tg_default": {
                    "Aliases": ["tg-signpulse", "app", OLD_ID[:12]],
                    "IPAMConfig": None,
                    "Links": None,
                },
                "proxy_net": {
                    "Aliases": ["app"],
                    "IPAMConfig": {"IPv4Address": "10.0.0.9"},
                },
            }
        },
        "State": {"Running": True},
    }
    container.update(overrides)
    return container


OLD_IMAGE = {
    "Id": OLD_IMAGE_ID,
    "RepoDigests": ["luochend/tg-signpulse@sha256:old"],
    "Config": {
        "Env": ["PATH=/venv/bin:/usr/bin", "BUILD_SHA=old"],
        "Labels": {"org.opencontainers.image.revision": "old"},
        "Entrypoint": ["/entrypoint.sh"],
        "Cmd": None,
        "WorkingDir": "/app",
        "User": "",
        "ExposedPorts": {"8080/tcp": {}},
        "Healthcheck": {"Test": ["CMD", "true"]},
    },
}


# ── 纯函数 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("luochend/tg-signpulse", ("luochend/tg-signpulse", "latest", None)),
        ("ghcr.io/o/app:v1.2", ("ghcr.io/o/app", "v1.2", None)),
        ("localhost:5000/app", ("localhost:5000/app", "latest", None)),
        ("app@sha256:abc", ("app", None, "sha256:abc")),
    ],
)
def test_split_image_ref(ref, expected):
    assert split_image_ref(ref) == expected


def test_latest_version_ignores_prereleases():
    from backend.services.self_update import latest_version_from_tags, parse_version

    assert (
        latest_version_from_tags(["v0.9.0", "v0.10.1", "v0.11.0-rc1", "x"]) == "0.10.1"
    )
    assert latest_version_from_tags(["nightly"]) is None
    assert parse_version("0.10.0") > parse_version("0.9.9")


def test_demux_logs_strips_frame_headers():
    frames = b"".join(
        struct.pack(">BxxxI", 1, len(chunk)) + chunk
        for chunk in (b"a\n", "完成\n".encode())
    )
    assert demux_logs(frames) == "a\n完成\n"
    assert demux_logs(b"plain") == "plain"


def test_recreate_spec_drops_image_defaults_and_keeps_user_config():
    body, extra = build_recreate_spec(
        make_container(), OLD_IMAGE, "luochend/tg-signpulse:latest"
    )

    assert "Hostname" not in body
    assert body["Env"] == ["APP_SECRET_KEY=keep-me"]
    assert body["Labels"] == {"com.docker.compose.project": "tg"}
    for key in ("Entrypoint", "WorkingDir", "User", "ExposedPorts", "Healthcheck"):
        assert key not in body
    assert body["StopTimeout"] == 30
    assert body["HostConfig"]["Binds"][0] == "/srv/tg/data:/data"
    assert body["NetworkingConfig"] == {
        "EndpointsConfig": {"tg_default": {"Aliases": ["tg-signpulse", "app"]}}
    }
    assert extra == {
        "proxy_net": {"Aliases": ["app"], "IPAMConfig": {"IPv4Address": "10.0.0.9"}}
    }


def test_recreate_spec_default_bridge_and_host_network():
    container = make_container()
    container["HostConfig"]["NetworkMode"] = "default"
    container["NetworkSettings"]["Networks"] = {"bridge": {"Aliases": None}}
    body, extra = build_recreate_spec(container, OLD_IMAGE, "img")
    assert "NetworkingConfig" not in body
    assert extra == {}

    container["HostConfig"]["NetworkMode"] = "host"
    container["NetworkSettings"]["Networks"] = {"host": {}}
    _, extra = build_recreate_spec(container, OLD_IMAGE, "img")
    assert extra == {}


def test_recreate_spec_keeps_overridden_command():
    container = make_container()
    container["Config"]["Cmd"] = ["--debug"]
    body, _ = build_recreate_spec(container, OLD_IMAGE, "img")
    assert body["Cmd"] == ["--debug"]


# ── 助手重建流程 ─────────────────────────────────────────────────────────


class FakeDocker:
    socket_path = "/var/run/docker.sock"
    host = "unix:///var/run/docker.sock"

    def __init__(self, new_image_id=NEW_IMAGE_ID, fail_start=False):
        self.calls = []
        self.containers = {OLD_ID: make_container()}
        self.images = {
            OLD_IMAGE_ID: OLD_IMAGE,
            "luochend/tg-signpulse:latest": {
                "Id": OLD_IMAGE_ID,
                "RepoDigests": OLD_IMAGE["RepoDigests"],
            },
        }
        self.remote_digests = {"luochend/tg-signpulse:latest": "sha256:old"}
        self.new_image_id = new_image_id
        self.fail_start = fail_start
        self.created = []

    def _log(self, *call):
        self.calls.append(call)

    def inspect_container(self, ref):
        if ref not in self.containers:
            raise DockerError(404, "no such container")
        return self.containers[ref]

    def inspect_image(self, ref):
        if ref not in self.images:
            raise DockerError(404, "no such image")
        return self.images[ref]

    def remote_digest(self, ref):
        if ref not in self.remote_digests:
            raise DockerError(404, "manifest unknown")
        return self.remote_digests[ref]

    def tag_image(self, source, target):
        image = next(image for image in self.images.values() if image["Id"] == source)
        self.images[target] = dict(image)
        self._log("tag", source, target)

    def pull(self, ref, on_progress=None):
        self._log("pull", ref)
        self.images[ref] = {"Id": self.new_image_id}

    def stop(self, ref, timeout=30):
        self._log("stop", ref, timeout)

    def rename(self, ref, name):
        self._log("rename", ref, name)

    def create_container(self, body, name):
        new_id = "b" * 64 if not self.created else "c" * 64
        self.created.append((name, body))
        self._log("create", name)
        return new_id

    def connect_network(self, network, container, endpoint):
        self._log("connect", network, container[:1])

    def start(self, ref):
        self._log("start", ref[:1])
        if self.fail_start and ref != OLD_ID:
            raise DockerError(500, "port is already allocated")

    def remove_container(self, ref, force=False):
        self._log("remove", ref[:1] if len(ref) == 64 else ref, force)

    def remove_image(self, ref):
        self._log("rmi", ref)

    def close(self):
        pass


def test_recreate_replaces_container_and_cleans_up():
    from backend.update_helper import recreate

    docker = FakeDocker()
    docker.images["luochend/tg-signpulse:latest"] = {"Id": NEW_IMAGE_ID}
    logs = []
    changed = recreate(
        docker,
        OLD_ID,
        "luochend/tg-signpulse:latest",
        log=logs.append,
        ready=lambda c, i: None,
    )

    assert changed
    names = [c[0] for c in docker.calls]
    assert names == ["stop", "rename", "create", "connect", "start", "remove", "rmi"]
    assert docker.calls[1][2].startswith("tg-signpulse-old-")
    assert docker.created[0][0] == "tg-signpulse"
    assert docker.calls[5] == ("remove", "a", False)
    assert logs[-1] == "更新完成"


def test_recreate_skips_when_image_unchanged():
    from backend.update_helper import recreate

    docker = FakeDocker()
    assert not recreate(
        docker, OLD_ID, "luochend/tg-signpulse:latest", log=lambda m: None
    )
    assert docker.calls == []


def _new_image(env):
    return {"Id": NEW_IMAGE_ID, "Config": {**OLD_IMAGE["Config"], "Env": env}}


def test_recreate_uses_saved_old_image_config():
    """containerd 镜像存储里，标签移到新镜像后旧镜像就查不到了"""
    from backend.update_helper import recreate

    docker = FakeDocker()
    del docker.images[OLD_IMAGE_ID]
    docker.images["luochend/tg-signpulse:latest"] = _new_image(
        ["PATH=/venv/bin:/usr/bin", "BUILD_SHA=new"]
    )
    recreate(
        docker,
        OLD_ID,
        "luochend/tg-signpulse:latest",
        log=lambda m: None,
        ready=lambda c, i: None,
        old_image=image_defaults(OLD_IMAGE),
    )
    body = docker.created[0][1]
    assert body["Env"] == ["APP_SECRET_KEY=keep-me"]
    assert "Healthcheck" not in body


def test_recreate_falls_back_to_new_image_config():
    from backend.update_helper import recreate

    docker = FakeDocker()
    del docker.images[OLD_IMAGE_ID]
    docker.images["luochend/tg-signpulse:latest"] = _new_image(
        ["PATH=/venv/bin:/usr/bin", "BUILD_SHA=new"]
    )
    logs = []
    recreate(
        docker,
        OLD_ID,
        "luochend/tg-signpulse:latest",
        log=logs.append,
        ready=lambda c, i: None,
    )
    assert "找不到旧镜像的配置，按新镜像处理" in logs
    # 与新镜像相同的值交给镜像；不同的值无法分辨来源，只能保留
    assert docker.created[0][1]["Env"] == ["BUILD_SHA=old", "APP_SECRET_KEY=keep-me"]


def test_helper_main_reads_saved_config(monkeypatch):
    from backend import update_helper

    seen = {}

    def fake_recreate(client, target, image_ref, cleanup, old_image):
        seen.update(target=target, cleanup=cleanup, old_image=old_image)

    class Client:
        def __init__(self, timeout):
            pass

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(update_helper, "recreate", fake_recreate)
    monkeypatch.setattr(update_helper, "DockerClient", Client)
    monkeypatch.setenv("UPDATE_HELPER_DELAY", "0")
    monkeypatch.setenv("APP_UPDATE_CLEANUP", "0")
    monkeypatch.setenv("UPDATE_OLD_IMAGE_CONFIG", '{"Config": {"Env": ["A=1"]}}')
    assert update_helper.main([OLD_ID, "img:latest"]) == 0
    assert seen == {
        "target": OLD_ID,
        "cleanup": False,
        "old_image": {"Config": {"Env": ["A=1"]}},
        "closed": True,
    }

    monkeypatch.setenv("UPDATE_OLD_IMAGE_CONFIG", "not json")
    assert update_helper.main([OLD_ID, "img:latest"]) == 0
    assert seen["old_image"] is None
    assert update_helper.main([OLD_ID]) == 2


def test_recreate_rolls_back_when_new_container_fails():
    from backend.update_helper import recreate

    docker = FakeDocker(fail_start=True)
    docker.images["luochend/tg-signpulse:latest"] = {"Id": NEW_IMAGE_ID}
    with pytest.raises(DockerError):
        recreate(docker, OLD_ID, "luochend/tg-signpulse:latest", log=lambda m: None)

    names = [c[0] for c in docker.calls]
    assert names == [
        "stop",
        "rename",
        "create",
        "connect",
        "start",
        "remove",
        "rename",
        "start",
    ]
    assert docker.calls[5] == ("remove", "b", True)
    assert docker.calls[6] == ("rename", OLD_ID, "tg-signpulse")
    assert docker.calls[7] == ("start", "a")


def test_wait_until_ready_detects_crash_and_health():
    from backend.update_helper import wait_until_ready

    class States:
        def __init__(self, *states):
            self.states = list(states)

        def inspect_container(self, ref):
            return {"State": self.states.pop(0)}

    with pytest.raises(RuntimeError, match="退出"):
        wait_until_ready(States({"Running": False, "ExitCode": 3}), "x", poll=0)
    with pytest.raises(RuntimeError, match="健康检查"):
        wait_until_ready(
            States({"Running": True, "Health": {"Status": "unhealthy"}}), "x", poll=0
        )
    wait_until_ready(
        States(
            {"Running": True, "Health": {"Status": "starting"}},
            {"Running": True, "Health": {"Status": "healthy"}},
        ),
        "x",
        poll=0,
    )
    wait_until_ready(
        States({"Running": True}, {"Running": True}), "x", settle=0, poll=0
    )


# ── 面板侧服务 ───────────────────────────────────────────────────────────


@pytest.fixture
def service(monkeypatch):
    from backend.services import self_update

    monkeypatch.delenv("APP_SELF_UPDATE", raising=False)
    monkeypatch.delenv("APP_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(
        self_update,
        "current_build",
        lambda: {"version": "0.9.0", "build_sha": "", "built_at": ""},
    )
    svc = self_update.SelfUpdateService()
    monkeypatch.setattr(svc, "_latest_github_version", lambda: "0.9.2")
    return svc


def test_status_without_socket_compares_versions(service, monkeypatch):
    monkeypatch.setattr(service, "_docker", lambda: (None, None, "no_socket"))
    status = service.status()
    assert status["docker"] == {
        "enabled": False,
        "reason": "no_socket",
        "image": None,
        "container": None,
    }
    assert status["latest_version"] == "0.9.2"
    assert status["update_available"] is True

    # 结果会缓存，不会每次都去请求 GitHub
    monkeypatch.setattr(
        service, "_latest_github_version", lambda: pytest.fail("不该再次请求")
    )
    assert service.status()["update_available"] is True


def test_status_check_can_be_disabled(service, monkeypatch):
    monkeypatch.setenv("APP_UPDATE_CHECK", "false")
    monkeypatch.setattr(service, "_docker", lambda: (None, None, "no_socket"))
    status = service.status()
    assert status["check_enabled"] is False
    assert status["update_available"] is None


def test_status_with_docker_compares_digests(service, monkeypatch):
    docker = FakeDocker()
    docker.remote_digest = lambda ref: "sha256:old"
    docker.list_containers = lambda label: []
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    status = service.status()
    assert status["docker"]["enabled"] is True
    assert status["docker"]["image"] == "luochend/tg-signpulse:latest"
    # 版本号更高，但镜像仓库里的摘要没变，以摘要为准
    assert status["update_available"] is False

    docker.remote_digest = lambda ref: "sha256:new"
    assert service.status(refresh=True)["update_available"] is True


def _refuse(status, message):
    def remote_digest(ref):
        raise DockerError(status, message)

    return remote_digest


def test_status_blocks_one_click_when_registry_refuses(service, monkeypatch):
    from backend.services.self_update import UpdateUnavailable

    docker = FakeDocker()
    docker.list_containers = lambda label: []
    docker.remote_digest = _refuse(403, "denied: requested access is denied")
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    status = service.status()
    assert status["docker"]["enabled"] is False
    assert status["docker"]["reason"] == "not_pullable"
    assert status["docker"]["image"] == "luochend/tg-signpulse:latest"
    # 拉不到镜像时仍按版本号给出结果
    assert status["update_available"] is True

    with pytest.raises(UpdateUnavailable) as exc:
        service.apply()
    assert exc.value.reason == "not_pullable"
    assert service._job["state"] == "idle"

    # 镜像推到仓库后，重新检查即可恢复
    docker.remote_digest = lambda ref: "sha256:old"
    assert service.status(refresh=True)["docker"]["reason"] is None


def test_status_keeps_one_click_when_registry_is_down(service, monkeypatch):
    docker = FakeDocker()
    docker.list_containers = lambda label: []
    docker.remote_digest = _refuse(500, "registry unavailable")
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    status = service.status()
    assert status["docker"]["enabled"] is True
    assert status["check_error"] == "registry unavailable"
    assert status["update_available"] is True


def test_status_local_build_compares_versions(service, monkeypatch):
    docker = FakeDocker()
    docker.list_containers = lambda label: []
    docker.images[OLD_IMAGE_ID] = {**OLD_IMAGE, "RepoDigests": []}
    docker.remote_digest = lambda ref: "sha256:other"
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    status = service.status()
    assert status["docker"]["enabled"] is True
    assert status["local_build"] is True
    assert status["update_available"] is True


def test_status_when_tag_already_moved(service, monkeypatch):
    docker = FakeDocker()
    docker.list_containers = lambda label: []
    docker.remote_digest = lambda ref: "sha256:new"
    del docker.images[OLD_IMAGE_ID]
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    status = service.status()
    assert status["update_available"] is True
    assert status["check_error"] is None
    assert status["docker"]["enabled"] is True


def test_status_collects_finished_helper(service, monkeypatch):
    docker = FakeDocker()
    docker.remote_digest = lambda ref: "sha256:old"
    helper = {
        "Id": "h" * 64,
        "State": {"ExitCode": 1, "FinishedAt": "2026-09-17T08:00:00Z"},
    }
    docker.containers[helper["Id"]] = helper
    docker.list_containers = lambda label: [{"Id": helper["Id"], "State": "exited"}]
    docker.logs = lambda ref, tail=20: (
        "停止旧容器\n已恢复旧容器\n更新失败：新容器健康检查未通过\n"
    )
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )

    result = service.status()["last_result"]
    assert result["ok"] is False
    assert result["message"] == "更新失败：新容器健康检查未通过"
    assert ("remove", "h", True) in docker.calls


def _wait_job(svc, states=("pulling", "restarting")):
    for _ in range(100):
        if svc._job["state"] not in states:
            return svc._job
        time.sleep(0.01)
    raise AssertionError(svc._job)


def test_apply_reports_up_to_date_when_image_unchanged(service, monkeypatch):
    docker = FakeDocker(new_image_id=OLD_IMAGE_ID)
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    assert service.apply()["state"] in ("pulling", "up_to_date")
    assert _wait_job(service)["state"] == "up_to_date"
    assert docker.created == []


def test_apply_spawns_helper_with_socket_and_same_identity(service, monkeypatch):
    docker = FakeDocker()
    real_pull = docker.pull

    def pull_and_drop_old_image(ref, on_progress=None):
        real_pull(ref)
        del docker.images[OLD_IMAGE_ID]

    docker.pull = pull_and_drop_old_image
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    service.apply()
    job = _wait_job(service, states=("pulling",))
    assert job["state"] == "restarting"

    assert ("remove", "tg-signpulse-updater", True) in docker.calls
    name, body = docker.created[0]
    assert name == "tg-signpulse-updater"
    assert body["Image"] == "luochend/tg-signpulse:latest"
    assert body["Cmd"] == [OLD_ID, "luochend/tg-signpulse:latest"]
    assert body["Entrypoint"] == ["python", "-m", "backend.update_helper"]
    assert body["Labels"] == {"io.tg-signpulse.updater-for": "tg-signpulse"}
    host = body["HostConfig"]
    assert host["Binds"] == ["/run/docker.sock:/var/run/docker.sock"]
    assert host["NetworkMode"] == "none"
    assert host["CapDrop"] == ["ALL"]
    assert "DOCKER_HOST=unix:///var/run/docker.sock" in body["Env"]
    assert body["Healthcheck"] == {"Test": ["NONE"]}
    # 拉取前记下的旧镜像配置交给助手
    saved = [e for e in body["Env"] if e.startswith("UPDATE_OLD_IMAGE_CONFIG=")]
    assert len(saved) == 1
    assert json.loads(saved[0].split("=", 1)[1]) == image_defaults(OLD_IMAGE)

    # 更新进行中不能再点
    from backend.services.self_update import UpdateInProgress

    with pytest.raises(UpdateInProgress):
        service.apply()


def test_apply_failure_is_reported(service, monkeypatch):
    docker = FakeDocker()

    def broken_pull(ref, on_progress=None):
        raise DockerError(500, "manifest unknown")

    docker.pull = broken_pull
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    service.apply()
    job = _wait_job(service)
    assert job["state"] == "failed"
    assert job["message"] == "manifest unknown"


def test_apply_pull_refused_blocks_one_click(service, monkeypatch):
    docker = FakeDocker()
    docker.list_containers = lambda label: []
    docker.remote_digest = lambda ref: "sha256:old"
    docker.pull = lambda ref, on_progress=None: _refuse(404, "pull access denied")(ref)
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    assert service.status()["docker"]["enabled"] is True
    service.apply()
    job = _wait_job(service)
    assert job["state"] == "failed"
    assert job["code"] == "not_pullable"
    assert service.status()["docker"]["reason"] == "not_pullable"


def test_apply_unavailable_resets_job(service, monkeypatch):
    from backend.services.self_update import UpdateUnavailable

    monkeypatch.setattr(service, "_docker", lambda: (None, None, "permission"))
    with pytest.raises(UpdateUnavailable) as exc:
        service.apply()
    assert exc.value.reason == "permission"
    assert service._job["state"] == "idle"


def test_docker_disabled_by_env(service, monkeypatch):
    monkeypatch.setenv("APP_SELF_UPDATE", "0")
    from backend.services.self_update import SelfUpdateService

    assert SelfUpdateService()._docker() == (None, None, "disabled")


def test_docker_missing_socket(monkeypatch, tmp_path):
    from backend.services.self_update import SelfUpdateService

    monkeypatch.delenv("APP_SELF_UPDATE", raising=False)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path}/docker.sock")
    assert SelfUpdateService()._docker() == (None, None, "no_socket")


def test_guess_self_container_refs(monkeypatch):
    from backend.services import self_update

    monkeypatch.setenv("APP_UPDATE_CONTAINER", "panel")
    assert self_update.guess_self_container_refs() == ["panel"]


# ── 接口 ─────────────────────────────────────────────────────────────────


@pytest.fixture
def api(monkeypatch, service):
    from backend.api.routes import update
    from backend.core.auth import get_current_user
    from backend.services import self_update, sign_tasks

    running = {"value": False}
    fake_tasks = type("T", (), {"has_running_tasks": lambda self: running["value"]})()
    monkeypatch.setattr(sign_tasks, "get_sign_task_service", lambda: fake_tasks)
    monkeypatch.setattr(self_update, "get_self_update_service", lambda: service)
    monkeypatch.setattr(update, "get_self_update_service", lambda: service)

    app = FastAPI()
    app.include_router(update.router, prefix="/api/update")
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: object()
    return client, app, running


def test_update_routes_require_login(api):
    client, app, _ = api
    app.dependency_overrides.clear()
    assert client.get("/api/update/status").status_code == 401
    assert client.post("/api/update/apply").status_code == 401


def test_apply_route_conflicts(api, service, monkeypatch):
    client, _, running = api
    running["value"] = True
    resp = client.post("/api/update/apply")
    assert resp.status_code == 409
    assert resp.json()["code"] == "TASKS_RUNNING"

    running["value"] = False
    monkeypatch.setattr(service, "_docker", lambda: (None, None, "no_socket"))
    resp = client.post("/api/update/apply")
    assert resp.status_code == 400
    assert resp.json()["code"] == "UPDATE_NO_SOCKET"


def test_status_route(api, service, monkeypatch):
    client, _, _ = api
    monkeypatch.setattr(service, "_docker", lambda: (None, None, "no_socket"))
    body = client.get("/api/update/status").json()
    assert body["current"]["version"] == "0.9.0"
    assert body["job"]["state"] == "idle"


# ── 数字版本镜像与加速缓存 ──────────────────────────────────────────────


def test_stale_latest_does_not_hide_release(service, monkeypatch):
    docker = FakeDocker()
    docker.remote_digests["luochend/tg-signpulse:0.9.2"] = "sha256:new"
    docker.list_containers = lambda label: []
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )

    status = service.status()

    assert status["update_available"] is True
    assert status["latest_version"] == "0.9.2"
    assert service._check["pull_ref"] == "luochend/tg-signpulse:0.9.2"


def test_release_already_installed_ignores_stale_latest(service, monkeypatch):
    from backend.services import self_update

    monkeypatch.setattr(self_update, "current_build", lambda: {"version": "0.9.2"})
    docker = FakeDocker()
    docker.remote_digests["luochend/tg-signpulse:0.9.2"] = "sha256:new"
    docker.images[OLD_IMAGE_ID] = {
        **OLD_IMAGE,
        "RepoDigests": ["luochend/tg-signpulse@sha256:new"],
    }
    docker.list_containers = lambda label: []
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )

    assert service.status()["update_available"] is False


def test_unpublished_release_does_not_offer_old_latest(service, monkeypatch):
    docker = FakeDocker()
    docker.list_containers = lambda label: []
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )

    assert service.status()["update_available"] is False
    assert service._check["pull_ref"] == "luochend/tg-signpulse:latest"


def test_apply_downloads_release_and_preserves_latest_alias(service, monkeypatch):
    docker = FakeDocker()
    docker.remote_digests["luochend/tg-signpulse:0.9.2"] = "sha256:new"
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )

    service.apply()
    assert _wait_job(service, states=("pulling",))["state"] == "restarting"
    assert ("pull", "luochend/tg-signpulse:0.9.2") in docker.calls
    assert ("pull", "luochend/tg-signpulse:latest") not in docker.calls
    assert ("tag", NEW_IMAGE_ID, "luochend/tg-signpulse:latest") in docker.calls
    assert docker.images["luochend/tg-signpulse:latest"]["Id"] == NEW_IMAGE_ID
    assert docker.created[0][1]["Cmd"] == [OLD_ID, "luochend/tg-signpulse:latest"]


def test_newer_installed_release_does_not_downgrade(service, monkeypatch):
    from backend.services import self_update

    monkeypatch.setattr(self_update, "current_build", lambda: {"version": "0.9.3"})
    docker = FakeDocker()
    docker.remote_digests["luochend/tg-signpulse:0.9.3"] = "sha256:old"
    docker.list_containers = lambda label: []
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )

    assert service.status()["update_available"] is False
    assert service._check["pull_ref"] == "luochend/tg-signpulse:0.9.3"


def test_custom_image_tag_is_kept(service):
    assert (
        service._release_image_ref("registry:5000/app:stable", "0.9.2")
        == "registry:5000/app:stable"
    )
    assert (
        service._release_image_ref("registry:5000/app:latest", "0.9.2")
        == "registry:5000/app:0.9.2"
    )


def test_tag_image_sends_docker_tag_request():
    import httpx

    from backend.utils.docker_api import DockerClient

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(201)

    client = DockerClient("tcp://docker.example:2375")
    client._client.close()
    client._client = httpx.Client(
        base_url="http://docker.example:2375", transport=httpx.MockTransport(handle)
    )
    try:
        client.tag_image("sha256:new", "registry:5000/owner/app:latest")
        assert len(requests) == 1
        assert requests[0].method == "POST"
        assert requests[0].url.path == "/images/sha256:new/tag"
        assert requests[0].url.params["repo"] == "registry:5000/owner/app"
        assert requests[0].url.params["tag"] == "latest"
    finally:
        client.close()


def test_numeric_image_network_failure_keeps_numeric_download(service, monkeypatch):
    docker = FakeDocker()

    def digest(ref):
        if ref.endswith(":0.9.2"):
            raise DockerError(500, "registry unavailable")
        return "sha256:old"

    docker.remote_digest = digest
    docker.list_containers = lambda label: []
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    status = service.status()
    assert status["check_error"] == "registry unavailable"
    assert service._check["pull_ref"] == "luochend/tg-signpulse:0.9.2"


def test_apply_repairs_alias_when_release_is_already_running(service, monkeypatch):
    docker = FakeDocker(new_image_id=OLD_IMAGE_ID)
    docker.remote_digests["luochend/tg-signpulse:0.9.2"] = "sha256:old"
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    service.apply()
    assert _wait_job(service)["state"] == "up_to_date"
    assert ("tag", OLD_IMAGE_ID, "luochend/tg-signpulse:latest") in docker.calls
    assert docker.created == []


def test_apply_refuses_older_cached_image(service, monkeypatch):
    from backend.services import self_update

    monkeypatch.setattr(self_update, "current_build", lambda: {"version": "0.9.3"})
    monkeypatch.setenv("APP_UPDATE_CHECK", "0")
    docker = FakeDocker()
    original_pull = docker.pull

    def old_cached_pull(ref, on_progress=None):
        original_pull(ref)
        docker.images[ref]["Config"] = {
            "Labels": {"org.opencontainers.image.version": "0.9.1"}
        }

    docker.pull = old_cached_pull
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    service.apply()
    job = _wait_job(service)
    assert job["state"] == "failed"
    assert job["code"] == "older_version"
    assert docker.created == []
    assert docker.images["luochend/tg-signpulse:latest"]["Id"] == OLD_IMAGE_ID


# ── 下载中断恢复 ──────────────────────────────────────────────────────────


@pytest.fixture
def pull_client(monkeypatch):
    import httpx

    from backend.utils import docker_api

    monkeypatch.setattr(docker_api.time, "sleep", lambda seconds: None)
    client = docker_api.DockerClient("tcp://docker.example:2375")
    client._client.close()
    calls = []

    def configure(handler):
        def handle(request):
            calls.append(request)
            return handler(request)

        client._client = httpx.Client(
            base_url="http://docker.example:2375", transport=httpx.MockTransport(handle)
        )
        return client, calls

    yield configure
    client.close()


def test_pull_recovers_from_eof_stream(pull_client):
    import httpx

    attempts = []

    def handle(request):
        if request.url.path == "/images/create":
            attempts.append(request)
            event = (
                {"error": "unexpected EOF"}
                if len(attempts) < 3
                else {"status": "Status: Downloaded newer image for test"}
            )
            return httpx.Response(200, content=json.dumps(event) + "\n")
        return httpx.Response(500, json={"message": "registry unavailable"})

    client, _ = pull_client(handle)
    progress = []
    client.pull("luochend/tg-signpulse:0.9.4", on_progress=progress.append)
    assert len(attempts) == 3
    assert [e["message"] for e in progress if "message" in e] == [
        "下载连接中断，正在重试（2/3）",
        "下载连接中断，正在重试（3/3）",
    ]


@pytest.mark.parametrize("failure", ["transport", "http", "stream"])
def test_pull_has_bounded_transient_retries(pull_client, failure):
    import httpx

    attempts = []

    def handle(request):
        if request.url.path == "/images/create":
            attempts.append(request)
            if failure == "transport":
                raise httpx.ReadError("connection reset", request=request)
            if failure == "http":
                return httpx.Response(500, json={"message": "unexpected EOF"})
            return httpx.Response(200, content='{"error":"unexpected EOF"}\n')
        return httpx.Response(404, json={"message": "not found"})

    client, calls = pull_client(handle)
    with pytest.raises((DockerError, httpx.ReadError)):
        client.pull("luochend/tg-signpulse:0.9.4")
    assert len(attempts) == 3
    assert not any(r.url.path.endswith("/tag") for r in calls)


@pytest.mark.parametrize(
    "status,message",
    [(401, "denied"), (404, "manifest unknown"), (500, "manifest unknown")],
)
def test_pull_does_not_retry_permanent_failure(pull_client, status, message):
    import httpx

    client, calls = pull_client(
        lambda request: httpx.Response(status, json={"message": message})
    )
    with pytest.raises(DockerError):
        client.pull("luochend/tg-signpulse:0.9.4")
    assert len(calls) == 1


def test_pull_uses_configured_mirror_with_pinned_digest(pull_client):
    import httpx

    digest = "sha256:" + "a" * 64
    attempted = []

    def handle(request):
        path = request.url.path
        if path == "/images/create":
            attempted.append(request.url.params)
            if len(attempted) == 1:
                return httpx.Response(200, content='{"error":"unexpected EOF"}\n')
            return httpx.Response(
                200, content='{"status":"Status: Image is up to date for test"}\n'
            )
        if path.startswith("/distribution/"):
            return httpx.Response(200, json={"Descriptor": {"digest": digest}})
        if path == "/info":
            return httpx.Response(
                200, json={"RegistryConfig": {"Mirrors": ["https://mirror.example/"]}}
            )
        if path.endswith("/json"):
            return httpx.Response(200, json={"Id": NEW_IMAGE_ID})
        if path.endswith("/tag"):
            return httpx.Response(201)
        raise AssertionError(path)

    client, calls = pull_client(handle)
    client.pull("luochend/tg-signpulse:0.9.4")
    assert len(attempted) == 2
    assert attempted[1]["fromImage"] == "mirror.example/luochend/tg-signpulse"
    assert attempted[1]["tag"] == digest
    tag = calls[-1]
    assert tag.url.path == f"/images/{NEW_IMAGE_ID}/tag"
    assert tag.url.params["repo"] == "luochend/tg-signpulse"
    assert tag.url.params["tag"] == "0.9.4"


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("luochend/tg-signpulse:0.9.4", "luochend/tg-signpulse"),
        ("docker.io/luochend/tg-signpulse:0.9.4", "luochend/tg-signpulse"),
        ("alpine:latest", "library/alpine"),
        ("ghcr.io/loochenx/tg-signpulse:0.9.4", None),
        ("localhost:5000/app:latest", None),
    ],
)
def test_mirror_only_applies_to_docker_hub(ref, expected):
    from backend.utils.docker_api import registry_mirror_refs

    digest = "sha256:" + "a" * 64
    mirrors = [
        "https://mirror.example/",
        "https://user:password@invalid.example/",
        "https://invalid.example/path",
    ]
    assert registry_mirror_refs(ref, mirrors, digest) == (
        [f"mirror.example/{expected}@{digest}"] if expected else []
    )
    assert registry_mirror_refs(ref, mirrors, "sha256:invalid") == []


def test_failed_download_does_not_restart_or_retag(service, monkeypatch):
    docker = FakeDocker()

    def fail(ref, on_progress=None):
        if on_progress:
            on_progress({"message": "下载连接中断，正在重试（2/3）"})
        assert service._job["state"] == "pulling"
        assert service._job["message"].endswith("（2/3）")
        raise DockerError(500, "unexpected EOF")

    docker.pull = fail
    monkeypatch.setattr(
        service, "_docker", lambda: (docker, docker.containers[OLD_ID], None)
    )
    service.apply()
    job = _wait_job(service)
    assert job["state"] == "failed"
    assert job["code"] == "download_interrupted"
    assert "自动重试" in job["message"]
    assert docker.created == []
    assert not any(c[0] in ("tag", "stop") for c in docker.calls)


@pytest.mark.parametrize("body", ["", '{"status":"Downloading"}\n', '{"status":'])
def test_incomplete_download_stream_is_not_success(pull_client, body):
    import httpx

    def handle(request):
        if request.url.path == "/images/create":
            return httpx.Response(200, content=body)
        return httpx.Response(404, json={"message": "not found"})

    client, calls = pull_client(handle)
    with pytest.raises(DockerError, match="did not finish"):
        client.pull("luochend/tg-signpulse:0.9.4")
    assert len([r for r in calls if r.url.path == "/images/create"]) == 3
    assert not any(r.url.path.endswith("/tag") for r in calls)


def test_refused_mirror_does_not_mark_original_as_refused(pull_client):
    import httpx

    digest = "sha256:" + "a" * 64
    attempts = []

    def handle(request):
        if request.url.path == "/images/create":
            attempts.append(request.url.params)
            if len(attempts) == 1:
                return httpx.Response(200, content='{"error":"unexpected EOF"}\n')
            return httpx.Response(
                404, json={"message": "mirror does not have this image"}
            )
        if request.url.path.startswith("/distribution/"):
            return httpx.Response(200, json={"Descriptor": {"digest": digest}})
        return httpx.Response(
            200, json={"RegistryConfig": {"Mirrors": ["https://mirror.example/"]}}
        )

    client, _ = pull_client(handle)
    with pytest.raises(DockerError) as exc:
        client.pull("luochend/tg-signpulse:0.9.4")
    # 第二次失败来自加速源，第三次回到原仓库，原仓库的实际拒绝应如实报告。
    assert len(attempts) == 3
    assert attempts[2]["fromImage"] == "luochend/tg-signpulse"
    assert exc.value.status == 404
