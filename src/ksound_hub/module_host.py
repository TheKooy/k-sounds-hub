"""Opt-in external module process protocol (ksounds-jsonl/1).

IP15m2: independent host, not yet connected to the K-Sounds UI/Remote.
Python isolation flags are *not* an OS sandbox. Run only code you trust.
"""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
from pathlib import Path, PurePosixPath

MAX_MESSAGE_BYTES = 65536
PROTOCOL = "ksounds-jsonl/1"


def runner_path(module_root: Path) -> Path:
    """Resolve only the validated relative Python entrypoint from module.json."""
    root = Path(module_root).resolve(strict=True)
    manifest = json.loads((root / "module.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("module.json must contain an object")
    runner = manifest.get("runner")
    if not isinstance(runner, dict) or runner.get("protocol") != PROTOCOL:
        raise ValueError("module has no compatible external runner")
    name = runner.get("entrypoint")
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("invalid runner entrypoint")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(p in {"", ".", ".."} for p in name.split("/")):
        raise ValueError("runner entrypoint must be a normal relative path")
    if relative.suffix != ".py":
        raise ValueError("runner entrypoint must be a Python file")
    target = (root / name).resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("runner entrypoint escapes module directory")
    return target


class ModuleProcess:
    """One module subprocess. No shell or in-process third-party imports."""

    def __init__(self, module_root: Path):
        self.root = Path(module_root).resolve()
        self.process: subprocess.Popen | None = None
        self._pending = bytearray()
        self._counter = 0

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _receive(self, timeout: float) -> dict:
        if not self.running or self.process is None or self.process.stdout is None:
            raise RuntimeError("module process is not running")
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            import time
            deadline = time.monotonic() + timeout
            while True:
                pos = self._pending.find(b"\n")
                if pos >= 0:
                    raw = bytes(self._pending[:pos])
                    del self._pending[:pos + 1]
                    try:
                        message = json.loads(raw)
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise RuntimeError("invalid module JSON response") from exc
                    if not isinstance(message, dict):
                        raise RuntimeError("module response must be an object")
                    return message
                if len(self._pending) >= MAX_MESSAGE_BYTES:
                    raise RuntimeError("module response exceeds size limit")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("module response timed out")
                if not selector.select(remaining):
                    raise TimeoutError("module response timed out")
                chunk = os.read(self.process.stdout.fileno(), min(4096, MAX_MESSAGE_BYTES - len(self._pending)))
                if not chunk:
                    raise RuntimeError("module closed stdout")
                self._pending.extend(chunk)

    def start(self, *, timeout: float = 3.0) -> dict:
        if self.running:
            raise RuntimeError("module already running")
        entrypoint = runner_path(self.root)
        raw_manifest = json.loads((self.root / "module.json").read_text(encoding="utf-8"))
        from .module_registry import ModuleManifest
        manifest = ModuleManifest.from_dict(raw_manifest)
        self._pending.clear()
        self._counter = 0
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-u", str(entrypoint)],
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
        )
        try:
            response = self._receive(timeout)
            if response.get("type") != "hello" or response.get("protocol") != PROTOCOL:
                raise RuntimeError("external module failed protocol handshake")
            if response.get("id") != manifest.module_id or response.get("version") != manifest.version:
                raise RuntimeError("external module identity/version mismatch")
            return response
        except BaseException:
            self.stop()
            raise

    def request(self, command: str, payload: dict | None = None, *, timeout: float = 3.0) -> dict:
        if not self.running or self.process is None or self.process.stdin is None:
            raise RuntimeError("module process is not running")
        if not isinstance(command, str) or not command or len(command) > 80:
            raise ValueError("invalid command")
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        self._counter += 1
        serial = self._counter
        encoded = (json.dumps({"seq": serial, "command": command, "payload": payload or {}}, separators=(",", ":")) + "\n").encode()
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise ValueError("request exceeds size limit")
        try:
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
            response = self._receive(timeout)
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError("module communication failed") from exc
        if response.get("seq") != serial or not isinstance(response.get("ok"), bool):
            raise RuntimeError("invalid or out-of-order module response")
        return response

    def stop(self) -> None:
        process, self.process = self.process, None
        self._pending.clear()
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


class ExternalModuleHost:
    """Owner of opt-in module processes; not an OS security sandbox."""

    def __init__(self, manager):
        import threading
        self.manager = manager
        self.registry = manager.registry
        self._processes: dict[str, ModuleProcess] = {}
        self._lock = threading.RLock()

    def start(self, module_id: str) -> dict:
        with self._lock:
            manifest = self.registry.manifest(module_id)
            if (manifest is None or manifest.builtin
                    or not self.registry.is_enabled(module_id)):
                raise ValueError("external module must be installed and enabled")
            existing = self._processes.get(module_id)
            if existing is not None:
                if existing.running:
                    raise RuntimeError("external module already running")
                self._processes.pop(module_id, None)
                existing.stop()
            process = ModuleProcess(self.registry.external_root / module_id)
            try:
                hello = process.start()
            except BaseException:
                process.stop()
                raise
            self._processes[module_id] = process
            self.manager.mark_runtime_state(module_id, "running")
            return hello

    def running(self, module_id: str) -> bool:
        """Is a particular external worker still alive?"""
        with self._lock:
            worker = self._processes.get(str(module_id))
            return worker is not None and worker.running

    def request(self, module_id: str, command: str, payload: dict | None = None, *, timeout: float = 3.0) -> dict:
        """Serialize requests against stop/restart; no module imports in host."""
        with self._lock:
            worker = self._processes.get(str(module_id))
            if worker is None or not worker.running:
                raise RuntimeError("external module is not running")
            return worker.request(command, payload, timeout=timeout)

    def stop(self, module_id: str) -> None:
        with self._lock:
            process = self._processes.pop(module_id, None)
            try:
                if process is not None:
                    process.stop()
            finally:
                self.manager.mark_runtime_state(module_id, "stopped")

    def restart(self, module_id: str) -> dict:
        with self._lock:
            self.stop(module_id)
            return self.start(module_id)

    def crashed(self) -> tuple[str, ...]:
        """Forget dead workers; caller records runtime failure on the UI thread."""
        with self._lock:
            dead = [mid for mid, process in self._processes.items()
                    if not process.running]
            for mid in dead:
                self.stop(mid)
            return tuple(dead)

    def stop_all(self) -> None:
        with self._lock:
            for mid in tuple(self._processes):
                self.stop(mid)
