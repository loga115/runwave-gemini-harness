# WebGame harness

This directory contains the engine-agnostic runtime used by Harbor WebGame
tasks. It turns a static browser game into a small MCP surface for a vision
agent and records an action-time replay for debugging and evaluation.

The harness is designed for WebGL games built with Unity, Godot, Three.js, or
another browser runtime. It does not inspect engine internals. A game only
needs to be serveable from a local `index.html` (or another relative
entrypoint), expose a visible canvas, and describe its controls in
`webgame.json`.

## Guarantees and boundaries

The runtime provides:

- one serialized browser session per episode;
- a frozen observation at every model boundary;
- input actions with deterministic simulation time;
- a separate measurement of active browser time and model-thinking time;
- PNG observations and a timestamped `gameplay.mp4` replay; and
- a read-only similarity diagnostic against each public reference; and
- an artifact trail containing the manifest, actions, frames, captures, and
  timing metadata.

It is not a zero-configuration adapter for every possible WebGL page. Each
game still supplies a manifest with its canvas/ready selectors. Named controls
are optional convenience aliases; the generic relative-pointer input is
available to every game. Games that require a remote service, unusual browser APIs, a custom
boot flow, or nonstandard input may need a small game-specific adapter or
manifest change. The harness itself does not infer win/loss state from pixels;
`timeout` and `fault` are the runtime-owned terminal states today.

## Runtime topology

```text
Harbor agent
    │ MCP/stdio: named tools + image content
    ▼
mcp_server.py  ─── one GameSession singleton
    │
    ├── GameSpec / Control validation (webgame.json)
    ├── _StaticServer (loopback HTTP)
    ├── _ChromiumProcess (isolated process group)
    │       └── Playwright + CDP page
    ├── pause/resume gate (SIGSTOP/SIGCONT)
    ├── input adapter + tick clock
    ├── observation capture (canvas-clipped PNG)
    └── replay capture (viewport screencast or direct canvas media)
                         │
                         ▼
              /logs/artifacts/webgame/
```

## Components

### `mcp_server.py`: protocol boundary

`mcp_server.py` is intentionally thin. It reads the three runtime paths from
environment variables, creates one `GameSession`, and exposes only named
tools:

| Tool | Behavior |
| --- | --- |
| `game_start` | Start the server/browser, wait for readiness, return the first frozen image and public controls. |
| `game_observe` | Return the latest frozen image without advancing game time. |
| `game_act` | Execute one manifest action for a requested duration and return the next frozen image. |
| `game_pointer` | Execute one bounded batch of relative pointer events and return the next frozen image. |
| `game_reference` | Return one static public target image. |
| `game_similarity` | Return the fixed additive pHash/layout/SIFT score plus RGB/SSIM diagnostics between the latest frozen frame and one public target; this never submits a frame or advances time. |
| `game_capture` | Copy the latest frozen observation into a named submission slot. |
| `game_snapshot` | **Builder only.** Copy the latest frozen observation into one of twenty candidate slots without a timing gate. |
| `game_select` | **Builder only.** Promote one candidate into a final reference slot after visual review. |
| `game_finish` | Close the episode, flush artifacts, encode the replay, and return a summary. |

The MCP response contains image content, not a path that the agent must read
from the container. The session lock serializes tool calls so two actions can
never manipulate the same browser concurrently.

### `GameSpec` and `Control`: manifest contract

`GameSpec.load()` parses and validates `webgame.json` before a browser is
started. It validates safe relative entrypoints, positive viewport/timing
values, unique control names, and bounded input values. A key control also
gets persistent `<name>_down` and `<name>_up` transitions for composing held
inputs across calls.

The important manifest fields are:

| Field | Purpose |
| --- | --- |
| `entrypoint` | Relative page to open, normally `index.html`. |
| `canvas_selector` | Canvas used for agent observations and relative input coordinates. |
| `ready_selector` | Element that must be visible before the first observation. |
| `ready_check_script` | Optional browser predicate for engine-specific loading completion. |
| `viewport.width/height` | Logical CSS viewport used by the game and manifest coordinates. |
| `ready_timeout_ms`, `ready_settle_ms` | Startup wait and post-ready settle window. |
| `action_window_ms` | Default duration when an action has no manifest duration. |
| `max_steps` | Maximum action calls in one episode. |
| `input` | Public description and limits for generic relative-pointer input. |
| `min_reference_time_ms` | Earliest accepted `game_capture` time; this template and finalized tasks use `0`. |
| `controls` | Optional named `key`, `mouse_click`, `mouse_move`, or `wait` actions. |
| `clock.tick_ms` | Simulation-clock quantum; defaults to 50 ms. |
| `clock.recording_interval_ms` | Sampling interval only used by the compatibility fallback. |
| `clock.post_action_settle_ms` | Extra ticks after an action for rendering/input settle. |
| `clock.require_strong_pause` | Require `/proc` confirmation that the browser process tree stopped. |

Mouse-click coordinates are normalized to the selected canvas (`0` to `1`).
Unlocked mouse moves are relative deltas clamped to the canvas; pointer-locked
games receive unbounded relative movement. Key actions are pressed for the
requested active window and released in a `finally` path;
explicit `_down`/`_up` transitions keep a key held across calls.

The generic `game_pointer` tool lets an agent compose relative pointer input
without a game-specific control list. Each batch is one harness step and may
contain `move`, `click`, `down`, and `up` events. A move uses CSS-pixel `dx`
and `dy` deltas (each at most `100` pixels); `repeat` expands a small move in
the browser while keeping one model boundary. A click targets the current
pointer position. Batches are capped at `256` expanded events, and explicit
mouse buttons remain held until an `up` event or episode cleanup. The initial
pointer position is the canvas center. Unlocked movement is clamped to
refreshed canvas bounds; pointer-locked movement is unbounded.

For example, the following moves right by `80` pixels in ten small events and
then clicks once, while consuming one gameplay step:

```json
{
  "events": [
    {"type": "move", "dx": 8, "dy": 0, "repeat": 10},
    {"type": "click", "button": "left"}
  ]
}
```

### `_StaticServer`: deterministic local hosting

The game root is served by a quiet `ThreadingHTTPServer` bound to loopback on
an ephemeral port. This gives Chromium a normal HTTP origin (required by many
WebGL builds) while keeping the episode self-contained. The server is closed
when the session closes.

### `_ChromiumProcess`: browser lifecycle and pause gate

Chromium runs headless with a temporary profile and a dedicated process group.
The harness connects to that browser over CDP, creates a Playwright page, and
stops the complete process tree with `SIGSTOP` before returning an observation.
It resumes the group only for startup, input, rendering, and capture. Strong
pause mode verifies process state through `/proc`; a failed pause/resume is a
`HarnessError` rather than silently allowing the game to run while the model
thinks.

The browser launch has two performance-related knobs:

- `WEBGAME_DEVICE_SCALE_FACTOR` (default `1.0`) changes physical capture size
  without changing the logical CSS viewport. For example, a manifest viewport
  of 1280×720 with `0.5` produces 640×360 screenshots/video while preserving
  game coordinate semantics. Values from `0.1` through `4.0` are accepted.
- `WEBGAME_USE_SWIFTSHADER=1` opts into Chromium's SwiftShader ANGLE backend.
  It is deliberately opt-in because forcing software rendering can be slower
  when a usable virtual GPU is available.

### `GameSession`: page, input, clock, and state machine

`GameSession` owns the episode directories and all browser-facing state. Its
high-level lifecycle is:

1. Validate the game root and entrypoint.
2. Start the loopback server and isolated Chromium process.
3. Navigate to the entrypoint, apply CDP viewport metrics, wait for the ready
   selector/script, and settle.
4. Capture the first observation and replay sample, then pause Chromium.
5. For each `game_act` or `game_pointer`, require a confirmed pause, resume
   Chromium, apply the named control or pointer batch, record active action
   time, settle, capture the next frozen observation, and pause again.
6. On `game_finish`, close the browser/server and finalize the replay.

Action duration is quantized with `ceil(duration_ms / tick_ms)`. The
deterministic simulation clock advances by action ticks plus post-action settle
ticks, and is limited only by `max_steps`; the solver wall-clock timeout is configured by the task. Host measurements are kept
separately:

- `gameplay_time_ms`: deterministic active simulation time;
- `active_wall_time_ms`: time the browser was being driven/captured;
- `model_thinking_time_ms`: wall time spent at frozen model boundaries; and
- `wall_elapsed_ms`: total episode wall time.

Only gameplay/action time appears in the replay timeline. Model-thinking time
is retained in the timeline and trajectory metadata but is not stretched into
the video.

### Observation capture

Agent observations are always lossless PNGs captured through CDP. They clip to
the selected canvas bounding box, so a wrapped or letterboxed page does not
change the agent's game observation dimensions. Observation capture happens
after the action is complete and the browser is live; CDP calls cannot finish
while Chromium is stopped.

### Replay capture and encoding

The replay source is independent of the one-observation-per-action protocol:

1. The default `viewport` mode starts one `Page.startScreencast` session during
   startup and keeps it alive across active actions. CDP frames are
   acknowledged promptly and persisted as JPEG quality 75 samples. The full
   browser viewport is used so centered, transformed, or letterboxed canvases
   do not need to be rewritten. Frames are tagged to the current action only
   while the browser is live; initial and final samples are added around every
   action.
2. If the browser lacks the screencast API, the harness falls back to a slower
   full-viewport JPEG sampler at `recording_interval_ms`.
3. `WEBGAME_CAPTURE_MODE=canvas` or `auto` may use
   `canvas.captureStream()` plus `MediaRecorder` inside the page. It records
   one WebM clip per active action at `WEBGAME_CANVAS_CAPTURE_FPS` (default
   60), then joins the clips into the same MP4. Direct mode also keeps a
   parallel viewport timeline as a fallback. If direct capture is unsupported,
   a clip is incomplete, or a later clip fails, the complete episode falls back
   to that viewport timeline rather than mixing sources or dropping earlier
   actions.

FFmpeg encodes the timestamped samples as constant-frame-rate H.264 MP4 (30 FPS
by default; configure with `WEBGAME_VIDEO_FPS`). Rejected compositor frames are
not written to the replay, so a bad surface cannot appear as a zoom or skip.
The timeline preserves both the sample timestamp and the corresponding
simulation tick. Viewport timelines use `timestamp_timebase: "gameplay_ms"`;
direct media timelines use `timestamp_timebase: "active_media_ms"`.

### Artifact layout

All paths below are relative to `WEBGAME_ARTIFACT_DIR`:

| Path | Contents |
| --- | --- |
| `manifest.json` | Resolved public game spec, pause mode, and capture settings. |
| `trajectory.jsonl` | Flushed start/observe/action/capture/close events with timing. |
| `replay.json` | Ordered named actions and transitions. |
| `frames/` | Harness-owned observation PNGs and viewport replay JPEGs. |
| `submissions/` | PNGs copied by `game_capture` or builder-only `game_select`. |
| `gameplay.mp4` | Final replay, if at least one valid frame exists. |

## Gemini solver runner

`gemini_runner.py` is a thin solver-side client for this same MCP surface. It
keeps the harness as the authority for browser lifecycle, input validation,
pause/resume, screenshots, and replay artifacts; Gemini only chooses the next
MCP tool call from the frozen observation. It uses Gemini's OpenAI-compatible
Chat Completions endpoint directly and sends returned PNGs as `data:` image
URLs.

Set `GEMINI_API_KEY` and run it with a new output directory:

```sh
python3 gemini_runner.py \
  --config /app/game/webgame.json \
  --instructions /app/task/instruction.md \
  --resources /app/resources \
  --out-dir /logs/artifacts/webgame
```

The endpoint may be supplied as either the base URL or the full URL shown
below; both normalize to one request path:

```sh
RUNWAVE_GEMINI_ENDPOINT=https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
RUNWAVE_AGENT_MODEL=gemini-3.8-flash
```

The runner writes `solver.jsonl`, `solver-summary.json`, and the harness-owned
`webgame/` artifact directory. Candidate snapshot and selection tools are
never advertised to the model.
| `gameplay_timeline.json` | Source frames, timestamps, durations, ticks, and wall-time accounting. |
| `candidates.json` | Builder-only candidate metadata and hashes; never shipped in a solver task. |
| `candidates/` | Builder-only candidate PNGs; never shipped in a solver task. |
| `gameplay.ffconcat` | FFmpeg concat input for the viewport replay. |
| `canvas-video/` | Per-action WebM clips when direct canvas capture is active. |
| `gameplay-canvas.ffconcat` | FFmpeg concat input for direct canvas clips. |
| `result.json` | Same summary returned by `game_finish`. |

## Adding another game

1. Put the complete browser build under the task's `environment/game/`.
2. Ensure its entrypoint is a relative file under that directory and can load
   without external network access.
3. Set `canvas_selector` to the visible game canvas and use a
   `ready_check_script` when a DOM-visible canvas is not enough to indicate
   that the engine finished booting.
4. Describe any game-specific actions in `controls`. Prefer semantic names and
   keep click coordinates normalized to the canvas; use `game_pointer` for
   dynamic relative-pointer input.
5. Choose the logical viewport in `webgame.json`; use
   `WEBGAME_DEVICE_SCALE_FACTOR` at runtime when a smaller physical recording
   is desired.
6. Run the harness tests and a real `game_start` → `game_act` → `game_finish`
   smoke test. Inspect both `gameplay.mp4` and
   `gameplay_timeline.json`.

No harness source change should be necessary merely because the game was built
with a different WebGL engine. Source changes are appropriate only when the
new game exposes a genuinely different browser/input/capture contract.

## Runtime configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `WEBGAME_CONFIG` | `/app/game/webgame.json` | Game manifest path. |
| `WEBGAME_ARTIFACT_DIR` | `/logs/artifacts/webgame` | Episode artifact directory. |
| `WEBGAME_RESOURCE_DIR` | `/app/resources` | Public reference image directory. |
| `CHROMIUM_BIN` | `chromium` | Browser executable. |
| `WEBGAME_CAPTURE_MODE` | `viewport` | `viewport`, `canvas`, or `auto`. |
| `WEBGAME_DEVICE_SCALE_FACTOR` | `1.0` | Physical capture scale; leaves CSS viewport unchanged. |
| `WEBGAME_CANVAS_CAPTURE_FPS` | `60` | Requested direct-canvas stream rate. |
| `WEBGAME_CAPTURE_MIN_REFERENCE_TIME_MS` | manifest value | Runtime lower bound for `game_capture`; finalized solver tasks set `0`. |
| `WEBGAME_ENABLE_CANDIDATE_SNAPSHOTS` | disabled | Enables builder-only `game_snapshot` and `game_select`; finalize removes it. |
| `WEBGAME_USE_SWIFTSHADER` | disabled | Force SwiftShader when set to `1`, `true`, or `yes`. |
| `FFMPEG_BIN` / `FFPROBE_BIN` | `ffmpeg` / `ffprobe` | Media tooling overrides. |

The task image installs Chromium, FFmpeg, Playwright, Pillow, and the MCP package. The
Harbor task wires `python3 /app/harness/mcp_server.py` over stdio; the agent
does not receive a browser handle or arbitrary JavaScript execution.
