"""Harbor 0.23 copy transport for single-service Linux tasks without host log mounts."""

import json
import shutil
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from harbor.environments.docker.docker import DockerEnvironment

from flashrsi.integrations._container_payload import (
    _RUNTIME_CACHE,
    runtime_cache_metadata,
)
from flashrsi.integrations._verifier_dependencies import ROOT, verifier_runtime_metadata


class _RemoteDockerEnvironment(DockerEnvironment):
    def __init__(
        self,
        *args,
        prepared_image=None,
        agent_runtime_cache_dir=None,
        verifier_runtime_cache=None,
        **kwargs,
    ):
        self._agent_runtime_cache = (
            runtime_cache_metadata(agent_runtime_cache_dir)
            if agent_runtime_cache_dir
            else None
        )
        self._verifier_runtime_cache = (
            verifier_runtime_metadata(verifier_runtime_cache)
            if verifier_runtime_cache
            else None
        )
        if prepared_image is not None:
            kwargs["task_env_config"] = kwargs["task_env_config"].model_copy(
                update={"docker_image": prepared_image}
            )
        super().__init__(*args, **kwargs)

    @property
    def capabilities(self):
        # Trial downloads logs via Harbor's existing copy transport.
        return super().capabilities.model_copy(update={"mounted": False})

    def _write_mounts_compose_file(self):
        # Task-defined volumes require their own synchronization semantics.
        # Refuse them rather than silently dropping inputs or isolation rules.
        if self._is_windows_container or self._uses_compose:
            raise ValueError("Remote Docker supports single-service Linux tasks only")
        for mount in self._mounts:
            target = PurePosixPath(mount.get("target", ""))
            if (
                mount.get("type") != "bind"
                or mount.get("read_only")
                or ".." in target.parts
                or not target.is_relative_to("/logs")
                or target == PurePosixPath("/logs")
            ):
                raise ValueError(
                    "Remote Docker supports only writable log mounts under /logs"
                )
        path = super()._write_mounts_compose_file()
        volumes = []
        for cache, target in (
            (self._agent_runtime_cache, _RUNTIME_CACHE),
            (getattr(self, "_verifier_runtime_cache", None), ROOT),
        ):
            if not cache:
                continue
            volumes.append(
                {
                    "type": "bind",
                    "source": cache["path"],
                    "target": target,
                    "read_only": True,
                    "bind": {"create_host_path": False},
                }
            )
        path.write_text(json.dumps({"services": {"main": {"volumes": volumes}}}))
        # Keep self._mounts: start() uses their targets to create log directories.
        return path

    async def download_file(self, source_path, target_path):
        # Docker copy can preserve symlinks. Do not put them where Harbor or
        # FlashRSI might interpret an absolute container link as a host path.
        with TemporaryDirectory(prefix="flashrsi-harbor-copy-") as temporary:
            staged = Path(temporary) / "file"
            await super().download_file(source_path, staged)
            _check_download(staged)
            if not staged.is_file():
                raise ValueError("Expected one regular Harbor output file")
            shutil.copyfile(staged, target_path)

    async def download_dir(self, source_dir, target_dir):
        with TemporaryDirectory(prefix="flashrsi-harbor-copy-") as temporary:
            staged = Path(temporary)
            await super().download_dir(source_dir, staged)
            for item in staged.rglob("*"):
                _check_download(item)
            shutil.copytree(staged, target_dir, dirs_exist_ok=True)


def _check_download(path):
    if (
        path.is_symlink()
        or path.is_junction()
        or not (path.is_file() or path.is_dir())
        or (path.is_file() and path.stat().st_nlink > 1)
    ):
        raise ValueError("Harbor output contains a linked or non-regular file")
