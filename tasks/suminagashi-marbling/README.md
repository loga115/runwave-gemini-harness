# Suminagashi — Ink Dissolution (runwave webapp task)

An interactive Japanese paper-marbling (*suminagashi*, 墨流し) web experience,
packaged as a self-contained runwave webapp task. The agent drags across a
paper canvas to float sumi ink that dissolves into slow, flowing marbled
patterns, switches ink colours, toggles an automatic performance, and washes
the sheet clean.

Run it with runwave in webapp mode:

```sh
RUNWAVE_PROMPT_MODE=webapp \
runwave --app-dir . --out-dir ./artifacts --port 3000 --viewport 1280x720
```

## Files

- `index.html` — the self-contained application (inline logic).
- `vendor/three.min.js` — Three.js r128 (MIT), vendored so the app runs offline.
- `start.sh` — serves the app over `python3 -m http.server` on `$PORT`.
- `playtest.md` — the agent's interaction guide.
- `metadata.json` — capture viewport.

## Source & credit

Concept and original web experience by the author of
[suminagashi.miraicode.one](https://suminagashi.miraicode.one/), shared at
<https://x.com/hayashimon1/status/2064658158698782873> (deployment mirror:
<https://suminagashi-fjdbyyqi.manus.space/>). Used as the source experience and
credited here; assets are staged offline for this task. Third-party analytics
scripts from the original deployment were removed. Three.js is bundled under the
MIT License.
