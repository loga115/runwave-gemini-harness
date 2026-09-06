from __future__ import annotations

import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from runwavepy import PlaytestError, run_playtest


def _write_fake_cli(directory: Path, body: str) -> Path:
    cli = directory / "runwave"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(body).lstrip(),
        encoding="utf-8",
    )
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli


class RunPlaytestTests(unittest.TestCase):
    def test_run_playtest_invokes_runwave_and_parses_summary(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            game_dir = root_path / "game"
            out_dir = root_path / "out"
            game_dir.mkdir()
            cli = _write_fake_cli(
                root_path,
                """
                import json
                import os
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                out_dir = Path(args[args.index("--out-dir") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "call.json").write_text(json.dumps({
                    "args": args,
                    "openrouter": os.environ.get("OPENROUTER_API_KEY"),
                    "runwave_api_key": os.environ.get("RUNWAVE_API_KEY"),
                    "runwave_base_url": os.environ.get("RUNWAVE_BASE_URL"),
                    "extra": os.environ.get("RUNWAVE_TEST_EXTRA"),
                }), encoding="utf-8")
                print('{"event":"start","ok":true}', flush=True)
                print("plain log line", flush=True)
                (out_dir / "summary.json").write_text(json.dumps({
                    "status": "passed",
                    "steps": 3,
                }), encoding="utf-8")
                """,
            )

            logs: list[dict] = []
            result = run_playtest(
                game_dir=game_dir,
                out_dir=out_dir,
                port=4011,
                openrouter_api_key="secret-key",
                viewport={"width": 1280, "height": 720},
                playtest_duration_ms=120000,
                min_playtest_ms=110000,
                launch_settle_ms=30000,
                model="openai/test-model",
                verbose=True,
                cli_path=cli,
                env={"RUNWAVE_TEST_EXTRA": "extra-value"},
                on_log=logs.append,
            )

            call = json.loads((out_dir / "call.json").read_text(encoding="utf-8"))
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.summary["steps"], 3)
            self.assertEqual(result.out_dir, out_dir.resolve())
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(call["openrouter"], "secret-key")
            self.assertEqual(call["runwave_api_key"], "secret-key")
            self.assertEqual(call["extra"], "extra-value")
            self.assertEqual(
                call["args"],
                [
                    "--game-dir", str(game_dir.resolve()),
                    "--out-dir", str(out_dir.resolve()),
                    "--kind", "web",
                    "--port", "4011",
                    "--viewport", "1280x720",
                    "--playtest-duration-ms", "120000",
                    "--min-playtest-ms", "110000",
                    "--launch-settle-ms", "30000",
                    "--model", "openai/test-model",
                    "--verbose",
                ],
            )
            self.assertEqual(logs, [{"event": "start", "ok": True}, {"raw": "plain log line"}])

    def test_run_playtest_requires_model_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "model API key is required"):
                    run_playtest(
                        game_dir=root_path / "game",
                        out_dir=root_path / "out",
                        port=4011,
                        cli_path=root_path / "runwave",
                    )

    def test_run_playtest_raises_with_failed_summary(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            game_dir = root_path / "game"
            out_dir = root_path / "out"
            game_dir.mkdir()
            cli = _write_fake_cli(
                root_path,
                """
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                out_dir = Path(args[args.index("--out-dir") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "summary.json").write_text(json.dumps({
                    "status": "failed",
                    "error": "model failed",
                }), encoding="utf-8")
                sys.exit(7)
                """,
            )

            with self.assertRaisesRegex(PlaytestError, "runwave exited 7: model failed") as raised:
                run_playtest(
                    game_dir=game_dir,
                    out_dir=out_dir,
                    port=4011,
                    openrouter_api_key="secret-key",
                    cli_path=cli,
                )

            self.assertEqual(raised.exception.exit_code, 7)
            self.assertEqual(raised.exception.summary, {"status": "failed", "error": "model failed"})

    def test_run_playtest_raises_when_success_writes_no_summary(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            game_dir = root_path / "game"
            out_dir = root_path / "out"
            game_dir.mkdir()
            cli = _write_fake_cli(root_path, "import sys\nsys.exit(0)\n")

            with self.assertRaisesRegex(PlaytestError, "did not write summary.json") as raised:
                run_playtest(
                    game_dir=game_dir,
                    out_dir=out_dir,
                    port=4011,
                    openrouter_api_key="secret-key",
                    cli_path=cli,
                )

            self.assertEqual(raised.exception.exit_code, 0)
            self.assertIsNone(raised.exception.summary)

    def test_run_playtest_supports_linux_target_without_port(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            game_dir = root_path / "game"
            out_dir = root_path / "out"
            game_dir.mkdir()
            cli = _write_fake_cli(
                root_path,
                """
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                out_dir = Path(args[args.index("--out-dir") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "call.json").write_text(json.dumps({"args": args}), encoding="utf-8")
                (out_dir / "summary.json").write_text(json.dumps({"status": "passed"}), encoding="utf-8")
                """,
            )

            run_playtest(
                game_dir=game_dir,
                out_dir=out_dir,
                target_kind="linux",
                openrouter_api_key="secret-key",
                viewport={"width": 1280, "height": 720},
                cli_path=cli,
            )

            call = json.loads((out_dir / "call.json").read_text(encoding="utf-8"))
            self.assertEqual(
                call["args"],
                [
                    "--game-dir", str(game_dir.resolve()),
                    "--out-dir", str(out_dir.resolve()),
                    "--kind", "linux",
                    "--viewport", "1280x720",
                ],
            )

    def test_run_playtest_accepts_generic_endpoint_and_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            game_dir = root_path / "app"
            out_dir = root_path / "out"
            game_dir.mkdir()
            cli = _write_fake_cli(
                root_path,
                """
                import json
                import os
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                out_dir = Path(args[args.index("--out-dir") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "call.json").write_text(json.dumps({
                    "args": args,
                    "api_key": os.environ.get("RUNWAVE_API_KEY"),
                    "base_url": os.environ.get("RUNWAVE_BASE_URL"),
                }), encoding="utf-8")
                (out_dir / "summary.json").write_text(json.dumps({"status": "passed"}), encoding="utf-8")
                """,
            )

            run_playtest(
                game_dir=game_dir,
                out_dir=out_dir,
                port=4011,
                api_key="generic-key",
                base_url="https://example.test/v1",
                cli_path=cli,
            )

            call = json.loads((out_dir / "call.json").read_text(encoding="utf-8"))
            self.assertEqual(call["api_key"], "generic-key")
            self.assertEqual(call["base_url"], "https://example.test/v1")
            self.assertIn("--base-url", call["args"])


if __name__ == "__main__":
    unittest.main()
