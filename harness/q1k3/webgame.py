"""Browser-owned WebGL game session with paused-while-thinking semantics.

The module deliberately keeps the agent-facing surface small.  The browser,
HTTP server, input validation, pause gate, screenshots, and trajectory are all
owned here; a solver only asks for an observation and submits a named action
or a bounded relative-pointer batch. This keeps the input path game-agnostic
without exposing a browser handle.

The first pause adapter is a dedicated Chromium process group.  The harness
resumes that process only while executing one action and stops it again before
returning an observation.  This freezes the page's main loop, workers, audio,
and renderer together.  Games that provide a cooperative bridge can add a
bridge adapter later without changing the GameSession API.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Literal

from PIL import Image as PILImage
from PIL import ImageChops, ImageFilter, ImageStat

try:
    from .visual_score import SCORE_PROFILE, VisualScore, score_pair
except ImportError:
    from visual_score import SCORE_PROFILE, VisualScore, score_pair

PIXEL_RMS_SCALE = 24.0
CANDIDATE_COUNT = 20
CALIBRATION_PATH = Path(
    os.environ.get("WEBGAME_CALIBRATION_FILE", str(Path(__file__).with_name("calibration.json")))
)
# Matches the verifier's guard: below this the floor and ceiling cannot separate
# a real capture from an arbitrary one.
MIN_CALIBRATION_SPAN = 0.05
_CALIBRATION_CACHE: list[dict[str, Any] | None] = []


def make_agent_preview(source: Path, preview_dir: Path, max_dim: int = 768) -> Path:
    """Create a compact JPEG preview without changing the source artifact."""
    source = Path(source)
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:24]
    preview = Path(preview_dir) / f"{digest}.jpg"
    if not preview.is_file():
        preview.parent.mkdir(parents=True, exist_ok=True)
        with PILImage.open(source) as image:
            image = image.convert("RGB")
            image.thumbnail((max_dim, max_dim), PILImage.Resampling.LANCZOS)
            image.save(preview, format="JPEG", quality=70, optimize=True)
    return preview


class HarnessError(RuntimeError):
    """Stable error raised for invalid or unsafe episode operations."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


MAX_POINTER_DELTA = 100.0
MAX_POINTER_REPEAT = 128
MAX_POINTER_BATCH_EVENTS = 256


@dataclass(frozen=True)
class Control:
    name: str
    type: Literal["key", "mouse_click", "mouse_move", "wait"]
    key: str | None = None
    description: str = ""
    x: float | None = None
    y: float | None = None
    dx: float | None = None
    dy: float | None = None
    button: Literal["left", "middle", "right"] = "left"
    duration_ms: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Control:
        if not isinstance(raw, dict):
            raise HarnessError("invalid_manifest", "each control must be an object")
        name = raw.get("name")
        kind = raw.get("type")
        if not isinstance(name, str) or not name or name.strip() != name:
            raise HarnessError("invalid_manifest", "each control needs a non-empty name")
        if kind not in {"key", "mouse_click", "mouse_move", "wait"}:
            raise HarnessError("invalid_manifest", f"unsupported control type: {kind!r}")
        key = raw.get("key")
        if kind == "key" and (not isinstance(key, str) or not key.strip()):
            raise HarnessError("invalid_manifest", f"key control {name!r} needs key")
        x = raw.get("x")
        y = raw.get("y")
        if kind == "mouse_click":
            if (
                not isinstance(x, (int, float))
                or isinstance(x, bool)
                or not isinstance(y, (int, float))
                or isinstance(y, bool)
                or not math.isfinite(float(x))
                or not math.isfinite(float(y))
            ):
                raise HarnessError("invalid_manifest", f"mouse control {name!r} needs x and y")
            if not (0 <= float(x) <= 1 and 0 <= float(y) <= 1):
                raise HarnessError("invalid_manifest", f"mouse control {name!r} coordinates must be in [0, 1]")
        dx = raw.get("dx")
        dy = raw.get("dy")
        if kind == "mouse_move":
            if (
                not isinstance(dx, (int, float))
                or isinstance(dx, bool)
                or not isinstance(dy, (int, float))
                or isinstance(dy, bool)
                or not math.isfinite(float(dx))
                or not math.isfinite(float(dy))
            ):
                raise HarnessError("invalid_manifest", f"mouse_move control {name!r} needs dx and dy")
            if abs(float(dx)) > 1000 or abs(float(dy)) > 1000:
                raise HarnessError("invalid_manifest", f"mouse_move control {name!r} delta is too large")
        button = raw.get("button", "left")
        if button not in {"left", "middle", "right"}:
            raise HarnessError("invalid_manifest", f"mouse control {name!r} has invalid button")
        duration = raw.get("duration_ms")
        if duration is not None and (type(duration) is not int or not 0 <= duration <= 10_000):
            raise HarnessError("invalid_manifest", f"control {name!r} has invalid duration_ms")
        return cls(
            name=name,
            type=kind,
            key=key,
            description=str(raw.get("description", "")),
            x=float(x) if x is not None else None,
            y=float(y) if y is not None else None,
            dx=float(dx) if dx is not None else None,
            dy=float(dy) if dy is not None else None,
            button=button,
            duration_ms=duration,
        )

    def public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"name": self.name, "type": self.type, "description": self.description}
        if self.key is not None:
            result["key"] = self.key
        if self.x is not None:
            result["x"] = self.x
        if self.y is not None:
            result["y"] = self.y
        if self.dx is not None:
            result["dx"] = self.dx
        if self.dy is not None:
            result["dy"] = self.dy
        if self.type == "mouse_click" and self.button != "left":
            result["button"] = self.button
        if self.duration_ms is not None:
            result["duration_ms"] = self.duration_ms
        return result


@dataclass(frozen=True)
class PointerEvent:
    """One relative pointer event in a dynamic input batch."""

    type: Literal["move", "click", "down", "up"]
    dx: float = 0.0
    dy: float = 0.0
    button: Literal["left", "middle", "right"] = "left"
    repeat: int = 1

    def public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.type}
        if self.type == "move":
            result.update({"dx": self.dx, "dy": self.dy})
            if self.repeat != 1:
                result["repeat"] = self.repeat
        else:
            result["button"] = self.button
        return result


@dataclass(frozen=True)
class GameSpec:
    root: Path
    entrypoint: str
    title: str
    canvas_selector: str
    ready_selector: str
    ready_check_script: str | None
    viewport_width: int
    viewport_height: int
    ready_timeout_ms: int
    ready_settle_ms: int
    action_window_ms: int
    max_steps: int
    episode_budget_ms: int | None
    min_reference_time_ms: int
    tick_ms: int
    recording_interval_ms: int
    post_action_settle_ms: int
    require_strong_pause: bool
    controls: tuple[Control, ...]

    @property
    def episode_budget_ticks(self) -> int | None:
        if self.episode_budget_ms is None:
            return None
        return self.episode_budget_ms // self.tick_ms

    @property
    def min_reference_ticks(self) -> int:
        return math.ceil(self.min_reference_time_ms / self.tick_ms)

    @classmethod
    def load(cls, path: str | Path) -> GameSpec:
        path = Path(path).resolve()
        try:
            raw = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HarnessError("invalid_manifest", f"cannot read game manifest {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise HarnessError("invalid_manifest", "game manifest must be a JSON object")
        root = path.parent
        viewport = raw.get("viewport", {})
        clock = raw.get("clock", {})
        if not isinstance(viewport, dict):
            raise HarnessError("invalid_manifest", "viewport must be an object")
        if not isinstance(clock, dict):
            raise HarnessError("invalid_manifest", "clock must be an object")

        def manifest_int(container: dict[str, Any], key: str, default: int) -> int:
            value = container.get(key, default)
            if type(value) is not int:
                raise HarnessError("invalid_manifest", f"{key} must be an integer")
            return value

        require_strong_pause = clock.get("require_strong_pause", True)
        if not isinstance(require_strong_pause, bool):
            raise HarnessError("invalid_manifest", "require_strong_pause must be a boolean")

        controls_raw = raw.get("controls", [])
        if not isinstance(controls_raw, list):
            raise HarnessError("invalid_manifest", "controls must be a list")
        controls = tuple(Control.from_dict(item) for item in controls_raw)
        names = [item.name for item in controls]
        if len(set(names)) != len(names):
            raise HarnessError("invalid_manifest", "control names must be unique")
        key_names = {item.name for item in controls if item.type == "key"}
        key_values = [item.key for item in controls if item.type == "key"]
        if len(set(key_values)) != len(key_values):
            raise HarnessError("invalid_manifest", "key controls must not share the same physical key")
        for name in names:
            for suffix in ("_down", "_up"):
                if name.endswith(suffix) and name[: -len(suffix)] in key_names:
                    raise HarnessError(
                        "invalid_manifest",
                        f"control name {name!r} collides with a generated persistent-key transition",
                    )
        ready_check_script = raw.get("ready_check_script")
        if ready_check_script is not None and not isinstance(ready_check_script, str):
            raise HarnessError("invalid_manifest", "ready_check_script must be a string when provided")
        try:
            spec = cls(
                root=root,
                entrypoint=str(raw.get("entrypoint", "index.html")),
                title=str(raw.get("title", path.stem)),
                canvas_selector=str(raw.get("canvas_selector", "canvas")),
                ready_selector=str(raw.get("ready_selector", raw.get("canvas_selector", "canvas:visible"))),
                ready_check_script=ready_check_script,
                viewport_width=manifest_int(viewport, "width", 1280),
                viewport_height=manifest_int(viewport, "height", 720),
                ready_timeout_ms=manifest_int(raw, "ready_timeout_ms", 120_000),
                ready_settle_ms=manifest_int(raw, "ready_settle_ms", 3_000),
                action_window_ms=manifest_int(raw, "action_window_ms", 250),
                max_steps=manifest_int(raw, "max_steps", 300),
                episode_budget_ms=(
                    manifest_int(raw, "episode_budget_ms", 0)
                    if "episode_budget_ms" in raw
                    else None
                ),
                min_reference_time_ms=manifest_int(raw, "min_reference_time_ms", 150_000),
                tick_ms=manifest_int(clock, "tick_ms", 50),
                recording_interval_ms=manifest_int(clock, "recording_interval_ms", 100),
                post_action_settle_ms=manifest_int(clock, "post_action_settle_ms", 50),
                require_strong_pause=require_strong_pause,
                controls=controls,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise HarnessError("invalid_manifest", f"manifest timing or viewport values are invalid: {exc}") from exc
        if not spec.entrypoint or Path(spec.entrypoint).is_absolute() or ".." in Path(spec.entrypoint).parts:
            raise HarnessError("invalid_manifest", "entrypoint must be a relative path inside the game root")
        if spec.viewport_width <= 0 or spec.viewport_height <= 0:
            raise HarnessError("invalid_manifest", "viewport dimensions must be positive")
        if (
            spec.ready_timeout_ms <= 0
            or spec.ready_settle_ms < 0
            or spec.action_window_ms < 0
            or spec.tick_ms <= 0
            or spec.recording_interval_ms <= 0
            or spec.post_action_settle_ms < 0
            or spec.max_steps <= 0
            or spec.min_reference_time_ms < 0
            or (
                spec.episode_budget_ms is not None
                and (
                    spec.episode_budget_ms <= 0
                    or spec.min_reference_time_ms > spec.episode_budget_ms
                    or (spec.episode_budget_ticks is not None and spec.episode_budget_ticks <= 0)
                    or (
                        spec.episode_budget_ticks is not None
                        and spec.min_reference_ticks > spec.episode_budget_ticks
                    )
                )
            )
        ):
            raise HarnessError("invalid_manifest", "settle time, action window, step, and episode timing must be valid")
        return spec

    def public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "title": self.title,
            "entrypoint": self.entrypoint,
            "viewport": {"width": self.viewport_width, "height": self.viewport_height},
            "input": {
                "pointer": {
                    "mode": "relative",
                    "coordinate_space": "canvas_css_pixels",
                    "initial_position": "center",
                    "max_delta": MAX_POINTER_DELTA,
                    "max_repeat": MAX_POINTER_REPEAT,
                    "max_batch_events": MAX_POINTER_BATCH_EVENTS,
                    "events": ["move", "click", "down", "up"],
                }
            },
            "controls": [control.public_dict() for control in self.controls],
            "action_window_ms": self.action_window_ms,
            "max_steps": self.max_steps,
            "min_reference_time_ms": self.min_reference_time_ms,
            "min_reference_ticks": self.min_reference_ticks,
            "clock": {
                "tick_ms": self.tick_ms,
                "recording_interval_ms": self.recording_interval_ms,
                "post_action_settle_ms": self.post_action_settle_ms,
                "require_strong_pause": self.require_strong_pause,
            },
        }
        if self.episode_budget_ms is not None:
            result["episode_budget_ms"] = self.episode_budget_ms
            result["episode_budget_ticks"] = self.episode_budget_ticks
        persistent_keys = {
            control.name: {"down": f"{control.name}_down", "up": f"{control.name}_up"}
            for control in self.controls
            if control.type == "key"
        }
        if persistent_keys:
            result["input_state"] = {"persistent_keys": persistent_keys}
        if self.ready_check_script is not None:
            result["ready_check_script"] = self.ready_check_script
        return result


@dataclass(frozen=True)
class Observation:
    step: int
    status: Literal["running", "won", "lost", "timeout", "fault"]
    image_path: Path
    image_sha256: str
    width: int
    height: int

    def public_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "status": self.status,
            "terminal": self.status in {"won", "lost", "timeout", "fault"},
            "image_path": str(self.image_path),
            "image_sha256": self.image_sha256,
            "width": self.width,
            "height": self.height,
        }


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def guess_type(self, path: str) -> str:
        lowered = path.lower()
        if lowered.endswith((".wasm", ".wasm.gz", ".wasm.br")):
            return "application/wasm"
        if lowered.endswith((".js", ".js.gz", ".js.br")):
            return "application/javascript"
        return super().guess_type(path)

    def end_headers(self) -> None:
        """Serve browser game bundles with the headers their loaders expect.

        Some S3 exports retain Unity's historical ``.gz``/``.br`` filename
        suffix even though the downloaded payload is already decompressed.
        We therefore do not invent a ``Content-Encoding`` header here.  The
        MIME overrides keep raw WebAssembly and JavaScript loadable, while
        the isolation headers are required by Godot's worker/audio runtime.
        """
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()


class _StaticServer:
    def __init__(self, root: Path):
        self.root = root
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: Thread | None = None

    def start(self) -> str:
        directory = str(self.root)

        def handler(*args: Any, **kwargs: Any) -> _QuietHandler:
            return _QuietHandler(*args, directory=directory, **kwargs)

        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
            self.thread.start()
        except OSError as exc:
            raise HarnessError("server_start_failed", str(exc)) from exc
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None


class _ChromiumProcess:
    def __init__(
        self,
        executable: str,
        profile_dir: Path,
        require_strong_pause: bool = True,
        window_size: tuple[int, int] | None = None,
    ):
        self.executable = executable
        self.profile_dir = profile_dir
        self.require_strong_pause = require_strong_pause
        self.window_size = window_size
        self.process: subprocess.Popen[bytes] | None = None
        self.debug_port: int | None = None
        self.paused = False

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    async def start(self) -> str:
        self.debug_port = self._free_port()
        command = [
            self.executable,
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-background-networking",
            f"--remote-debugging-port={self.debug_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.profile_dir}",
            "about:blank",
        ]
        # A browser connected over CDP owns a persistent default context, so
        # Playwright's set_viewport_size() is not reliable across Chromium
        # builds. Pin the outer headless window to the manifest viewport as a
        # fallback; startup also applies an explicit CDP viewport override to
        # keep Page.startScreencast from exposing a larger default surface.
        if self.window_size is not None:
            width, height = self.window_size
            command.insert(4, f"--window-size={int(width)},{int(height)}")
        # SwiftShader is a useful fallback in GPU-less containers, but forcing
        # it disables an available virtual GPU and can collapse a Unity/WebGL
        # render loop to a couple of frames per second.  Keep the historical
        # fallback available as an explicit opt-in while letting Chromium use
        # its detected backend by default.
        use_swiftshader = os.environ.get("WEBGAME_USE_SWIFTSHADER", "0").lower() in {
            "1",
            "true",
            "yes",
        }
        if use_swiftshader:
            command.insert(4, "--use-angle=swiftshader")
        scale = _configured_device_scale_factor()
        if scale != 1.0:
            command.insert(4, f"--force-device-scale-factor={scale:g}")
        try:
            self.process = await asyncio.to_thread(
                subprocess.Popen,
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise HarnessError("browser_start_failed", str(exc)) from exc
        endpoint = f"http://127.0.0.1:{self.debug_port}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise HarnessError("browser_start_failed", f"Chromium exited with {self.process.returncode}")
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", self.debug_port), timeout=0.2
                )
                writer.close()
                await writer.wait_closed()
                return endpoint
            except OSError:
                await asyncio.sleep(0.1)
            except asyncio.TimeoutError:
                await asyncio.sleep(0.1)
        raise HarnessError("browser_start_timeout", "Chromium did not open its debugging endpoint")

    def _group(self, *, allow_exited: bool = False) -> int:
        if self.process is None:
            raise HarnessError("browser_crashed", "Chromium is not running")
        if not allow_exited and self.process.poll() is not None:
            raise HarnessError("browser_crashed", "Chromium is not running")
        if allow_exited and self.process.returncode is not None:
            # Popen.poll() reaps the launcher.  Once reaped, its PID may be
            # reused, so never use it to signal a possibly unrelated group.
            raise HarnessError("browser_crashed", "Chromium launcher has already exited")
        try:
            group = os.getpgid(self.process.pid)
        except OSError as exc:
            raise HarnessError("browser_crashed", f"cannot inspect Chromium process group: {exc}") from exc
        if group in {os.getpgrp(), 0} or group != self.process.pid:
            raise HarnessError("unsafe_browser_process_group", "refusing to signal an unisolated Chromium process group")
        return group

    def _process_tree(self) -> set[int]:
        """Return Chromium and all currently discoverable descendants.

        Chromium normally keeps every child in the launcher's process group, but
        the pause contract is stronger than that implementation detail.  The
        tree lets us verify (and, when necessary, signal) helpers that create a
        separate process group.
        """
        if self.process is None:
            return set()
        root_pid = self.process.pid
        parents: dict[int, int] = {}
        proc = Path("/proc")
        try:
            entries = list(proc.iterdir())
        except OSError:
            return {root_pid}
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8")
                end_comm = stat.rfind(")")
                if end_comm < 0:
                    continue
                fields = stat[end_comm + 2 :].split()
                # After the comm field, fields are state, ppid, pgrp, ...
                if len(fields) >= 2:
                    parents[int(entry.name)] = int(fields[1])
            except (OSError, ValueError):
                continue
        tree = {root_pid}
        changed = True
        while changed:
            changed = False
            for pid, parent in parents.items():
                if parent in tree and pid not in tree:
                    tree.add(pid)
                    changed = True
        return tree

    @staticmethod
    def _state(pid: int) -> str | None:
        try:
            for line in (Path("/proc") / str(pid) / "status").read_text(encoding="utf-8").splitlines():
                if line.startswith("State:"):
                    return line.split()[1]
        except (OSError, IndexError):
            return None
        return None

    @staticmethod
    def _pgrp(pid: int) -> int | None:
        try:
            stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
            end_comm = stat.rfind(")")
            fields = stat[end_comm + 2 :].split()
            return int(fields[2]) if len(fields) >= 3 else None
        except (OSError, ValueError, IndexError):
            return None

    def _wait_for_state(self, *, stopped: bool, timeout_s: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            tree = self._process_tree()
            states = [self._state(pid) for pid in tree]
            states = [state for state in states if state is not None and state not in {"Z", "X"}]
            if states and all((state in {"T", "t"}) == stopped for state in states):
                return True
            time.sleep(0.005)
        return False

    def is_paused_confirmed(self) -> bool:
        """Verify the process-tree state instead of trusting the local flag.

        A signal sent by an unrelated supervisor (or a test fixture) can make
        ``self.paused`` stale.  The session uses this check at every model
        boundary so an episode never silently continues with a live browser.
        """
        if not self.paused or self.process is None or self.process.poll() is not None:
            return False
        return self._wait_for_state(stopped=True, timeout_s=0.05)

    def pause(self) -> None:
        if self.paused:
            if self.is_paused_confirmed():
                return
            # The local state was stale (for example, Chromium was externally
            # resumed).  Re-issue SIGSTOP rather than trusting the flag.
            self.paused = False
        group = self._group()
        tree = self._process_tree()
        try:
            os.killpg(group, signal.SIGSTOP)
        except OSError as exc:
            self.paused = False
            raise HarnessError("browser_crashed", f"could not pause Chromium: {exc}") from exc
        # Signal helpers outside the launch group individually.  They are
        # descendants of this isolated browser, so this does not widen the
        # signal boundary to unrelated processes.
        for pid in tree:
            if pid != self.process.pid and self._pgrp(pid) not in {None, group}:
                try:
                    os.kill(pid, signal.SIGSTOP)
                except OSError:
                    pass
        if not self.require_strong_pause:
            self.paused = True
            return
        confirmed = self._wait_for_state(stopped=True)
        if not confirmed and self.require_strong_pause:
            try:
                os.killpg(group, signal.SIGCONT)
            except OSError:
                pass
            for pid in tree:
                if pid != self.process.pid and self._pgrp(pid) not in {None, group}:
                    try:
                        os.kill(pid, signal.SIGCONT)
                    except OSError:
                        pass
            self.paused = False
            raise HarnessError("pause_not_confirmed", "Chromium process tree did not stop")
        self.paused = True

    def resume(self) -> None:
        if not self.paused:
            return
        group = self._group()
        tree = self._process_tree()
        try:
            os.killpg(group, signal.SIGCONT)
        except OSError as exc:
            self.paused = False
            raise HarnessError("browser_crashed", f"could not resume Chromium: {exc}") from exc
        for pid in tree:
            if pid != self.process.pid and self._pgrp(pid) not in {None, group}:
                try:
                    os.kill(pid, signal.SIGCONT)
                except OSError:
                    pass
        if not self.require_strong_pause:
            self.paused = False
            return
        confirmed = self._wait_for_state(stopped=False)
        if not confirmed and self.require_strong_pause:
            # Do not leave a failed transition looking paused.  The caller's
            # finally block will attempt a fresh SIGSTOP before returning.
            self.paused = False
            raise HarnessError("resume_not_confirmed", "Chromium process tree did not resume")
        self.paused = False

    def close(self) -> None:
        if self.process is None:
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            return
        process = self.process
        group: int | None = None
        tree: set[int] = set()
        try:
            # SIGCONT is harmless for a running process and guarantees that a
            # partially failed pause cannot leave a browser helper stopped
            # while the parent is being torn down.
            group = self._group(allow_exited=True)
            tree = self._process_tree()
            os.killpg(group, signal.SIGCONT)
            for pid in tree:
                if pid != self.process.pid and self._pgrp(pid) not in {None, group}:
                    try:
                        os.kill(pid, signal.SIGCONT)
                    except OSError:
                        pass
            self.paused = False
        except (HarnessError, OSError):
            # The launcher may have exited between the liveness check and the
            # signal.  We still clear the handle/profile below.
            self.paused = False
        # Terminate the isolated process group, not only Chromium's launcher.
        # Helpers that deliberately changed process groups are still limited to
        # the descendant set captured above.
        if group is not None:
            try:
                os.killpg(group, signal.SIGTERM)
            except OSError:
                pass
        for pid in tree:
            if pid != process.pid and self._pgrp(pid) not in {None, group}:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        if process.poll() is None:
            if group is None:
                # We refused group signalling because the process-group
                # identity was no longer safe to inspect.  Still terminate
                # the launcher itself without widening the signal boundary.
                try:
                    process.terminate()
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if group is not None:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except OSError:
                        pass
                else:
                    try:
                        process.kill()
                    except OSError:
                        pass
                for pid in tree:
                    if pid != process.pid and self._pgrp(pid) not in {None, group}:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        # A helper in a separate process group can outlive the launcher even
        # when the launcher's wait completed promptly.  Re-check the captured
        # descendants independently and forcefully reap any survivors.
        def alive(pid: int) -> bool:
            state = self._state(pid)
            return state is not None and state not in {"Z", "X"}

        same_group_survivor = False
        for pid in tree:
            if pid == process.pid or not alive(pid):
                continue
            if group is not None and self._pgrp(pid) == group:
                same_group_survivor = True
            elif self._pgrp(pid) not in {None, group}:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        if same_group_survivor and group is not None:
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass
        self.process = None
        shutil.rmtree(self.profile_dir, ignore_errors=True)


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


def _calibration() -> dict[str, Any] | None:
    """Return the task's chance floor and ceiling, or None when uncalibrated.

    Same file and same guards as the verifier, so the score the agent optimizes
    against is the score it is graded on.
    """
    if _CALIBRATION_CACHE:
        return _CALIBRATION_CACHE[0]
    loaded: dict[str, Any] | None = None
    try:
        raw = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
        floor, ceiling = raw.get("floor"), raw.get("ceiling")
        if (
            isinstance(floor, (int, float))
            and isinstance(ceiling, (int, float))
            and math.isfinite(floor)
            and math.isfinite(ceiling)
            and ceiling - floor > MIN_CALIBRATION_SPAN
        ):
            loaded = {"floor": float(floor), "ceiling": float(ceiling)}
    except (OSError, ValueError, TypeError):
        loaded = None
    _CALIBRATION_CACHE.append(loaded)
    return loaded


def _calibrate(score: float, calibration: dict[str, Any] | None) -> float:
    """Rescale a raw reward onto the task's chance-to-ceiling range."""
    if calibration is None:
        return score
    span = calibration["ceiling"] - calibration["floor"]
    return max(0.0, min(1.0, (score - calibration["floor"]) / span))


def _pixel_similarity(reference_path: Path, candidate_path: Path) -> float:
    """Return the verifier-compatible strict RGB pixel score for two PNGs."""
    try:
        with (
            PILImage.open(reference_path) as reference_image,
            PILImage.open(candidate_path) as candidate_image,
        ):
            reference = reference_image.convert("RGB")
            candidate = candidate_image.convert("RGB")
            if reference.size != candidate.size:
                return 0.0
            rms = ImageStat.Stat(ImageChops.difference(reference, candidate)).rms
            mean_squared_error = sum(channel_rms**2 for channel_rms in rms) / 3.0
            return max(0.0, min(1.0, math.exp(-mean_squared_error / (2.0 * PIXEL_RMS_SCALE**2))))
    except (OSError, ValueError):
        return 0.0


def _ssim_similarity(reference_path: Path, candidate_path: Path) -> float:
    """Return a standard grayscale, windowed SSIM score in ``[0, 1]``.

    The verifier and the MCP diagnostic intentionally use the same bounded
    implementation. Images are reduced to at most 320 pixels wide before an
    11x11-equivalent Gaussian window is applied; this keeps the metric cheap
    enough for repeated in-game feedback while preserving structural changes.
    """
    try:
        with (
            PILImage.open(reference_path) as reference_image,
            PILImage.open(candidate_path) as candidate_image,
        ):
            if reference_image.size != candidate_image.size:
                return 0.0
            if reference_image.convert("RGB").tobytes() == candidate_image.convert("RGB").tobytes():
                return 1.0
            width, height = reference_image.size
            sample_width = min(320, width)
            sample_height = max(1, round(height * sample_width / width))
            reference = reference_image.convert("L").resize(
                (sample_width, sample_height), PILImage.Resampling.BILINEAR
            )
            candidate = candidate_image.convert("L").resize(
                (sample_width, sample_height), PILImage.Resampling.BILINEAR
            )
            blur_reference = reference.filter(ImageFilter.GaussianBlur(1.5))
            blur_candidate = candidate.filter(ImageFilter.GaussianBlur(1.5))
            square_reference = reference.point(lambda value: (value * value + 127) // 255)
            square_candidate = candidate.point(lambda value: (value * value + 127) // 255)
            product = ImageChops.multiply(reference, candidate)
            blur_square_reference = square_reference.filter(ImageFilter.GaussianBlur(1.5))
            blur_square_candidate = square_candidate.filter(ImageFilter.GaussianBlur(1.5))
            blur_product = product.filter(ImageFilter.GaussianBlur(1.5))
            c1 = (0.01 * 255) ** 2
            c2 = (0.03 * 255) ** 2
            values = []
            for mean_reference, mean_candidate, second_reference, second_candidate, cross in zip(
                blur_reference.getdata(),
                blur_candidate.getdata(),
                blur_square_reference.getdata(),
                blur_square_candidate.getdata(),
                blur_product.getdata(),
            ):
                variance_reference = max(0.0, second_reference - mean_reference * mean_reference / 255.0)
                variance_candidate = max(0.0, second_candidate - mean_candidate * mean_candidate / 255.0)
                covariance = cross - mean_reference * mean_candidate / 255.0
                numerator = (2.0 * mean_reference * mean_candidate + c1) * (2.0 * covariance + c2)
                denominator = (
                    (mean_reference * mean_reference + mean_candidate * mean_candidate + c1)
                    * (variance_reference + variance_candidate + c2)
                )
                values.append(numerator / denominator if denominator else 1.0)
            return max(0.0, min(1.0, sum(values) / len(values)))
    except (OSError, ValueError, ZeroDivisionError):
        return 0.0


def _combined_similarity(reference_path: Path, candidate_path: Path) -> VisualScore:
    """Return the fixed additive production score and all diagnostics."""
    return score_pair(reference_path, candidate_path)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _frame_signature(data: bytes) -> tuple[int, int, bytes] | None:
    """Return a small signature for the static page chrome at the frame edge."""
    try:
        with PILImage.open(io.BytesIO(data)) as image:
            image.load()
            image = image.convert("RGB")
            width, height = image.size
            strip_height = min(96, height)
            strip = image.crop((0, height - strip_height, width, height))
            signature = strip.resize((64, 8), PILImage.Resampling.BILINEAR)
            return width, height, signature.tobytes()
    except (OSError, ValueError):
        return None


def _normalize_viewport_frame(data: bytes, expected_size: tuple[int, int]) -> bytes | None:
    """Resize same-aspect captures to the episode's locked replay geometry."""
    signature = _frame_signature(data)
    if signature is None:
        return None
    width, height = signature[:2]
    expected_width, expected_height = expected_size
    if (width, height) == expected_size:
        return data
    if width * expected_height != height * expected_width:
        return None
    try:
        with PILImage.open(io.BytesIO(data)) as image:
            image = image.convert("RGB").resize(expected_size, PILImage.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90, optimize=True)
            return output.getvalue()
    except (OSError, ValueError):
        return None


def _viewport_frame_is_valid(
    data: bytes,
    expected_size: tuple[int, int],
    reference: tuple[int, int, bytes] | None,
) -> bool:
    """Reject canvas-only/partial compositor frames from the viewport replay."""
    signature = _frame_signature(data)
    if signature is None or signature[:2] != expected_size:
        return False
    if reference is None or reference[:2] != expected_size:
        return True
    current = signature[2]
    expected = reference[2]
    if len(current) != len(expected):
        return False
    mean_abs_diff = sum(abs(left - right) for left, right in zip(current, expected)) / len(current)
    # Stable page margin/chrome pixels are well below this threshold under
    # JPEG noise; a capture surface that switches to a stretched canvas
    # differs by hundreds of levels.
    return mean_abs_diff <= 60.0


def _viewport_has_page_chrome(canvas_box: dict[str, float] | None, viewport_size: tuple[int, int]) -> bool:
    """Return whether the canvas leaves visible page chrome in the viewport."""
    if not canvas_box:
        return False
    width, height = viewport_size
    epsilon = 1.0
    return (
        canvas_box["x"] > epsilon
        or canvas_box["y"] > epsilon
        or canvas_box["x"] + canvas_box["width"] < width - epsilon
        or canvas_box["y"] + canvas_box["height"] < height - epsilon
    )


def _configured_device_scale_factor() -> float:
    raw = os.environ.get("WEBGAME_DEVICE_SCALE_FACTOR", "").strip()
    if not raw:
        return 1.0
    try:
        scale = float(raw)
    except ValueError:
        return 1.0
    return scale if math.isfinite(scale) and 0.1 <= scale <= 4.0 else 1.0


class GameSession:
    """One serialized, recorded browser episode."""

    def __init__(self, spec: GameSpec, run_dir: str | Path, chromium_bin: str | None = None):
        self.spec = spec
        # The manifest value describes the private builder's reference window.
        # Finalized solver tasks override this to zero so a solver may capture
        # any frozen frame; the Yggdrasil authoring gate still enforces the
        # builder's candidate-pool and visual-selection checks on the host.
        self.capture_min_reference_time_ms = self._read_capture_min_time()
        self.run_dir = Path(run_dir).resolve()
        self.frames_dir = self.run_dir / "frames"
        self.candidates_dir = self.run_dir / "candidates"
        self.submissions_dir = self.run_dir / "submissions"
        self.events_path = self.run_dir / "trajectory.jsonl"
        self.candidates_path = self.run_dir / "candidates.json"
        self.submission_path = self.run_dir / "submission.json"
        self.replay_path = self.run_dir / "replay.json"
        self.manifest_path = self.run_dir / "manifest.json"
        self.result_path = self.run_dir / "result.json"
        self.video_path = self.run_dir / "gameplay.mp4"
        self.video_timeline_path = self.run_dir / "gameplay_timeline.json"
        self.server: _StaticServer | None = None
        self.chrome: _ChromiumProcess | None = None
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.cdp: Any = None
        self._screencast_started = False
        # Keep every video sample in one image codec.  JPEG is substantially
        # cheaper than PNG for a live 640p/720p compositor stream; observations
        # remain lossless PNGs, but replay samples must not make Chromium spend
        # most of each action compressing screenshots.
        self._screencast_format = "jpeg"
        self._screencast_quality = 75
        self._screencast_handler = self._on_screencast_frame
        self._screencast_listener_registered = False
        self._screencast_context: dict[str, Any] | None = None
        self._screencast_tasks: set[asyncio.Task[Any]] = set()
        self._screencast_write_lock = asyncio.Lock()
        self._viewport_reference_signature: tuple[int, int, bytes] | None = None
        self._last_valid_viewport_frame: Path | None = None
        self._viewport_frame_size: tuple[int, int] | None = None
        # ``viewport`` is the universally-safe capture source.  ``canvas``
        # opts into MediaRecorder/canvas.captureStream() for canvas games;
        # ``auto`` tries that source and falls back to the viewport stream if
        # the browser or game does not support it.  The mode is deliberately
        # an environment setting so games do not need to change their page
        # layout just to make a recording.
        capture_mode = os.environ.get("WEBGAME_CAPTURE_MODE", "viewport").strip().lower()
        if capture_mode not in {"viewport", "canvas", "auto"}:
            capture_mode = "viewport"
        self.capture_mode = capture_mode
        self.canvas_capture_requested = capture_mode in {"canvas", "auto"}
        self.canvas_capture_available: bool | None = None
        self._canvas_capture_active = False
        self._canvas_capture_context: dict[str, Any] | None = None
        self._canvas_video_segments: list[dict[str, Any]] = []
        self._canvas_capture_failed = False
        self._canvas_capture_fps = self._read_capture_fps()
        self._video_fps = self._read_video_fps()
        self.canvas_video_dir = self.run_dir / "canvas-video"
        self.canvas_box: dict[str, float] | None = None
        self.pointer_position: tuple[float, float] | None = None
        self.pointer_locked = False
        self.base_url: str | None = None
        self.last_observation: Observation | None = None
        self.status: Literal["running", "won", "lost", "timeout", "fault"] = "running"
        self.step_count = 0
        self.frame_count = 0
        self.started = False
        self.closed = False
        self._lock = asyncio.Lock()
        self._actions: list[dict[str, Any]] = []
        self._candidates: dict[str, dict[str, Any]] = {}
        self._submissions: dict[str, dict[str, Any]] = {}
        self._video_segments: list[dict[str, Any]] = []
        self._pressed_keys: set[str] = set()
        self._pressed_buttons: set[str] = set()
        self.gameplay_ticks = 0
        self.gameplay_time_ms = 0
        self.episode_wall_started_ns: int | None = None
        self.active_wall_time_ms = 0.0
        self.wall_elapsed_at_close_ms: float | None = None
        self._close_result: dict[str, Any] | None = None
        self.chromium_bin = chromium_bin or os.environ.get("CHROMIUM_BIN", "chromium")

    @staticmethod
    def _read_capture_fps() -> int:
        raw = os.environ.get("WEBGAME_CANVAS_CAPTURE_FPS", "60").strip()
        try:
            value = int(raw)
        except ValueError:
            return 60
        return min(120, max(1, value))

    @staticmethod
    def _read_video_fps() -> int:
        raw = os.environ.get("WEBGAME_VIDEO_FPS", "30").strip()
        try:
            value = int(raw)
        except ValueError:
            return 30
        return min(120, max(1, value))

    def _viewport_capture_size(self) -> tuple[int, int]:
        if self._viewport_frame_size is not None:
            return self._viewport_frame_size
        scale = _configured_device_scale_factor()
        return (
            max(1, round(self.spec.viewport_width * scale)),
            max(1, round(self.spec.viewport_height * scale)),
        )

    def _read_capture_min_time(self) -> int:
        raw = os.environ.get("WEBGAME_CAPTURE_MIN_REFERENCE_TIME_MS")
        if raw is None or not raw.strip():
            return self.spec.min_reference_time_ms
        try:
            value = int(raw)
        except ValueError:
            return self.spec.min_reference_time_ms
        if self.spec.episode_budget_ms is None:
            return max(0, value)
        return max(0, min(self.spec.episode_budget_ms, value))

    @property
    def gameplay_tick(self) -> int:
        """Current deterministic simulation tick."""
        return self.gameplay_ticks

    @property
    def wall_elapsed_ms(self) -> float:
        if self.episode_wall_started_ns is None:
            return 0.0
        return (time.monotonic_ns() - self.episode_wall_started_ns) / 1_000_000

    @property
    def model_thinking_time_ms(self) -> float:
        """Wall time spent paused between active action windows."""
        return max(0.0, self.wall_elapsed_ms - self.active_wall_time_ms)

    def input_state(self) -> dict[str, Any]:
        """Return agent-visible pointer and keyboard state."""
        result: dict[str, Any] = {
            "keys_down": sorted(self._pressed_keys),
            "buttons_down": sorted(self._pressed_buttons),
            "pointer": None,
        }
        if self.pointer_locked:
            result["pointer"] = {"locked": True}
        elif self.canvas_box is not None:
            box = self.canvas_box
            pointer_position = self.pointer_position or (
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
            )
            result["pointer"] = {
                "locked": False,
                "x": round((pointer_position[0] - box["x"]) / box["width"], 6),
                "y": round((pointer_position[1] - box["y"]) / box["height"], 6),
            }
        return result

    def _write_submissions(self) -> None:
        """Persist the public submission manifest after every capture."""
        self.submission_path.write_text(
            json.dumps(
                {
                    "schema": "harbor-webgame/submission/1",
                    "references": self._submissions,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def _write_candidates(self) -> None:
        """Persist builder-only candidate snapshots after each snapshot."""
        self.candidates_path.write_text(
            json.dumps(
                {
                    "schema": "harbor-webgame/candidates/1",
                    "candidates": self._candidates,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def _event(self, payload: dict[str, Any]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        record = {"recorded_at": time.time(), "sequence": self.step_count, **payload}
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()

    def _pause_confirmed(self) -> bool:
        """Return whether Chromium is actually stopped at this boundary."""
        if self.chrome is None or not self.chrome.paused:
            return False
        if not self.spec.require_strong_pause:
            # Weak mode explicitly opts out of process-state verification for
            # environments where SIGSTOP cannot cover every helper.
            return True
        verifier = getattr(self.chrome, "is_paused_confirmed", None)
        return bool(verifier()) if verifier is not None else True

    async def _pause_confirmed_async(self) -> bool:
        """Run the /proc verification off the MCP event loop."""
        return await asyncio.to_thread(self._pause_confirmed)

    async def start(self) -> Observation:
        async with self._lock:
            if self.closed:
                raise HarnessError("closed", "session is already closed")
            if self.started:
                if self.last_observation is None:
                    raise HarnessError("internal_error", "started session has no observation")
                return self.last_observation
            if not self.spec.root.is_dir():
                raise HarnessError("game_not_found", f"game root does not exist: {self.spec.root}")
            entrypoint = (self.spec.root / self.spec.entrypoint).resolve()
            if not entrypoint.is_file() or self.spec.root not in entrypoint.parents:
                raise HarnessError("game_not_found", f"entrypoint does not exist under game root: {entrypoint}")

            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.frames_dir.mkdir(parents=True, exist_ok=True)
            self.candidates_dir.mkdir(parents=True, exist_ok=True)
            self.submissions_dir.mkdir(parents=True, exist_ok=True)
            self.manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "harbor-webgame/1",
                        "game": self.spec.public_dict(),
                        "pause_mode": "process_group",
                        "capture": {
                            "mode": self.capture_mode,
                            "canvas_capture_fps": self._canvas_capture_fps,
                            "video_fps": self._video_fps,
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            self.server = _StaticServer(self.spec.root)
            self.base_url = self.server.start()
            self.chrome = _ChromiumProcess(
                self.chromium_bin,
                Path(tempfile.mkdtemp(prefix="webgame-chromium-")),
                require_strong_pause=self.spec.require_strong_pause,
                window_size=(self.spec.viewport_width, self.spec.viewport_height),
            )
            try:
                endpoint = await self.chrome.start()
                from playwright.async_api import async_playwright

                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.connect_over_cdp(endpoint, timeout=30_000)
                if not self.browser.contexts:
                    raise HarnessError("browser_context_failed", "Chromium exposed no browser context")
                self.context = self.browser.contexts[0]
                self.page = await self.context.new_page()
                await self.page.set_viewport_size({"width": self.spec.viewport_width, "height": self.spec.viewport_height})
                # The CDP-connected default context can ignore Playwright's
                # viewport setter (notably with headless=new). Keep a CDP
                # session ready; the metrics override is applied immediately
                # after navigation because navigation resets emulation state.
                self.cdp = await self.context.new_cdp_session(self.page)
                url = self.base_url + "/" + urllib.parse.quote(self.spec.entrypoint, safe="/")
                await self.page.goto(url, wait_until="domcontentloaded", timeout=self.spec.ready_timeout_ms)
                await self.cdp.send(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": self.spec.viewport_width,
                        "height": self.spec.viewport_height,
                        "deviceScaleFactor": _configured_device_scale_factor(),
                        "mobile": False,
                    },
                )
                await self.page.locator(self.spec.ready_selector).first.wait_for(
                    state="visible", timeout=self.spec.ready_timeout_ms
                )
                if self.spec.ready_check_script is not None:
                    await self.page.wait_for_function(
                        self.spec.ready_check_script,
                        timeout=self.spec.ready_timeout_ms,
                    )
                await self.page.wait_for_timeout(self.spec.ready_settle_ms)
                self.last_observation = await self._capture_frame(observation_step=0)
                # Keep the replay stream consistently full-viewport even
                # though the agent-facing observation is clipped to the game
                # canvas.  This also ensures the first concat input has the
                # same dimensions as compositor screencast frames.
                try:
                    viewport_frame = await self._capture_viewport_frame()
                except HarnessError:
                    viewport_frame = None
                if viewport_frame is not None:
                    self._append_video_sample_path(viewport_frame, 0, None)
                if viewport_frame is not None and _viewport_has_page_chrome(
                    self.canvas_box,
                    (self.spec.viewport_width, self.spec.viewport_height),
                ):
                    try:
                        reference = _frame_signature(viewport_frame.read_bytes())
                        if reference is not None and reference[:2] == self._viewport_capture_size():
                            self._viewport_reference_signature = reference
                            self._last_valid_viewport_frame = viewport_frame
                    except OSError:
                        self._viewport_reference_signature = None
                # Establish the compositor stream while the page is settled.
                # Keeping this session alive across SIGSTOP/SIGCONT avoids a
                # surface re-selection at every agent turn.
                await self._start_screencast()
                # Freeze immediately after the first capture.  CDP queries
                # (including screenshot and bounding-box calls) cannot finish
                # while Chromium's renderer is SIGSTOP'd.
                await asyncio.to_thread(self.chrome.pause)
                if not await self._pause_confirmed_async():
                    raise HarnessError("pause_not_confirmed", "Chromium did not remain stopped after startup")
                self._event(
                    {
                        "phase": "start",
                        "observation": self.last_observation.public_dict(),
                        "pause_confirmed": True,
                    }
                )
                self.episode_wall_started_ns = time.monotonic_ns()
                self.started = True
                return self.last_observation
            except HarnessError:
                await self._close_browser()
                if self.server is not None:
                    self.server.close()
                    self.server = None
                raise
            except Exception as exc:
                await self._close_browser()
                if self.server is not None:
                    self.server.close()
                    self.server = None
                raise HarnessError("page_load_failed", str(exc)) from exc

    async def observe(self) -> Observation:
        if not self.started:
            return await self.start()
        async with self._lock:
            if self.closed or self.last_observation is None:
                raise HarnessError("closed", "session is not available")
            if not await self._pause_confirmed_async():
                raise HarnessError("pause_not_confirmed", "game was not frozen before observation")
            self._event(
                {
                    "phase": "observe",
                    "observation": self.last_observation.public_dict(),
                    "pause_confirmed": True,
                    "simulation_ticks": self.gameplay_ticks,
                    "gameplay_time_ms": self.gameplay_time_ms,
                    "active_wall_time_ms": round(self.active_wall_time_ms, 2),
                    "wall_elapsed_ms": round(self.wall_elapsed_ms, 2),
                    "model_thinking_time_ms": round(self.model_thinking_time_ms, 2),
                }
            )
            return self.last_observation

    async def similarity(
        self, reference_path: str | Path, reference_name: str
    ) -> dict[str, Any]:
        """Compare the latest frozen frame without submitting or advancing time."""
        if not self.started:
            await self.start()
        async with self._lock:
            if self.closed or self.last_observation is None:
                raise HarnessError("closed", "session is not available")
            if not await self._pause_confirmed_async():
                raise HarnessError(
                    "pause_not_confirmed", "game was not frozen before similarity check"
                )
            visual = await asyncio.to_thread(
                _combined_similarity,
                Path(reference_path),
                self.last_observation.image_path,
            )
            calibration = _calibration()
            # ``similarity`` is the graded value so the agent optimizes the score
            # it is judged on. ``similarity_raw`` is kept because calibration
            # clamps everything below chance to zero, which would otherwise hide
            # whether an early frame is getting closer.
            result = {
                "reference": reference_name,
                "similarity": round(_calibrate(visual.score, calibration), 8),
                "similarity_raw": round(visual.score, 8),
                "calibration": calibration,
                "score_profile": SCORE_PROFILE if calibration is None else f"{SCORE_PROFILE}-calibrated",
                "layout_structure_similarity": round(visual.layout_structure, 8),
                "phash_similarity": round(visual.phash, 8),
                "sift_geometry_similarity": round(visual.sift_geometry, 8),
                "pixel_similarity": round(visual.pixel, 8),
                "ssim": round(visual.ssim, 8),
                "step": self.last_observation.step,
                "gameplay_time_ms": self.gameplay_time_ms,
                "simulation_ticks": self.gameplay_ticks,
            }
            self._event({"phase": "similarity", **result, "pause_confirmed": True})
            return result

    def _control(self, name: str) -> Control:
        for control in self.spec.controls:
            if control.name == name:
                return control
        raise HarnessError("invalid_action", f"unknown action {name!r}")

    def _resolve_action(self, action: str) -> tuple[Control, Literal["hold", "down", "up"]]:
        """Resolve a normal control or a persistent key transition.

        Every key control automatically exposes ``<name>_down`` and
        ``<name>_up`` in the public manifest.  These transitions make it
        possible to compose movement, dash, and camera actions across calls
        while retaining the original one-shot action names.
        """
        if not isinstance(action, str) or not action.strip():
            raise HarnessError("invalid_action", "action must be a non-empty control name")
        name = action.strip()
        try:
            return self._control(name), "hold"
        except HarnessError:
            pass
        for suffix, transition in (("_down", "down"), ("_up", "up")):
            if name.endswith(suffix):
                base = name[: -len(suffix)]
                control = self._control(base)
                if control.type != "key":
                    raise HarnessError("invalid_action", f"persistent transitions require a key control: {base!r}")
                return control, transition  # type: ignore[return-value]
        raise HarnessError("invalid_action", f"unknown action {action!r}")

    @staticmethod
    def _finite_number(value: Any) -> bool:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(value))
        except (OverflowError, ValueError):
            return False

    def _parse_pointer_batch(self, raw_events: Any) -> tuple[PointerEvent, ...]:
        """Validate and normalize a bounded batch of relative pointer events."""
        if not isinstance(raw_events, list) or not raw_events:
            raise HarnessError("invalid_action", "pointer batch must be a non-empty list")
        events: list[PointerEvent] = []
        expanded_count = 0
        for index, raw in enumerate(raw_events):
            if not isinstance(raw, dict):
                raise HarnessError("invalid_action", f"pointer event {index} must be an object")
            kind = raw.get("type")
            if not isinstance(kind, str) or kind not in {"move", "click", "down", "up"}:
                raise HarnessError("invalid_action", f"pointer event {index} has invalid type")
            button = raw.get("button", "left")
            if not isinstance(button, str) or button not in {"left", "middle", "right"}:
                raise HarnessError("invalid_action", f"pointer event {index} has invalid button")
            repeat = raw.get("repeat", 1)
            if type(repeat) is not int or not 1 <= repeat <= MAX_POINTER_REPEAT:
                raise HarnessError(
                    "invalid_action",
                    f"pointer event {index} repeat must be an integer between 1 and {MAX_POINTER_REPEAT}",
                )
            if kind != "move" and repeat != 1:
                raise HarnessError("invalid_action", f"pointer event {index} repeat is only supported for move")
            if kind == "move":
                dx = raw.get("dx")
                dy = raw.get("dy")
                if not self._finite_number(dx) or not self._finite_number(dy):
                    raise HarnessError("invalid_action", f"pointer event {index} move needs finite dx and dy")
                dx_float = float(dx)
                dy_float = float(dy)
                if abs(dx_float) > MAX_POINTER_DELTA or abs(dy_float) > MAX_POINTER_DELTA:
                    raise HarnessError(
                        "invalid_action",
                        f"pointer event {index} delta must be at most {MAX_POINTER_DELTA:g} pixels",
                    )
            else:
                dx_float = 0.0
                dy_float = 0.0
            expanded_count += repeat
            if expanded_count > MAX_POINTER_BATCH_EVENTS:
                raise HarnessError(
                    "invalid_action",
                    f"pointer batch expands to at most {MAX_POINTER_BATCH_EVENTS} events",
                )
            events.append(
                PointerEvent(
                    type=kind,
                    dx=dx_float,
                    dy=dy_float,
                    button=button,
                    repeat=repeat,
                )
            )
        return tuple(events)

    def _append_video_sample(self, observation: Observation, gameplay_time_ms: int, action: str | None) -> None:
        self._append_video_sample_path(observation.image_path, gameplay_time_ms, action)

    def _append_video_sample_path(
        self,
        frame_path: Path,
        gameplay_time_ms: int,
        action: str | None,
        *,
        timestamp_ms: int | None = None,
        simulation_tick: int | None = None,
    ) -> None:
        # ``gameplay_time_ms`` is the deterministic simulation clock. Video
        # timestamps may be finer-grained wall-clock samples from the live
        # action window, so keep both values instead of quantizing the replay
        # timeline to simulation ticks.
        if timestamp_ms is None:
            timestamp_ms = gameplay_time_ms
        if simulation_tick is None:
            simulation_tick = gameplay_time_ms // self.spec.tick_ms
        self._video_segments.append(
            {
                "frame": str(frame_path),
                "timestamp_ms": int(timestamp_ms),
                "simulation_tick": int(simulation_tick),
                "gameplay_time_ms": int(gameplay_time_ms),
                "action": action,
                "wall_elapsed_ms": round(self.wall_elapsed_ms, 2),
            }
        )

    async def _on_screencast_frame(self, payload: dict[str, Any]) -> None:
        """Acknowledge and persist one browser screencast frame.

        This callback is intentionally independent from ``_capture_frame``.
        PNG screenshots are expensive on software-rendered Chromium; the
        screencast stream lets Chrome encode frames continuously without making
        the action loop wait for each frame. Replay samples use one configured
        image codec (JPEG by default) so the FFmpeg concat input has one
        decoder for every sample.
        """
        task = asyncio.current_task()
        if task is not None:
            self._screencast_tasks.add(task)
        try:
            # Capture the action context before acknowledging the event. The
            # acknowledgement can yield long enough for the next turn to
            # replace the shared context.
            context = self._screencast_context
            cdp = self.cdp
            session_id = payload.get("sessionId")
            if cdp is not None and session_id is not None:
                try:
                    await asyncio.wait_for(
                        cdp.send("Page.screencastFrameAck", {"sessionId": session_id}),
                        timeout=1.0,
                    )
                except Exception:
                    return
            if context is None:
                return
            encoded = payload.get("data")
            if not isinstance(encoded, str):
                return
            data = base64.b64decode(encoded)
            data = _normalize_viewport_frame(data, self._viewport_capture_size())
            if data is None:
                return
            if not _viewport_frame_is_valid(
                data,
                self._viewport_capture_size(),
                self._viewport_reference_signature,
            ):
                return
            metadata = payload.get("metadata")
            capture_timestamp = metadata.get("timestamp") if isinstance(metadata, dict) else None
            if isinstance(capture_timestamp, (int, float)) and math.isfinite(capture_timestamp):
                origin = context.get("capture_timestamp")
                if origin is None:
                    context["capture_timestamp"] = float(capture_timestamp)
                    elapsed_ms = 0
                else:
                    elapsed_ms = round((float(capture_timestamp) - float(origin)) * 1000)
            else:
                elapsed_ms = round((time.monotonic_ns() - int(context["started_ns"])) / 1_000_000)
            elapsed_ms = min(
                int(context["duration_ms"]),
                max(0, elapsed_ms),
            )
            duration_ms = int(context["duration_ms"])
            elapsed_ticks = (
                min(max(1, math.ceil(elapsed_ms / self.spec.tick_ms)), math.ceil(duration_ms / self.spec.tick_ms))
                if duration_ms
                else 0
            )
            gameplay_time_ms = int(context["start_gameplay_time_ms"]) + elapsed_ticks * self.spec.tick_ms
            timestamp_ms = int(context["start_gameplay_time_ms"]) + elapsed_ms
            async with self._screencast_write_lock:
                self.frame_count += 1
                frame_path = self.frames_dir / f"{self.frame_count:06d}.{self._screencast_format}"
                await asyncio.to_thread(frame_path.write_bytes, data)
                self._last_valid_viewport_frame = frame_path
                self._append_video_sample_path(
                    frame_path,
                    gameplay_time_ms,
                    context["action"],
                    timestamp_ms=timestamp_ms,
                    simulation_tick=gameplay_time_ms // self.spec.tick_ms,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A late frame during teardown must not poison the action that
            # already completed.  The final PNG observation remains the
            # fallback artifact if a screencast frame cannot be persisted.
            return
        finally:
            if task is not None:
                self._screencast_tasks.discard(task)

    async def _start_screencast(self) -> None:
        if self.cdp is None or self._screencast_started:
            return
        # Page.startScreencast captures the browser surface rather than a
        # particular DOM element.  Always use it when available: a canvas may
        # be centered, letterboxed, transformed, or accompanied by UI and
        # should not have to be rewritten just to unlock high-rate recording.
        # Frozen observations continue to use the canvas clip in
        # ``_capture_frame``; the replay intentionally represents the full
        # viewport.
        if not self._screencast_listener_registered:
            self.cdp.on("Page.screencastFrame", self._screencast_handler)
            self._screencast_listener_registered = True
        try:
            scale = _configured_device_scale_factor()
            await self.cdp.send(
                "Page.startScreencast",
                {
                    "format": self._screencast_format,
                    "quality": self._screencast_quality,
                    # CDP's screencast bounds are physical pixels, while the
                    # manifest viewport is CSS pixels. Keep event frames in
                    # the same geometry as full-surface screenshots when a
                    # non-1 device scale factor is explicitly requested.
                    "maxWidth": round(self.spec.viewport_width * scale),
                    "maxHeight": round(self.spec.viewport_height * scale),
                    "everyNthFrame": 1,
                },
            )
        except Exception:
            # Keep the interval JPEG sampler as a compatibility fallback for
            # Chromium builds that do not expose Page.startScreencast.
            try:
                self.cdp.remove_listener("Page.screencastFrame", self._screencast_handler)
            except Exception:
                pass
            self._screencast_listener_registered = False
            return
        self._screencast_started = True

    async def _start_canvas_recording(self, start_gameplay_time_ms: int, duration_ms: int, action: str) -> bool:
        """Start an optional direct canvas MediaRecorder for one action.

        The recorder lives in the page so the browser can encode the canvas
        frames without a screenshot round-trip through Python.  It is scoped
        to one active action; this keeps model-thinking pauses out of the
        media timestamps and avoids asking MediaRecorder to survive a frozen
        renderer.
        """
        if not self.canvas_capture_requested or self.page is None:
            return False
        if self.canvas_capture_available is False:
            return False
        script = """
            ({selector, fps}) => {
                const existing = window.__webgameCanvasRecorder;
                if (existing && existing.recorder && existing.recorder.state !== 'inactive') {
                    return {supported: false, reason: 'recorder_already_active'};
                }
                const canvas = document.querySelector(selector);
                if (!canvas || typeof canvas.captureStream !== 'function' ||
                    typeof MediaRecorder !== 'function') {
                    return {supported: false, reason: 'canvas_capture_unavailable'};
                }
                const candidates = [
                    'video/webm;codecs=vp9',
                    'video/webm;codecs=vp8',
                    'video/webm'
                ];
                const mimeType = candidates.find((candidate) =>
                    typeof MediaRecorder.isTypeSupported !== 'function' ||
                    MediaRecorder.isTypeSupported(candidate));
                if (!mimeType) {
                    return {supported: false, reason: 'no_supported_media_type'};
                }
                let stream;
                try {
                    stream = canvas.captureStream(Math.max(1, Math.min(120, Number(fps) || 60)));
                } catch (error) {
                    return {supported: false, reason: String(error)};
                }
                const chunks = [];
                let recorder;
                try {
                    recorder = new MediaRecorder(stream, {mimeType});
                    recorder.ondataavailable = (event) => {
                        if (event.data && event.data.size > 0) chunks.push(event.data);
                    };
                    recorder.start();
                } catch (error) {
                    for (const track of stream.getTracks()) track.stop();
                    return {supported: false, reason: String(error)};
                }
                window.__webgameCanvasRecorder = {recorder, stream, chunks, mimeType};
                return {supported: true, mimeType};
            }
        """
        try:
            result = await asyncio.wait_for(
                self.page.evaluate(
                    script,
                    {"selector": self.spec.canvas_selector, "fps": self._canvas_capture_fps},
                ),
                timeout=5.0,
            )
        except Exception:
            result = {"supported": False, "reason": "page_evaluation_failed"}
        supported = isinstance(result, dict) and result.get("supported") is True
        if not supported:
            self.canvas_capture_available = False
            # Do not later join a partial set of direct clips with the
            # compositor/image timeline. Once one action falls back, the
            # complete episode must use the same capture source.
            self._canvas_capture_failed = True
            if self.capture_mode == "canvas":
                # Direct capture is an optimization, not a reason to fail an
                # otherwise playable episode.  The caller will use the
                # compositor viewport stream instead.
                self._event(
                    {
                        "phase": "canvas_capture_fallback",
                        "reason": result.get("reason") if isinstance(result, dict) else None,
                    }
                )
            return False
        self.canvas_capture_available = True
        self.canvas_video_dir.mkdir(parents=True, exist_ok=True)
        self._canvas_capture_context = {
            "start_gameplay_time_ms": int(start_gameplay_time_ms),
            "duration_ms": int(duration_ms),
            "action": action,
            "started_ns": time.monotonic_ns(),
            "mime_type": result.get("mimeType"),
        }
        self._canvas_capture_active = True
        return True

    async def _stop_canvas_recording(self) -> None:
        """Stop the page recorder and persist its WebM clip, if any."""
        if not self._canvas_capture_active:
            self._canvas_capture_context = None
            return
        context = self._canvas_capture_context
        if self.page is None or context is None:
            self._canvas_capture_active = False
            self._canvas_capture_context = None
            self.canvas_capture_available = False
            self._canvas_capture_failed = True
            return
        script = """
            async () => {
                const state = window.__webgameCanvasRecorder;
                if (!state || !state.recorder) return null;
                const {recorder, stream, chunks, mimeType} = state;
                if (recorder.state !== 'inactive') {
                    await new Promise((resolve) => {
                        let finished = false;
                        const finish = () => {
                            if (finished) return;
                            finished = true;
                            resolve();
                        };
                        recorder.addEventListener('stop', finish, {once: true});
                        try {
                            recorder.stop();
                        } catch (_) {
                            finish();
                        }
                        // A broken encoder must not leave the page-side
                        // recorder alive forever. Any chunks already emitted
                        // still make a useful best-effort clip.
                        setTimeout(finish, 2000);
                    });
                }
                for (const track of stream.getTracks()) track.stop();
                window.__webgameCanvasRecorder = null;
                const blob = new Blob(chunks, {type: mimeType});
                const bytes = new Uint8Array(await blob.arrayBuffer());
                let binary = '';
                for (let offset = 0; offset < bytes.length; offset += 0x8000) {
                    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
                }
                return {mimeType, base64: btoa(binary)};
            }
        """
        try:
            result = await asyncio.wait_for(self.page.evaluate(script), timeout=10.0)
        except Exception:
            result = None
            # The main stop script may have timed out while MediaRecorder was
            # waiting for an encoder event. Clear the page-side state and stop
            # its tracks before allowing another action to start.
            try:
                await asyncio.wait_for(
                    self.page.evaluate(
                        """() => {
                            const state = window.__webgameCanvasRecorder;
                            window.__webgameCanvasRecorder = null;
                            if (!state) return;
                            try { if (state.recorder.state !== 'inactive') state.recorder.stop(); } catch (_) {}
                            for (const track of state.stream.getTracks()) track.stop();
                        }"""
                    ),
                    timeout=2.0,
                )
            except Exception:
                pass
        finally:
            self._canvas_capture_active = False
            self._canvas_capture_context = None
        encoded = result.get("base64") if isinstance(result, dict) else None
        if not isinstance(encoded, str) or not encoded:
            self.canvas_capture_available = False
            self._canvas_capture_failed = True
            return
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            self.canvas_capture_available = False
            self._canvas_capture_failed = True
            return
        if not data:
            self.canvas_capture_available = False
            self._canvas_capture_failed = True
            return
        elapsed_ms = min(
            int(context["duration_ms"]),
            max(1, round((time.monotonic_ns() - int(context["started_ns"])) / 1_000_000)),
        )
        clip_path = self.canvas_video_dir / f"{len(self._canvas_video_segments):06d}.webm"
        try:
            await asyncio.to_thread(clip_path.write_bytes, data)
        except OSError:
            self.canvas_capture_available = False
            self._canvas_capture_failed = True
            return
        self._canvas_video_segments.append(
            {
                "file": str(clip_path),
                "timestamp_ms": int(context["start_gameplay_time_ms"]),
                "duration_ms": elapsed_ms,
                "requested_duration_ms": int(context["duration_ms"]),
                "simulation_tick": int(context["start_gameplay_time_ms"]) // self.spec.tick_ms,
                "gameplay_time_ms": int(context["start_gameplay_time_ms"]),
                "action": context["action"],
                "wall_elapsed_ms": round(self.wall_elapsed_ms, 2),
            }
        )

    async def _finish_screencast_recording(self) -> None:
        tasks = tuple(self._screencast_tasks)
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                for task in tasks:
                    if not task.done():
                        task.cancel()
        self._screencast_context = None

    async def _stop_screencast(self) -> None:
        cdp = self.cdp
        if cdp is not None and self._screencast_started:
            try:
                await asyncio.wait_for(cdp.send("Page.stopScreencast"), timeout=2.0)
            except Exception:
                pass
        self._screencast_started = False
        await self._finish_screencast_recording()

    def _begin_screencast_recording(self, start_gameplay_time_ms: int, duration_ms: int, action: str) -> None:
        self._screencast_context = {
            "start_gameplay_time_ms": int(start_gameplay_time_ms),
            "duration_ms": int(duration_ms),
            "action": action,
            "started_ns": time.monotonic_ns(),
        }

    async def _capture_frame(self, observation_step: int | None = None) -> Observation:
        if self.page is None:
            raise HarnessError("browser_context_failed", "page is not ready")
        try:
            # Locator.screenshot waits for the element to be layout-stable.
            # Unity can repaint/resize its canvas continuously even when the
            # visible game state is valid, which makes that wait timeout in
            # headless Chromium. A clipped page capture takes the same pixels
            # without imposing a layout-stability requirement.
            box = self.canvas_box
            if box is None:
                target = self.page.locator(self.spec.canvas_selector).first
                measured = await target.bounding_box()
                if measured:
                    box = self.canvas_box = {key: float(value) for key, value in measured.items()}
            if not box or box["width"] <= 0 or box["height"] <= 0:
                raise HarnessError("frame_capture_failed", "game canvas has no visible bounding box")
            # Playwright's screenshot helper waits for document fonts to settle.
            # Unity pages can keep a font promise pending indefinitely while
            # the canvas is already fully rendered. CDP captures the surface
            # directly and therefore avoids making frame capture depend on a
            # page-level font/layout promise.
            if self.context is None:
                raise HarnessError("frame_capture_failed", "browser context is unavailable")
            if self.cdp is None:
                self.cdp = await self.context.new_cdp_session(self.page)
                await self._start_screencast()
            result = await asyncio.wait_for(
                self.cdp.send(
                    "Page.captureScreenshot",
                    {
                        "format": "png",
                        "fromSurface": True,
                        "captureBeyondViewport": True,
                        "clip": {
                            "x": box["x"],
                            "y": box["y"],
                            "width": box["width"],
                            "height": box["height"],
                            "scale": 1,
                        },
                    },
                ),
                # A deadlocked renderer must not hold the session forever.
                timeout=5.0,
            )
            data = base64.b64decode(result["data"])
        except Exception as exc:
            if isinstance(exc, HarnessError):
                raise
            raise HarnessError("frame_capture_failed", str(exc)) from exc
        self.frame_count += 1
        path = self.frames_dir / f"{self.frame_count:06d}.png"
        path.write_bytes(data)
        width, height = _png_dimensions(data)
        step = self.step_count if observation_step is None else observation_step
        return Observation(step, self.status, path, _sha256(data), width, height)

    async def _capture_viewport_frame(self) -> Path:
        """Capture one full viewport JPEG for the replay timeline.

        Agent observations intentionally clip to ``canvas_selector``. Replay
        samples use the browser surface so offset/letterboxed games do not
        force the event-driven screencast frames through a different image
        geometry at encode time.
        """
        if self.page is None:
            raise HarnessError("browser_context_failed", "page is not ready")
        try:
            if self.context is None:
                raise HarnessError("frame_capture_failed", "browser context is unavailable")
            if self.cdp is None:
                self.cdp = await self.context.new_cdp_session(self.page)
            result = await asyncio.wait_for(
                self.cdp.send(
                    "Page.captureScreenshot",
                    {
                        "format": self._screencast_format,
                        "quality": self._screencast_quality,
                        "fromSurface": True,
                        "captureBeyondViewport": False,
                    },
                ),
                timeout=5.0,
            )
            data = base64.b64decode(result["data"])
            expected_size = self._viewport_capture_size()
            data = _normalize_viewport_frame(data, expected_size)
            if data is None:
                raise HarnessError("frame_capture_failed", "viewport capture has incompatible geometry")
            if self._viewport_frame_size is None:
                self._viewport_frame_size = expected_size
            if not _viewport_frame_is_valid(data, self._viewport_capture_size(), self._viewport_reference_signature):
                raise HarnessError("frame_capture_failed", "viewport capture did not contain the full page")
        except HarnessError:
            raise
        except Exception as exc:
            raise HarnessError("frame_capture_failed", str(exc)) from exc
        self.frame_count += 1
        path = self.frames_dir / f"{self.frame_count:06d}.{self._screencast_format}"
        try:
            path.write_bytes(data)
        except OSError as exc:
            raise HarnessError("frame_capture_failed", str(exc)) from exc
        self._last_valid_viewport_frame = path
        return path

    async def _begin_control(self, control: Control, transition: Literal["hold", "down", "up"] = "hold") -> bool:
        if self.page is None:
            raise HarnessError("browser_context_failed", "page is not ready")
        if control.type == "key":
            assert control.key is not None
            if transition == "down":
                if control.key not in self._pressed_keys:
                    await self.page.keyboard.down(control.key)
                    self._pressed_keys.add(control.key)
                return False
            if transition == "up":
                if control.key in self._pressed_keys:
                    await self.page.keyboard.up(control.key)
                    self._pressed_keys.discard(control.key)
                return False
            temporary = control.key not in self._pressed_keys
            if temporary:
                await self.page.keyboard.down(control.key)
            return temporary
        elif control.type == "mouse_click":
            box = await self._canvas_bounds()
            assert control.x is not None and control.y is not None
            click_x = box["x"] + box["width"] * control.x
            click_y = box["y"] + box["height"] * control.y
            await self.page.mouse.click(click_x, click_y, button=control.button)
            self.pointer_position = (click_x, click_y)
            return False
        elif control.type == "mouse_move":
            assert control.dx is not None and control.dy is not None
            await self._move_pointer_by(control.dx, control.dy)
            return False
        else:
            return False

    async def _canvas_bounds(self) -> dict[str, float]:
        if self.page is None:
            raise HarnessError("browser_context_failed", "page is not ready")
        box = self.canvas_box
        if box is None:
            measured = await self.page.locator(self.spec.canvas_selector).first.bounding_box()
            if measured:
                box = self.canvas_box = {key: float(value) for key, value in measured.items()}
        if not box:
            raise HarnessError("input_failed", "game canvas has no bounding box")
        return box

    async def _refresh_canvas_bounds(self) -> dict[str, float]:
        """Refresh responsive canvas geometry before a dynamic input batch."""
        if self.page is None:
            raise HarnessError("browser_context_failed", "page is not ready")
        measured = await self.page.locator(self.spec.canvas_selector).first.bounding_box()
        if measured:
            self.canvas_box = {key: float(value) for key, value in measured.items()}
        return await self._canvas_bounds()

    async def _pointer_context(self) -> tuple[dict[str, float], tuple[float, float]]:
        box = await self._canvas_bounds()
        if self.pointer_position is None:
            self.pointer_position = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        return box, self.pointer_position

    async def _pointer_lock_active(self) -> bool:
        if self.page is None:
            return False
        try:
            self.pointer_locked = bool(await self.page.evaluate("() => document.pointerLockElement !== null"))
        except Exception:
            # Retain the last confirmed state if the page cannot answer during
            # an input transition. Cleanup still releases held buttons.
            pass
        return self.pointer_locked

    async def _move_pointer_by(self, dx: float, dy: float) -> None:
        box, (current_x, current_y) = await self._pointer_context()
        target_x = current_x + dx
        target_y = current_y + dy
        if not await self._pointer_lock_active():
            # Keep normal browser input on the canvas. Pointer-locked games are
            # deliberately not clamped: their movement deltas are unbounded.
            target_x = min(max(box["x"], target_x), box["x"] + box["width"])
            target_y = min(max(box["y"], target_y), box["y"] + box["height"])
        await self.page.mouse.move(target_x, target_y)
        self.pointer_position = (target_x, target_y)

    async def _release_pressed_buttons(self) -> None:
        if self.page is None:
            self._pressed_buttons.clear()
            return
        for button in tuple(self._pressed_buttons):
            try:
                await self.page.mouse.up(button=button)
            except Exception:
                pass
            self._pressed_buttons.discard(button)

    async def _begin_pointer_event(self, event: PointerEvent) -> None:
        """Dispatch one dynamic relative pointer event to the game canvas."""
        if self.page is None:
            raise HarnessError("browser_context_failed", "page is not ready")
        if event.type == "move":
            await self._move_pointer_by(event.dx, event.dy)
            return
        pointer_was_unset = self.pointer_position is None
        _, (current_x, current_y) = await self._pointer_context()
        if event.type == "click":
            await self.page.mouse.click(current_x, current_y, button=event.button)
        elif event.type == "down":
            if event.button not in self._pressed_buttons:
                if pointer_was_unset:
                    await self.page.mouse.move(current_x, current_y)
                await self.page.mouse.down(button=event.button)
                self._pressed_buttons.add(event.button)
        elif event.type == "up":
            if event.button in self._pressed_buttons:
                await self.page.mouse.up(button=event.button)
                self._pressed_buttons.discard(event.button)

    async def _run_pointer_batch(self, events: tuple[PointerEvent, ...]) -> None:
        """Dispatch a batch, expanding repeated moves without model boundaries."""
        try:
            await self._refresh_canvas_bounds()
            for event in events:
                for _ in range(event.repeat):
                    await self._begin_pointer_event(
                        PointerEvent(
                            type=event.type,
                            dx=event.dx,
                            dy=event.dy,
                            button=event.button,
                        )
                    )
        except Exception:
            # A fault ends the usable episode, so release every held button,
            # including buttons pressed by an earlier successful batch.
            await self._release_pressed_buttons()
            raise

    async def _end_control(self, control: Control, temporary: bool) -> None:
        if temporary and control.type == "key" and control.key is not None:
            await self.page.keyboard.up(control.key)

    async def _apply(self, control: Control, duration_ms: int, transition: Literal["hold", "down", "up"] = "hold") -> None:
        """Apply one control without recording intermediate frames.

        Kept as a small adapter for callers/tests that only need input
        semantics.  ``step`` uses ``_run_control`` so action-time frames are
        sampled while the browser is live.
        """
        temporary = await self._begin_control(control, transition)
        try:
            await asyncio.sleep(duration_ms / 1000)
        finally:
            await self._end_control(control, temporary)

    async def _run_control(
        self,
        control: Control,
        transition: Literal["hold", "down", "up"],
        duration_ms: int,
        action_name: str,
        observation_step: int,
        start_gameplay_time_ms: int,
    ) -> None:
        """Run an action and record samples at fixed wall-clock intervals."""
        temporary = await self._begin_control(control, transition)
        started_at = time.monotonic()
        deadline = started_at + duration_ms / 1000
        released = False
        release_task: asyncio.Task[None] | None = None

        async def release_at_deadline() -> None:
            nonlocal released
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            if temporary and not released:
                await self._end_control(control, temporary=True)
                released = True

        # Screenshot latency must never extend a requested key hold.  The
        # release timer runs independently while a CDP capture is in flight.
        if temporary and duration_ms > 0:
            release_task = asyncio.create_task(release_at_deadline())
        try:
            await self._run_action_window(
                duration_ms,
                action_name,
                observation_step,
                start_gameplay_time_ms,
            )
        finally:
            if release_task is not None:
                if not release_task.done():
                    release_task.cancel()
                try:
                    await release_task
                except asyncio.CancelledError:
                    pass
            if temporary and not released:
                await self._end_control(control, temporary)

    async def _run_action_window(
        self,
        duration_ms: int,
        action_name: str,
        observation_step: int,
        start_gameplay_time_ms: int,
    ) -> None:
        """Advance one active window and collect compatibility replay samples."""
        if duration_ms <= 0:
            return
        started_at = time.monotonic()
        deadline = started_at + duration_ms / 1000
        if self._screencast_started or self._canvas_capture_active:
            # The browser stream/MediaRecorder records independently while
            # this coroutine only holds the action window open.
            await asyncio.sleep(duration_ms / 1000)
            return
        # Compatibility fallback for Chromium builds without Page.startScreencast;
        # slower JPEG sampling is retained only when the event-driven stream
        # is unavailable.
        interval_s = self.spec.recording_interval_ms / 1000
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval_s, remaining))
            elapsed_ms = min(duration_ms, max(0, round((time.monotonic() - started_at) * 1000)))
            elapsed_ticks = min(
                max(1, math.ceil(elapsed_ms / self.spec.tick_ms)),
                math.ceil(duration_ms / self.spec.tick_ms),
            )
            # The compatibility sampler is also a replay source, so capture
            # the full viewport rather than mixing canvas clips with compositor
            # frames.
            try:
                sample_path = await self._capture_viewport_frame()
            except HarnessError:
                # Keep the replay on the same full-viewport source during a
                # transient capture failure.
                sample_path = self._last_valid_viewport_frame
            gameplay_time_ms = start_gameplay_time_ms + elapsed_ticks * self.spec.tick_ms
            if sample_path is not None:
                self._append_video_sample_path(
                    sample_path,
                    gameplay_time_ms,
                    action_name,
                    timestamp_ms=start_gameplay_time_ms + elapsed_ms,
                    simulation_tick=gameplay_time_ms // self.spec.tick_ms,
                )

    async def _step_with_runner(
        self,
        action: str,
        actual_duration: int,
        action_record: dict[str, Any],
        runner: Callable[[int, int], Awaitable[None]],
    ) -> Observation:
        """Run one serialized action through the shared browser/timing seam."""
        if self.closed:
            raise HarnessError("closed", "session is already closed")
        if self.status != "running":
            raise HarnessError("terminal", f"episode is {self.status}")
        if not await self._pause_confirmed_async():
            raise HarnessError("pause_not_confirmed", "game was not frozen before the action")
        if self.step_count >= self.spec.max_steps:
            self.status = "timeout"
            raise HarnessError("step_limit", "episode step limit reached")
        if type(actual_duration) is not int or not 0 <= actual_duration <= 10_000:
            raise HarnessError("invalid_action", "duration_ms must be an integer between 0 and 10000")
        action_ticks = math.ceil(actual_duration / self.spec.tick_ms) if actual_duration else 0
        settle_ticks = math.ceil(self.spec.post_action_settle_ms / self.spec.tick_ms) if self.spec.post_action_settle_ms else 0
        total_ticks = action_ticks + settle_ticks
        if (
            self.spec.episode_budget_ticks is not None
            and self.gameplay_ticks + total_ticks > self.spec.episode_budget_ticks
        ):
            self.status = "timeout"
            raise HarnessError("episode_time_limit", "episode gameplay budget reached")
        before = self.last_observation
        action_index = self.step_count + 1
        start_gameplay_time_ms = self.gameplay_time_ms
        started_at = time.monotonic()
        active_elapsed_ms = 0.0
        pause_error: HarnessError | None = None
        canvas_recording = False
        browser_resumed = False
        action_succeeded = False
        try:
            await asyncio.to_thread(self.chrome.resume)
            browser_resumed = True
            if self.canvas_capture_requested:
                await self._start_screencast()
            if not self.canvas_capture_requested or self._screencast_started:
                canvas_recording = await self._start_canvas_recording(
                    start_gameplay_time_ms,
                    actual_duration + self.spec.post_action_settle_ms,
                    action,
                )
            if not canvas_recording:
                if not self._screencast_started:
                    await self._start_screencast()
                self._begin_screencast_recording(
                    start_gameplay_time_ms,
                    actual_duration + self.spec.post_action_settle_ms,
                    action,
                )
            elif self._screencast_started:
                self._begin_screencast_recording(
                    start_gameplay_time_ms,
                    actual_duration + self.spec.post_action_settle_ms,
                    action,
                )
            await runner(action_index, start_gameplay_time_ms)
            if self.spec.post_action_settle_ms:
                await self.page.wait_for_timeout(self.spec.post_action_settle_ms)
            if canvas_recording:
                await self._stop_canvas_recording()
            try:
                viewport_after = await self._capture_viewport_frame()
            except HarnessError:
                viewport_after = None
            after = await self._capture_frame(observation_step=action_index)
            action_succeeded = True
        except HarnessError:
            self.status = "fault"
            if browser_resumed:
                await self._release_pressed_buttons()
            raise
        except Exception as exc:
            self.status = "fault"
            if browser_resumed:
                await self._release_pressed_buttons()
            raise HarnessError("step_failed", str(exc)) from exc
        finally:
            try:
                if self._canvas_capture_active:
                    await self._stop_canvas_recording()
                if not action_succeeded:
                    await self._stop_screencast()
                else:
                    # Drain while Chromium is still live. An in-flight CDP
                    # ACK cannot complete after SIGSTOP, which would add a
                    # teardown timeout to every successful turn.
                    await self._finish_screencast_recording()
            except Exception:
                pass
            try:
                await asyncio.to_thread(self.chrome.pause)
            except HarnessError as exc:
                self.status = "fault"
                pause_error = exc
            if not action_succeeded:
                await self._finish_screencast_recording()
            active_elapsed_ms = (time.monotonic() - started_at) * 1000
            self.active_wall_time_ms += active_elapsed_ms
        if pause_error is not None:
            raise pause_error
        if not await self._pause_confirmed_async():
            self.status = "fault"
            raise HarnessError("pause_not_confirmed", "game did not remain frozen after the action")
        self.last_observation = after
        self.step_count = action_index
        self.gameplay_ticks += total_ticks
        self.gameplay_time_ms = self.gameplay_ticks * self.spec.tick_ms
        sample_path = viewport_after or self._last_valid_viewport_frame
        if sample_path is not None:
            self._append_video_sample_path(sample_path, self.gameplay_time_ms, action)
        completed_action = {
            **action_record,
            "duration_ms": actual_duration,
            "simulation_ticks": total_ticks,
        }
        self._actions.append(completed_action)
        if (
            self.spec.episode_budget_ticks is not None
            and self.gameplay_ticks >= self.spec.episode_budget_ticks
        ):
            self.status = "timeout"
        event = {
            "phase": "step",
            "action": completed_action,
            "before": before.public_dict() if before else None,
            "after": after.public_dict(),
            "pause_confirmed": True,
            "gameplay_time_ms": self.gameplay_time_ms,
            "simulation_ticks": self.gameplay_ticks,
            "pressed_keys": sorted(self._pressed_keys),
            "pressed_buttons": sorted(self._pressed_buttons),
            "active_elapsed_ms": round(active_elapsed_ms, 2),
            "wall_elapsed_ms": round(self.wall_elapsed_ms, 2),
            "model_thinking_time_ms": round(self.model_thinking_time_ms, 2),
        }
        self._event(event)
        self.replay_path.write_text(json.dumps({"actions": self._actions}, indent=2) + "\n")
        return after

    async def step(self, action: str, duration_ms: int | None = None) -> Observation:
        # Do this outside the session lock: start() owns that lock while it
        # launches the server and browser.
        if not self.started:
            await self.start()
        async with self._lock:
            control, transition = self._resolve_action(action)
            actual_duration = duration_ms if duration_ms is not None else control.duration_ms
            if actual_duration is None:
                actual_duration = 0 if transition != "hold" else self.spec.action_window_ms

            async def run_control(action_index: int, start_gameplay_time_ms: int) -> None:
                await self._run_control(
                    control,
                    transition,
                    actual_duration,
                    action,
                    action_index,
                    start_gameplay_time_ms,
                )

            return await self._step_with_runner(
                action,
                actual_duration,
                {"name": action, "control": control.name, "transition": transition},
                run_control,
            )

    async def step_pointer(self, raw_events: Any, duration_ms: int | None = None) -> Observation:
        """Run a bounded batch of relative pointer events as one action."""
        if not self.started:
            await self.start()
        async with self._lock:
            events = self._parse_pointer_batch(raw_events)
            actual_duration = self.spec.action_window_ms if duration_ms is None else duration_ms

            async def run_pointer(action_index: int, start_gameplay_time_ms: int) -> None:
                await self._run_pointer_batch(events)
                await self._run_action_window(
                    actual_duration,
                    "pointer_batch",
                    action_index,
                    start_gameplay_time_ms,
                )

            return await self._step_with_runner(
                "pointer_batch",
                actual_duration,
                {"name": "pointer_batch", "events": [event.public_dict() for event in events]},
                run_pointer,
            )

    async def capture(self, reference: str) -> dict[str, Any]:
        """Save the current frozen observation as a named benchmark answer.

        Captures are copied from the most recent harness-owned frame rather
        than taking a new browser screenshot.  This keeps the operation
        deterministic and preserves the Lite invariant: a model may think,
        then mark exactly the frozen frame it selected.
        """
        if not isinstance(reference, str) or reference not in {
            "reference_01",
            "reference_02",
            "reference_03",
            "reference_04",
        }:
            raise HarnessError(
                "invalid_capture",
                "reference must be one of reference_01, reference_02, reference_03, reference_04",
            )
        if not self.started:
            await self.start()
        async with self._lock:
            if self.closed or self.last_observation is None:
                raise HarnessError("closed", "session is not available")
            if not await self._pause_confirmed_async():
                raise HarnessError("pause_not_confirmed", "game was not frozen before capture")
            if self.gameplay_time_ms < self.capture_min_reference_time_ms:
                raise HarnessError(
                    "capture_too_early",
                    f"reference frames may be captured only after {self.capture_min_reference_time_ms} ms of gameplay",
                )
            source = self.last_observation.image_path
            if not source.is_file():
                raise HarnessError("capture_failed", "latest observation frame is missing")
            target = self.submissions_dir / f"{reference}.png"
            target.write_bytes(source.read_bytes())
            record = {
                "name": reference,
                "step": self.last_observation.step,
                "simulation_ticks": self.gameplay_ticks,
                "gameplay_time_ms": self.gameplay_time_ms,
                "active_wall_time_ms": round(self.active_wall_time_ms, 2),
                "wall_elapsed_ms": round(self.wall_elapsed_ms, 2),
                "model_thinking_time_ms": round(self.model_thinking_time_ms, 2),
                "image_path": str(target),
                "image_sha256": self.last_observation.image_sha256,
            }
            self._submissions[reference] = record
            self._write_submissions()
            if not await self._pause_confirmed_async():
                self.status = "fault"
                raise HarnessError("pause_not_confirmed", "game did not remain frozen after capture")
            self._event(
                {
                    "phase": "capture",
                    "capture": record,
                    "pause_confirmed": True,
                }
            )
            return record

    @staticmethod
    def _candidate_index(candidate: str) -> int:
        """Validate and return the numeric suffix of a builder candidate."""
        match = re.fullmatch(r"candidate_(\d{2})", candidate) if isinstance(candidate, str) else None
        if match is None:
            raise HarnessError(
                "invalid_candidate",
                "candidate must be one of candidate_01 through candidate_20",
            )
        index = int(match.group(1))
        if not 1 <= index <= CANDIDATE_COUNT:
            raise HarnessError(
                "invalid_candidate",
                "candidate must be one of candidate_01 through candidate_20",
            )
        return index

    @staticmethod
    def _candidate_snapshots_enabled() -> bool:
        return os.environ.get("WEBGAME_ENABLE_CANDIDATE_SNAPSHOTS", "0").lower() in {
            "1",
            "true",
            "yes",
        }

    async def snapshot(self, candidate: str) -> dict[str, Any]:
        """Save one builder-only candidate frame without a timing gate.

        Candidate snapshots are intentionally separate from solver submissions:
        the private author can inspect a pool of twenty frames and then promote
        four with :meth:`select_candidate`. Finalized tasks disable this path
        entirely, leaving solvers with only the four named submission slots.
        """
        if not self._candidate_snapshots_enabled():
            raise HarnessError("candidate_snapshots_disabled", "candidate snapshots are available only to the private builder")
        index = self._candidate_index(candidate)
        if not self.started:
            await self.start()
        async with self._lock:
            if self.closed or self.last_observation is None:
                raise HarnessError("closed", "session is not available")
            if not await self._pause_confirmed_async():
                raise HarnessError("pause_not_confirmed", "game was not frozen before candidate snapshot")
            if candidate in self._candidates:
                raise HarnessError("duplicate_candidate", f"candidate {candidate} was already captured")
            source = self.last_observation.image_path
            if not source.is_file():
                raise HarnessError("capture_failed", "latest observation frame is missing")
            self.candidates_dir.mkdir(parents=True, exist_ok=True)
            target = self.candidates_dir / f"{candidate}.png"
            target.write_bytes(source.read_bytes())
            record = {
                "name": candidate,
                "index": index,
                "step": self.last_observation.step,
                "simulation_ticks": self.gameplay_ticks,
                "gameplay_time_ms": self.gameplay_time_ms,
                "active_wall_time_ms": round(self.active_wall_time_ms, 2),
                "wall_elapsed_ms": round(self.wall_elapsed_ms, 2),
                "model_thinking_time_ms": round(self.model_thinking_time_ms, 2),
                "image_path": str(target),
                "image_sha256": self.last_observation.image_sha256,
                "width": self.last_observation.width,
                "height": self.last_observation.height,
            }
            self._candidates[candidate] = record
            self._write_candidates()
            self._event({"phase": "candidate", "candidate": record, "pause_confirmed": True})
            return record

    async def select_candidate(self, candidate: str, reference: str) -> dict[str, Any]:
        """Promote one previously captured builder candidate into a slot."""
        if not self._candidate_snapshots_enabled():
            raise HarnessError("candidate_snapshots_disabled", "candidate selection is available only to the private builder")
        self._candidate_index(candidate)
        if not isinstance(reference, str) or reference not in {
            "reference_01",
            "reference_02",
            "reference_03",
            "reference_04",
        }:
            raise HarnessError("invalid_capture", "reference must be one of reference_01, reference_02, reference_03, reference_04")
        if not self.started:
            await self.start()
        async with self._lock:
            if self.closed:
                raise HarnessError("closed", "session is not available")
            if not await self._pause_confirmed_async():
                raise HarnessError("pause_not_confirmed", "game was not frozen before candidate selection")
            if candidate not in self._candidates:
                raise HarnessError("candidate_not_found", f"candidate {candidate} has not been captured")
            if reference in self._submissions:
                raise HarnessError("duplicate_capture", f"reference {reference} was already selected")
            source = self.candidates_dir / f"{candidate}.png"
            if not source.is_file() or source.is_symlink():
                raise HarnessError("capture_failed", f"candidate image is missing: {candidate}")
            self.submissions_dir.mkdir(parents=True, exist_ok=True)
            target = self.submissions_dir / f"{reference}.png"
            target.write_bytes(source.read_bytes())
            candidate_record = self._candidates[candidate]
            record = {
                "name": reference,
                "candidate_name": candidate,
                "step": candidate_record["step"],
                "simulation_ticks": candidate_record["simulation_ticks"],
                "gameplay_time_ms": candidate_record["gameplay_time_ms"],
                "active_wall_time_ms": candidate_record["active_wall_time_ms"],
                "wall_elapsed_ms": candidate_record["wall_elapsed_ms"],
                "model_thinking_time_ms": candidate_record["model_thinking_time_ms"],
                "image_path": str(target),
                "image_sha256": candidate_record["image_sha256"],
            }
            self._submissions[reference] = record
            self._write_submissions()
            self._event({"phase": "capture", "capture": record, "pause_confirmed": True})
            return record

    @staticmethod
    def _ffconcat_path(path: str) -> str:
        """Quote one absolute path for ffmpeg's concat demuxer."""
        return path.replace("'", "'\\''")

    @staticmethod
    def _probe_media_duration_ms(path: Path) -> int | None:
        """Read an encoded media duration, returning ``None`` on probe failure."""
        try:
            completed = subprocess.run(
                [
                    os.environ.get("FFPROBE_BIN", "ffprobe"),
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            value = float(completed.stdout.strip())
            if completed.returncode == 0 and math.isfinite(value) and value > 0:
                return max(1, round(value * 1000))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
        return None

    def _finalize_canvas_video(self) -> dict[str, Any] | None:
        """Encode direct canvas WebM clips into the standard MP4 artifact.

        Returning ``None`` asks the caller to use the compositor/image
        timeline instead.  A direct stream is an optimization and must never
        make an episode lose its ordinary replay when a browser codec or a
        game changes canvas dimensions between actions.
        """
        if not self._canvas_video_segments or self._canvas_capture_failed:
            return None
        valid_segments = [
            segment
            for segment in self._canvas_video_segments
            if Path(segment["file"]).is_file() and int(segment.get("duration_ms", 0)) > 0
        ]
        if len(valid_segments) != len(self._canvas_video_segments):
            # Never silently drop an action from a direct-capture replay. Use
            # the complete viewport timeline instead.
            self._canvas_capture_failed = True
            return None
        if not valid_segments:
            return None
        concat_path = self.run_dir / "gameplay-canvas.ffconcat"
        lines = ["ffconcat version 1.0"]
        for segment in valid_segments:
            # Unlike PNG samples, each WebM clip already carries its own
            # duration.  Supplying concat ``duration`` directives (or
            # repeating the final file) would count those durations twice.
            lines.append(f"file '{self._ffconcat_path(str(Path(segment['file'])))}'")
        try:
            concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            return None

        timeline_segments: list[dict[str, Any]] = []
        timestamp_ms = 0
        for segment in valid_segments:
            path = Path(segment["file"])
            requested_duration_ms = int(segment.get("requested_duration_ms", segment.get("duration_ms", 0)))
            duration_ms = self._probe_media_duration_ms(path)
            # MediaRecorder can finish a few milliseconds before the requested
            # action window, but a materially shorter clip means the direct
            # source lost part of an action. Keep the complete viewport
            # fallback instead of publishing a truncated direct replay.
            minimum_duration_ms = max(1, requested_duration_ms - self.spec.tick_ms)
            if duration_ms is None or duration_ms < minimum_duration_ms:
                self._canvas_capture_failed = True
                return None
            timeline_segments.append(
                {
                    **segment,
                    "frame": segment["file"],
                    "duration_ms": duration_ms,
                    "timestamp_ms": timestamp_ms,
                }
            )
            timestamp_ms += duration_ms
        self.video_timeline_path.parent.mkdir(parents=True, exist_ok=True)
        self.video_timeline_path.write_text(
            json.dumps(
                {
                    "schema": "harbor-webgame/video-timeline/1",
                    "capture_mode": "canvas",
                    "timestamp_timebase": "active_media_ms",
                    "tick_ms": self.spec.tick_ms,
                    "simulation_duration_ms": self.gameplay_time_ms,
                    "timeline_duration_ms": timestamp_ms,
                    "active_wall_time_ms": round(self.active_wall_time_ms, 2),
                    "wall_elapsed_ms": round(
                        self.wall_elapsed_at_close_ms if self.wall_elapsed_at_close_ms is not None else self.wall_elapsed_ms,
                        2,
                    ),
                    "model_thinking_time_ms": round(
                        max(
                            0.0,
                            (self.wall_elapsed_at_close_ms if self.wall_elapsed_at_close_ms is not None else self.wall_elapsed_ms)
                            - self.active_wall_time_ms,
                        ),
                        2,
                    ),
                    "segments": timeline_segments,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        command = [
            os.environ.get("FFMPEG_BIN", "ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-fps_mode",
            "cfr",
            "-vf",
            f"scale=trunc(iw/2)*2:trunc(ih/2)*2,fps={self._video_fps}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-bf",
            "0",
            "-movflags",
            "+faststart",
            str(self.video_path),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0 or not self.video_path.is_file():
            return None
        return {
            "video_path": str(self.video_path),
            "video_timeline_path": str(self.video_timeline_path),
            "video_duration_ms": sum(int(segment["duration_ms"]) for segment in timeline_segments),
            "video_capture_mode": "canvas",
        }

    def _finalize_video(self) -> dict[str, Any]:
        """Encode timestamped action-time samples as a portable MP4 artifact.

        The model still receives frozen observations, but samples captured
        while an action is running are retained for the replay.  Time spent
        paused while the model thinks is intentionally omitted from the video;
        it remains available in ``gameplay_timeline.json`` as wall-clock data.
        """
        direct_video = self._finalize_canvas_video()
        if direct_video is not None:
            return direct_video
        if not self._video_segments:
            return {"video_path": None, "video_error": "no frames were captured"}

        valid_samples = [
            segment
            for segment in self._video_segments
            if Path(segment["frame"]).is_file() and isinstance(segment.get("timestamp_ms"), int)
        ]
        if not valid_samples:
            return {"video_path": None, "video_error": "frame files are missing"}

        # Convert samples into forward durations.  A sample belongs to the
        # interval beginning at its timestamp; this fixes the old temporal
        # inversion where the post-action frame was displayed during the
        # action that produced it.  Duplicate timestamps are collapsed to the
        # newest sample, which is useful for zero-duration key transitions.
        samples: list[dict[str, Any]] = []
        for sample in sorted(valid_samples, key=lambda item: int(item["timestamp_ms"])):
            if samples and sample["timestamp_ms"] == samples[-1]["timestamp_ms"]:
                samples[-1] = sample
            else:
                samples.append(sample)
        end_time_ms = max(self.gameplay_time_ms, int(samples[-1]["timestamp_ms"])) + self.spec.tick_ms
        timeline_segments: list[dict[str, Any]] = []
        for index, sample in enumerate(samples):
            timestamp_ms = int(sample["timestamp_ms"])
            next_timestamp_ms = int(samples[index + 1]["timestamp_ms"]) if index + 1 < len(samples) else end_time_ms
            duration_ms = max(1, next_timestamp_ms - timestamp_ms)
            timeline_segments.append({**sample, "duration_ms": duration_ms})
        self.video_timeline_path.parent.mkdir(parents=True, exist_ok=True)
        self.video_timeline_path.write_text(
            json.dumps(
                {
                    "schema": "harbor-webgame/video-timeline/1",
                    "capture_mode": "viewport",
                    "timestamp_timebase": "gameplay_ms",
                    "tick_ms": self.spec.tick_ms,
                    "simulation_duration_ms": self.gameplay_time_ms,
                    "active_wall_time_ms": round(self.active_wall_time_ms, 2),
                    "wall_elapsed_ms": round(
                        self.wall_elapsed_at_close_ms if self.wall_elapsed_at_close_ms is not None else self.wall_elapsed_ms,
                        2,
                    ),
                    "model_thinking_time_ms": round(
                        max(
                            0.0,
                            (self.wall_elapsed_at_close_ms if self.wall_elapsed_at_close_ms is not None else self.wall_elapsed_ms)
                            - self.active_wall_time_ms,
                        ),
                        2,
                    ),
                    "segments": timeline_segments,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        concat_path = self.run_dir / "gameplay.ffconcat"
        lines = ["ffconcat version 1.0"]
        for segment in timeline_segments:
            frame = Path(segment["frame"])
            lines.append(f"file '{self._ffconcat_path(str(frame))}'")
            lines.append(f"duration {max(0.001, float(segment['duration_ms']) / 1000.0):.3f}")
        # The concat demuxer applies the final duration to the following file;
        # repeat the final frame so its duration is retained in the output.
        lines.append(f"file '{self._ffconcat_path(str(Path(timeline_segments[-1]['frame'])))}'")
        concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        command = [
            os.environ.get("FFMPEG_BIN", "ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-fps_mode",
            "cfr",
            # Browser CSS can produce an odd-sized canvas, while yuv420p/H.264
            # requires even dimensions. Preserve the full frame and round each
            # edge down by one pixel only when necessary.
            "-vf",
            f"scale=trunc(iw/2)*2:trunc(ih/2)*2,fps={self._video_fps}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-bf",
            "0",
            "-movflags",
            "+faststart",
            str(self.video_path),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"video_path": None, "video_error": str(exc)}
        if completed.returncode != 0 or not self.video_path.is_file():
            error = (completed.stderr or "ffmpeg failed").strip()[-1000:]
            return {"video_path": None, "video_error": error}
        return {
            "video_path": str(self.video_path),
            "video_timeline_path": str(self.video_timeline_path),
            "video_duration_ms": sum(int(segment["duration_ms"]) for segment in timeline_segments),
            "video_capture_mode": "viewport",
        }

    async def _close_browser(self) -> None:
        chrome = self.chrome
        if chrome is not None:
            try:
                await asyncio.to_thread(chrome.resume)
            except (HarnessError, OSError):
                pass
        if self._canvas_capture_active:
            try:
                await self._stop_canvas_recording()
            except Exception:
                self._canvas_capture_failed = True
        if self.page is not None and self._pressed_keys:
            for key in tuple(self._pressed_keys):
                try:
                    await self.page.keyboard.up(key)
                except Exception:
                    pass
            self._pressed_keys.clear()
        if self._pressed_buttons:
            await self._release_pressed_buttons()
        if self.browser is not None:
            if self.cdp is not None:
                await self._stop_screencast()
                try:
                    await self.cdp.detach()
                except Exception:
                    pass
                self.cdp = None
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self.browser = None
        self.context = None
        self.page = None
        self.cdp = None
        self.canvas_box = None
        self.pointer_position = None
        self.pointer_locked = False
        self._pressed_buttons.clear()
        if chrome is not None:
            await asyncio.to_thread(chrome.close)
        self.chrome = None

    async def close(self) -> dict[str, Any]:
        async with self._lock:
            if self.closed:
                if self._close_result is not None:
                    return dict(self._close_result)
                # Defensive fallback for sessions marked closed by a caller
                # before the normal result assembly completed.
                wall_elapsed_ms = self.wall_elapsed_at_close_ms or 0.0
                return {
                    "status": self.status,
                    "steps": len(self._actions),
                    "simulation_ticks": self.gameplay_ticks,
                    "gameplay_time_ms": self.gameplay_time_ms,
                    "active_wall_time_ms": round(self.active_wall_time_ms, 2),
                    "wall_elapsed_ms": round(wall_elapsed_ms, 2),
                    "model_thinking_time_ms": round(max(0.0, wall_elapsed_ms - self.active_wall_time_ms), 2),
                    "candidate_snapshots": sorted(self._candidates),
                    "captures": sorted(self._submissions),
                    "recording_dir": str(self.run_dir),
                }
            # Freeze timing accounting before browser shutdown and FFmpeg
            # encoding so cleanup cost is not misreported as model thinking.
            self.wall_elapsed_at_close_ms = self.wall_elapsed_ms
            wall_elapsed_ms = self.wall_elapsed_at_close_ms
            model_thinking_time_ms = max(0.0, wall_elapsed_ms - self.active_wall_time_ms)
            await self._close_browser()
            if self.server is not None:
                self.server.close()
                self.server = None
            self.closed = True
            # FFmpeg can take seconds for a long frame timeline.  Keep the
            # async MCP server responsive while the artifact is encoded.
            video = await asyncio.to_thread(self._finalize_video)
            result = {
                "status": self.status,
                "steps": len(self._actions),
                "simulation_ticks": self.gameplay_ticks,
                "gameplay_time_ms": self.gameplay_time_ms,
                "active_wall_time_ms": round(self.active_wall_time_ms, 2),
                "wall_elapsed_ms": round(wall_elapsed_ms, 2),
                "model_thinking_time_ms": round(model_thinking_time_ms, 2),
                "candidate_snapshots": sorted(self._candidates),
                "captures": sorted(self._submissions),
                "recording_dir": str(self.run_dir),
                **video,
            }
            self.result_path.write_text(json.dumps(result, indent=2) + "\n")
            self._event({"phase": "close", "result": result})
            self._close_result = dict(result)
            return result
