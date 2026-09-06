# Runwave

Runwave is an agentic harness which allows a VLM to explore and interact with
browser-based applications while capturing a reproducible video and action
history.

![](/assets/architecture.png)

It includes remote stress testing code (`./stress-test`) as a first class component to prevent regression and enable quick development cycles. It can scale horizontally to play hundreds of games at once.

![Runwave playing 12 games](/assets/12-games.gif)

Runwave supports two target types:

- web applications served to Chromium
- native Linux applications that open an X11 window

## Limitations

Runwave is still best suited to interactive applications that:

- can run in a browser or in a normal Linux/X11 window
- can run on the available server graphics stack
- do not involve a lot of precise clicks
- do not require reaction speeds of < 2 seconds

OpenRouter is the default model provider. The agent also accepts any endpoint
that implements the OpenAI Chat Completions API. Set `RUNWAVE_BASE_URL` and
`RUNWAVE_API_KEY` for a generic endpoint, or use the corresponding provider
environment variables (`OPENAI_API_KEY`, `GEMINI_API_KEY`, or the legacy
`OPENROUTER_API_KEY`). Set `RUNWAVE_AGENT_MODEL` (or the provider-specific
model variable) to choose the model. With no override, OpenRouter uses
`google/gemini-3.8-flash`; the direct Gemini endpoint uses `gemini-3.8-flash`.

The legacy web-game prompt is the default (`RUNWAVE_PROMPT_MODE=webgame`) so
existing delivered tasks keep their previous behavior. For a simulation or
other interactive website, set `RUNWAVE_PROMPT_MODE=webapp` (the aliases
`website` and `generic` are also accepted). This changes the VLM's guidance,
not the controller, action schema, or recording format.

## Requirements

Runwave's recording pipeline is **gstreamer-only** as other methods lead to audio/video mismatches. This entails:

- **Linux.** gstreamer's `ximagesrc` and `pulsesrc` elements only work on Linux.
- **gstreamer 1.x** with `ximagesrc`, `pulsesrc`, `vp8enc`, `opusenc`,
  `webmmux`, and `filesink` available on `PATH` as `gst-launch-1.0` (override
  via the `RUNWAVE_GSTREAMER` env var or the `gstreamerPath` start option).
- **An X server or Xvfb.** `DISPLAY` must be set to a display that Chromium
  or a native Linux game can render into and that `ximagesrc` can read.
- **PulseAudio running.** `pactl info` must succeed. Chromium's audio must be
  routed to a sink whose `.monitor` source is captured by `pulsesrc`. On
  headless servers, load a null-sink (e.g. `runwave_sink`) and set
  `PULSE_SINK` before starting Chromium; pass `audioSource: "runwave_sink.monitor"`
  (or `RUNWAVE_AUDIO_SOURCE`) to the start action.
- **`xdotool` for native Linux games.** Runwave uses it to find/focus the game
  window and send keyboard/mouse input.

The controller checks these prerequisites before spawning gstreamer and fails
fast with a message naming the missing piece.


## Install

From a local checkout:

```sh
npm install
npm test
```

From a private GitHub repo in a task Dockerfile:

```dockerfile
RUN apt-get update && apt-get install -y \
    gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly gstreamer1.0-x gstreamer1.0-pulseaudio \
    pulseaudio xvfb xdotool
RUN npm install -g https://github.com/parsewave/runwave.git
RUN npx playwright install --with-deps chromium
```

The public CLI runs an end-to-end playtest:

```sh
runwave --game-dir ./app --out-dir ./artifacts --port 3000 --viewport 1280x720
```

`--game-dir` is retained as a compatibility name; `--app-dir` is an alias.
The directory must contain an executable `start.sh` and a `playtest.md` guide.
For example, a direct Gemini-compatible setup is:

```sh
RUNWAVE_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai \
RUNWAVE_API_KEY="$GEMINI_API_KEY" \
RUNWAVE_AGENT_MODEL=gemini-3.8-flash \
runwave --app-dir ./app --out-dir ./artifacts --port 3000 --viewport 1280x720
```

For a native Linux application, put a `start.sh` beside `playtest.md`. The
script should launch the application in the current X11 display and keep
running until it exits. `--port` is not used:

```sh
runwave --kind linux --app-dir ./linux-app --out-dir ./artifacts --viewport 1280x720
```

`--kind` is only a controller routing setting. The agent still receives the same
screenshot, grid, playtest guide, and generic action schema for every target.
The controller owns target-specific launch, capture, recording, and input.
For Linux targets, `--viewport` is the virtual display capture size. Runwave
records and screenshots that full display area, then focuses the detected game
window for input. `start.sh` receives `RUNWAVE_VIEWPORT_WIDTH` and
`RUNWAVE_VIEWPORT_HEIGHT` so wrappers can pass engine-specific size flags
without hardcoding dimensions. Linux sessions wait 30 seconds after launch
before the first agent call by default; pass `--launch-settle-ms 0` to disable
that for fast smoke tests.

The package also includes a low-level controller CLI for direct browser actions:

```sh
runwave-controller '<json>'
```

Detailed controller action docs are in [runwave/controller/README.md](runwave/controller/README.md).

Use controller verbose mode to create profiling logs:

```sh
runwave-controller -v '{"action":"start","action_name":"run-start","file":"game/index.html"}'
runwave-controller -v '{"action":"state","action_name":"turn-001-state"}'
```

Controller verbose mode writes newline-delimited JSON timing events to
`<sessionDir>/runwave-verbose.ndjson` and includes that path as `verboseLog` in
verbose operation responses. The log records CLI, daemon, browser, output writing,
state, screenshot, navigation, step timeline, input-event, capture, and cleanup
timings.


## Test Locally

A good first step is to run a single playtest using the example game. This will create a local recording. See the skill `skills/runwave-local-stress-test` for more details.
