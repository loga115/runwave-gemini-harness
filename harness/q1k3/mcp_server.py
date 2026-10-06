"""MCP transport for the WebGame GameSession.

The MCP process is intentionally only an adapter. It exposes named actions
and bounded relative-pointer batches, never a browser handle, and returns the
screenshot as image content so a vision-capable Harbor agent can play without
reading the game filesystem.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from webgame import GameSession, GameSpec, HarnessError, make_agent_preview

# Codex launches stdio MCP servers with a deliberately filtered environment,
# so task-level environment variables do not reliably reach this process.  The
# authoring task therefore passes an explicit marker in the command arguments;
# the finalize gate removes it before the task is exposed to solvers.
if "--builder" in sys.argv:
    os.environ["WEBGAME_ENABLE_CANDIDATE_SNAPSHOTS"] = "1"

CONFIG_PATH = Path(os.environ.get("WEBGAME_CONFIG", "/app/game/webgame.json"))
ARTIFACT_DIR = Path(os.environ.get("WEBGAME_ARTIFACT_DIR", "/logs/artifacts/webgame"))
RESOURCE_DIR = Path(os.environ.get("WEBGAME_RESOURCE_DIR", "/app/resources"))
AGENT_PREVIEW_DIR = Path(tempfile.gettempdir()) / "webgame-agent-previews"
AGENT_PREVIEW_MAX_DIM = 768
MAX_RESTARTS = 1
_start_count = 0


def _agent_image(source: Path) -> Image:
    """Return a compact visual preview while preserving the full artifact.

    Full-resolution PNGs are retained in the harness artifact directory for
    pixel scoring and oracle packaging.  Codex receives a bounded JPEG preview
    instead; otherwise every MCP turn appends a multi-megabyte image to the
    model context and long builder episodes eventually fail with HTTP 400.
    """
    try:
        return Image(path=make_agent_preview(source, AGENT_PREVIEW_DIR, AGENT_PREVIEW_MAX_DIM))
    except (OSError, ValueError):
        # The full path remains a safe fallback for unusual image formats or a
        # transient preview-write failure; artifact fidelity is never reduced.
        return Image(path=source)

mcp = FastMCP(
    "webgame",
    instructions=(
        "Play the local WebGL game one named action at a time. "
        "Call game_start or game_observe first. The returned image is the only "
        "game observation. The game is paused while you think. Use game_reference "
        "to inspect public target frames, game_similarity to score the latest "
        "frozen frame against a target without submitting it, and game_capture "
        "to submit the latest frozen frame into a named reference slot. "
        "The private author may use game_snapshot to build a twenty-frame "
        "candidate pool and game_select to promote four candidates; those "
        "builder-only tools are disabled in finalized solver tasks. "
        "Key controls also expose "
        "<name>_down and <name>_up transitions for composing held inputs. "
        "Use game_pointer for bounded batches of relative pointer events. "
        "You may start the game at most twice total per episode, including the initial "
        "attempt (one restart maximum)."
    ),
)
_session: GameSession | None = None


def _response(observation: Any, session: GameSession | None = None) -> tuple[str, Image]:
    payload = observation.public_dict()
    # The model receives the image as MCP image content.  Keep harness-local
    # artifact paths out of the agent-visible JSON so the protocol never asks
    # the solver to read or write the container filesystem.
    payload.pop("image_path", None)
    if session is not None:
        payload.update(
            {
                "simulation_ticks": session.gameplay_ticks,
                "gameplay_time_ms": session.gameplay_time_ms,
                "active_wall_time_ms": round(session.active_wall_time_ms, 2),
                "wall_elapsed_ms": round(session.wall_elapsed_ms, 2),
                "model_thinking_time_ms": round(session.model_thinking_time_ms, 2),
                "input_state": session.input_state(),
            }
        )
    return json.dumps(payload, sort_keys=True), _agent_image(observation.image_path)


async def _get_session() -> GameSession:
    global _session
    if _session is None:
        _session = GameSession(GameSpec.load(CONFIG_PATH), ARTIFACT_DIR)
    return _session


def _restart_policy() -> dict[str, int]:
    restarts_used = max(0, _start_count - 1)
    return {
        "max_restarts": MAX_RESTARTS,
        "restarts_used": restarts_used,
        "restarts_remaining": max(0, MAX_RESTARTS - restarts_used),
    }


@mcp.tool()
async def game_start() -> Any:
    """Start the game and return its first frozen screenshot and public controls."""
    global _session, _start_count
    if _session is None and _start_count >= MAX_RESTARTS + 1:
        raise RuntimeError(
            f"restart_limit: the episode allows at most {MAX_RESTARTS} restart "
            "(two total starts including the initial attempt)"
        )
    new_session = _session is None
    try:
        session = await _get_session()
        observation = await session.start()
        if new_session:
            _start_count += 1
        public_observation = observation.public_dict()
        public_observation.pop("image_path", None)
        return [
            json.dumps(
                {
                    "game": session.spec.public_dict(),
                    "restart_policy": _restart_policy(),
                    "observation": {
                        **public_observation,
                        "simulation_ticks": session.gameplay_ticks,
                        "gameplay_time_ms": session.gameplay_time_ms,
                        "active_wall_time_ms": round(session.active_wall_time_ms, 2),
                        "wall_elapsed_ms": round(session.wall_elapsed_ms, 2),
                        "model_thinking_time_ms": round(session.model_thinking_time_ms, 2),
                        "input_state": session.input_state(),
                    },
                }
            ),
            _agent_image(observation.image_path),
        ]
    except HarnessError as exc:
        # A failed start must not leave a broken session (or Chromium process)
        # behind for the next tool call.  ``game_start`` is the only place that
        # creates the singleton, so it owns this rollback.
        if _session is not None:
            try:
                await _session.close()
            except Exception:
                pass
            _session = None
        raise RuntimeError(f"{exc.code}: {exc}") from exc


@mcp.tool()
async def game_reference(reference: str) -> Any:
    """Show one public target screenshot for visual comparison."""
    allowed = {"reference_01", "reference_02", "reference_03", "reference_04"}
    if reference not in allowed:
        raise RuntimeError("invalid_reference: use reference_01 through reference_04")
    path = RESOURCE_DIR / f"{reference}.png"
    if not path.is_file():
        raise RuntimeError(f"reference_not_found: {path}")
    return [json.dumps({"reference": reference}, sort_keys=True), _agent_image(path)]


@mcp.tool()
async def game_similarity(reference: str) -> str:
    """Score the frozen frame with the verifier's equal pHash and spatial-layout pipeline (with SIFT diagnostics).

    ``similarity`` is the graded value: rescaled so 0 is chance for this game
    and 1 is the best a capture can reach. ``similarity_raw`` is the unscaled
    score, useful while still below chance, where the graded value reads 0.
    Also returns pHash, layout, SIFT, RGB and SSIM diagnostics, all unscaled.
    This does not capture a submission or advance time.
    """
    allowed = {"reference_01", "reference_02", "reference_03", "reference_04"}
    if reference not in allowed:
        raise RuntimeError("invalid_reference: use reference_01 through reference_04")
    path = RESOURCE_DIR / f"{reference}.png"
    if not path.is_file():
        raise RuntimeError(f"reference_not_found: {path}")
    try:
        session = await _get_session()
        result = await session.similarity(path, reference)
        return json.dumps(result, sort_keys=True)
    except HarnessError as exc:
        raise RuntimeError(f"{exc.code}: {exc}") from exc


@mcp.tool()
async def game_observe() -> Any:
    """Return the latest frozen screenshot; this never advances game time."""
    try:
        session = await _get_session()
        observation = await session.observe()
        return _response(observation, session)
    except HarnessError as exc:
        raise RuntimeError(f"{exc.code}: {exc}") from exc


@mcp.tool()
async def game_act(action: str, duration_ms: int | None = None) -> Any:
    """Execute one action and return the next frozen screenshot.

    For key controls, use ``<control>_down`` and ``<control>_up`` to keep a
    key held across calls while another control runs.
    """
    try:
        session = await _get_session()
        observation = await session.step(action, duration_ms)
        return _response(observation, session)
    except HarnessError as exc:
        raise RuntimeError(f"{exc.code}: {exc}") from exc


@mcp.tool()
async def game_pointer(events: list[dict[str, Any]], duration_ms: int | None = None) -> Any:
    """Execute one bounded batch of relative pointer events.

    Each event is ``{"type": "move", "dx": number, "dy": number,
    "repeat": optional integer}`` or ``{"type": "click|down|up",
    "button": "left|middle|right"}``. A click uses the current pointer
    location. Repeated moves are expanded inside one action window, so a
    batch costs one step and returns one frozen observation.
    """
    try:
        session = await _get_session()
        observation = await session.step_pointer(events, duration_ms)
        return _response(observation, session)
    except HarnessError as exc:
        raise RuntimeError(f"{exc.code}: {exc}") from exc


@mcp.tool()
async def game_capture(reference: str) -> Any:
    """Save the latest frozen frame under one of the four reference slots.

    ``reference`` must be ``reference_01`` through ``reference_04``.  The
    returned image is the exact frame copied into the submission bundle.
    """
    try:
        session = await _get_session()
        record = await session.capture(reference)
        public_record = {key: value for key, value in record.items() if key != "image_path"}
        return [json.dumps(public_record, sort_keys=True), _agent_image(Path(record["image_path"]))]
    except HarnessError as exc:
        raise RuntimeError(f"{exc.code}: {exc}") from exc


@mcp.tool()
async def game_snapshot(candidate: str) -> Any:
    """Save a builder-only candidate screenshot without a timing gate.

    ``candidate`` must be ``candidate_01`` through ``candidate_20``. The
    returned image is the exact frozen frame saved in the candidate pool.
    """
    try:
        session = await _get_session()
        record = await session.snapshot(candidate)
        public_record = {key: value for key, value in record.items() if key != "image_path"}
        return [json.dumps(public_record, sort_keys=True), _agent_image(Path(record["image_path"]))]
    except HarnessError as exc:
        raise RuntimeError(f"{exc.code}: {exc}") from exc


@mcp.tool()
async def game_select(candidate: str, reference: str) -> Any:
    """Promote one builder candidate into a final reference slot."""
    try:
        session = await _get_session()
        record = await session.select_candidate(candidate, reference)
        public_record = {key: value for key, value in record.items() if key != "image_path"}
        return [json.dumps(public_record, sort_keys=True), _agent_image(Path(record["image_path"]))]
    except HarnessError as exc:
        raise RuntimeError(f"{exc.code}: {exc}") from exc


@mcp.tool()
async def game_finish() -> str:
    """End the episode, flush recordings, and return the run summary."""
    global _session
    if _session is None:
        return json.dumps({"status": "not_started", "steps": 0})
    try:
        result = await _session.close()
        return json.dumps(result, sort_keys=True)
    finally:
        _session = None


if __name__ == "__main__":
    mcp.run(transport="stdio")
