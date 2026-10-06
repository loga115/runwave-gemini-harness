"""Drive the frozen-frame MCP harness through Gemini Chat Completions."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEFAULT_ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
SOLVER_TOOLS = frozenset({
    'game_start', 'game_observe', 'game_act', 'game_pointer',
    'game_reference', 'game_similarity', 'game_capture', 'game_finish',
})


def completions_url(value: str) -> str:
    value = value.strip().rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('endpoint must be an HTTP(S) URL without credentials, query or fragment')
    return value if value.endswith('/chat/completions') else value + '/chat/completions'


def model_request(model: str, goal: str, catalog: list[dict], observation: list[dict],
                  history: list[dict], references: dict[str, list[dict]], max_tokens: int) -> dict:
    content = [{'type': 'text', 'text': 'Goal:\n' + goal + '\nRecent calls:\n' + json.dumps(history[-16:])}]
    for name, blocks in references.items():
        content += [{'type': 'text', 'text': 'Public target: ' + name}, *blocks]
    content += [{'type': 'text', 'text': 'Latest tool result (game is frozen):'}, *observation]
    return {
        'model': model,
        'messages': [
            {'role': 'system', 'content': (
                'Play using only the listed game tools and returned images. The browser is paused '
                'while you think; only game_act and game_pointer advance it. Return exactly one '
                'JSON object: {"tool":"game_act","arguments":{"action":"forward",'
                '"duration_ms":250},"reason":"brief explanation"}. Choose any listed tool '
                'and its matching arguments. Use the public controls returned by game_start. '
                'For simultaneous key holds, use <control>_down / <control>_up. Pointer batches '
                'accept relative dx/dy, not screen coordinates. Reference images are targets, '
                'not current observations. Use game_finish when done; finishing is not proof '
                'of success. No filesystem, shell, browser internals, or builder tools.\nTools:\n'
                + json.dumps(catalog)
            )},
            {'role': 'user', 'content': content},
        ],
        'response_format': {'type': 'json_object'},
        'max_tokens': max_tokens,
        'stream': False,
    }


def parse_decision(payload: dict, allowed: set[str]) -> dict:
    try:
        choice = payload['choices'][0]
        if choice.get('finish_reason') == 'length':
            raise ValueError('model response exhausted max_tokens')
        decision = json.loads(choice['message']['content'])
        if not isinstance(decision, dict) or decision.get('tool') not in allowed:
            raise ValueError('model selected an unavailable tool')
        if not isinstance(decision.get('arguments'), dict):
            raise ValueError('tool arguments must be an object')
        return decision
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('model response must contain a JSON tool and arguments object') from exc


def result_blocks(result: Any) -> list[dict]:
    blocks = []
    for block in result.content:
        if block.type == 'text':
            blocks.append({'type': 'text', 'text': block.text})
        elif block.type == 'image':
            blocks.append({'type': 'image_url', 'image_url': {
                'url': f'data:{block.mimeType};base64,{block.data}'}})
    if result.isError:
        blocks.insert(0, {'type': 'text', 'text': 'Tool error; no successful action is implied.'})
    return blocks


def text_result(blocks: list[dict]) -> str:
    return '\n'.join(b['text'] for b in blocks if b['type'] == 'text')


async def completion(client: httpx.AsyncClient, endpoint: str, key: str,
                     body: dict, attempts: int = 3) -> dict:
    for attempt in range(attempts):
        try:
            response = await client.post(endpoint, headers={'Authorization': f'Bearer {key}'}, json=body)
        except httpx.TransportError:
            if attempt + 1 == attempts:
                raise RuntimeError('model transport failed after retries') from None
        else:
            if response.is_success:
                return response.json()
            if response.status_code not in {429, 500, 502, 503, 504} or attempt + 1 == attempts:
                # Avoid logging provider error bodies: they can echo request credentials/data.
                raise RuntimeError(f'model endpoint HTTP {response.status_code}')
        await asyncio.sleep(min(2 ** attempt, 8))
    raise RuntimeError('model request failed')


async def drive(session: Any, client: httpx.AsyncClient, args: argparse.Namespace, key: str) -> dict:
    allowed = set(SOLVER_TOOLS)
    if not args.resources:
        allowed -= {'game_reference', 'game_similarity', 'game_capture'}
    catalog = [{'name': t.name, 'description': t.description, 'parameters': t.inputSchema}
               for t in (await session.list_tools()).tools if t.name in allowed]
    allowed &= {t['name'] for t in catalog}
    history: list[dict] = []
    references: dict[str, list[dict]] = {}
    goal = args.instructions.read_text()
    outcome: dict = {'status': 'running', 'model': args.model, 'turns': 0}
    started = time.monotonic()
    finished = False
    last_image: list[dict] = []
    trace = args.out_dir / 'solver.jsonl'
    try:
        observation = result_blocks(await session.call_tool('game_start', {}))
        last_image = [b for b in observation if b['type'] == 'image_url']
        if not last_image:
            raise RuntimeError('game_start failed to return an image')
        with trace.open('x') as log:
            for turn in range(1, args.max_turns + 1):
                remaining = args.wall_timeout - (time.monotonic() - started)
                if remaining <= 0:
                    outcome['status'] = 'wall_timeout'
                    break
                request_started = time.monotonic()
                try:
                    async with asyncio.timeout(remaining):
                        payload = await completion(client, args.endpoint, key, model_request(
                            args.model, goal, catalog, observation, history, references, args.max_tokens))
                except TimeoutError:
                    outcome['status'] = 'wall_timeout'
                    break
                model_ms = round((time.monotonic() - request_started) * 1000, 2)
                record = {'turn': turn, 'model_ms': model_ms, 'usage': payload.get('usage')}
                try:
                    decision = parse_decision(payload, allowed)
                except ValueError as exc:
                    record['error'] = str(exc)
                    observation = [{'type': 'text', 'text': str(exc) + '; return a valid JSON tool call.'}, *observation]
                else:
                    record['decision'] = decision
                    tool_started = time.monotonic()
                    result = await session.call_tool(decision['tool'], decision['arguments'])
                    blocks = result_blocks(result)
                    record.update(tool_ms=round((time.monotonic() - tool_started) * 1000, 2),
                                  is_error=bool(result.isError), result=text_result(blocks))
                    if decision['tool'] == 'game_reference' and not result.isError:
                        references[str(decision['arguments']['reference'])] = blocks
                        observation = [{'type': 'text', 'text': record['result']}, *observation]
                    else:
                        images = [b for b in blocks if b['type'] == 'image_url']
                        if images:
                            last_image = images
                        observation = blocks if images else [*blocks, *last_image]
                    if decision['tool'] == 'game_finish' and not result.isError:
                        finished = True
                        outcome.update(status='finished', harness_result=record['result'])
                history.append(record)
                log.write(json.dumps(record) + '\n')
                log.flush()
                outcome['turns'] = turn
                print(f'turn={turn} model_ms={model_ms} tool={record.get("decision", {}).get("tool", "invalid")}', flush=True)
                if finished:
                    break
            else:
                outcome['status'] = 'turn_limit'
    except BaseException as exc:
        outcome.update(status='error', error=type(exc).__name__)
        raise
    finally:
        try:
            if not finished:
                result = await session.call_tool('game_finish', {})
                outcome['harness_result'] = text_result(result_blocks(result))
        finally:
            outcome['wall_ms'] = round((time.monotonic() - started) * 1000, 2)
            (args.out_dir / 'solver-summary.json').write_text(json.dumps(outcome, indent=2) + '\n')
    return outcome


async def run(args: argparse.Namespace) -> dict:
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('RUNWAVE_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        raise ValueError('set GEMINI_API_KEY (or RUNWAVE_API_KEY / GOOGLE_API_KEY)')
    if not args.config.is_file() or not args.instructions.is_file():
        raise ValueError('--config and --instructions must be existing files')
    if args.resources and not args.resources.is_dir():
        raise ValueError('--resources must be an existing directory')
    if args.out_dir.exists():
        raise ValueError('--out-dir must not already exist (use a new run directory)')
    args.out_dir.mkdir(parents=True, exist_ok=False)
    # The model credential is only needed by this process, not the game/MCP child.
    env = {k: v for k, v in os.environ.items()
           if k in {'PATH', 'HOME', 'DISPLAY', 'XAUTHORITY', 'LD_LIBRARY_PATH', 'CHROMIUM_BIN',
                    'PLAYWRIGHT_BROWSERS_PATH', 'TMPDIR', 'LANG'} or k.startswith('WEBGAME_')}
    env.update(WEBGAME_CONFIG=str(args.config), WEBGAME_ARTIFACT_DIR=str(args.out_dir / 'webgame'),
               WEBGAME_RESOURCE_DIR=str(args.resources or args.out_dir / 'no-resources'),
               WEBGAME_ENABLE_CANDIDATE_SNAPSHOTS='0')
    server = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name('mcp_server.py'))], env=env)
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            async with httpx.AsyncClient(timeout=args.request_timeout, follow_redirects=False) as client:
                return await drive(session, client, args, key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=lambda p: Path(p).resolve(), required=True, help='game webgame.json')
    parser.add_argument('--instructions', type=lambda p: Path(p).resolve(), required=True, help='public goal/controls text')
    parser.add_argument('--resources', type=lambda p: Path(p).resolve(), help='public reference PNG directory')
    parser.add_argument('--out-dir', type=lambda p: Path(p).resolve(), required=True, help='new output directory')
    parser.add_argument('--model', default=os.environ.get('RUNWAVE_AGENT_MODEL', 'gemini-3.8-flash'))
    parser.add_argument('--endpoint', type=completions_url,
                        default=os.environ.get('RUNWAVE_GEMINI_ENDPOINT', DEFAULT_ENDPOINT))
    parser.add_argument('--max-turns', type=int, default=100)
    parser.add_argument('--max-tokens', type=int, default=4096)
    parser.add_argument('--wall-timeout', type=float, default=900, help='solver wall budget in seconds')
    parser.add_argument('--request-timeout', type=float, default=60)
    args = parser.parse_args()
    if min(args.max_turns, args.max_tokens, args.wall_timeout, args.request_timeout) <= 0:
        parser.error('limits and timeouts must be positive')
    try:
        outcome = asyncio.run(run(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f'Run failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(outcome, indent=2))
    raise SystemExit(0 if outcome['status'] == 'finished' else 1)


if __name__ == '__main__':
    main()
