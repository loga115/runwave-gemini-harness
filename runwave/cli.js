#!/usr/bin/env node
'use strict';

const path = require('path');
const { runPlaytest } = require('./index');
const { resolveApiKey } = require('./agent/src/model-client');

function usage() {
  return [
    'Usage:',
    '  runwave --game-dir <path> --out-dir <path> --port <n> --viewport <width>x<height> [options]',
    '  runwave --kind linux --game-dir <path> --out-dir <path> --viewport <width>x<height> [options]',
    '',
    'Required:',
    '  --game-dir <path>            web application directory containing start.sh and playtest.md',
    '  --app-dir <path>             alias for --game-dir',
    '  --out-dir <path>             directory for artifacts (video, screenshots, agent history, summary.json)',
    '  --port <n>                   web mode only: port passed to start.sh via PORT= and used as http://127.0.0.1:<port>/',
    '  --viewport <width>x<height>  browser viewport and video size, e.g. 1280x720',
    '',
    'Environment:',
    '  RUNWAVE_API_KEY              API key for the configured model endpoint',
    '  RUNWAVE_BASE_URL             optional OpenAI-compatible endpoint (defaults to OpenRouter)',
    '  RUNWAVE_AGENT_MODEL          optional model identifier (or use --model)',
    '  RUNWAVE_PROMPT_MODE          webgame (default) or webapp/generic',
    '  RUNWAVE_HTTP_REFERER         optional OpenRouter attribution URL',
    '  OPENROUTER_API_KEY           legacy default-provider key',
    '  OPENROUTER_MODEL             legacy default-provider model override',
    '',
    'Options:',
    '  --kind <web|linux>           target kind (default web)',
    '  --playtest-duration-ms <n>   max playtest wall time in ms (default 150000)',
    '  --min-playtest-ms <n>        floor before the agent may self-stop (default duration - 10000)',
    '  --launch-settle-ms <n>       linux only: wait after launch before first agent call (default 30000)',
    '  --base-url <url>             OpenAI-compatible endpoint override',
    '  --model <id>                 model identifier (sets RUNWAVE_AGENT_MODEL)',
    '  --verbose, -v                forward verbose flag to the runwave controller',
    '  --help, -h                   show this help',
  ].join('\n');
}

function parseViewport(value) {
  const match = /^(\d+)x(\d+)$/i.exec(String(value || '').trim());
  if (!match) throw new Error(`invalid viewport: ${value}`);
  return { width: Number(match[1]), height: Number(match[2]) };
}

function parseArgs(argv) {
  const out = { verbose: false, kind: 'web', startOverrides: {} };
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => argv[++i];
    if (arg === '--help' || arg === '-h') { out.help = true; continue; }
    if (arg === '--verbose' || arg === '-v') { out.verbose = true; continue; }
    if (arg === '--game-dir' || arg === '--app-dir') { out.gameDir = next(); continue; }
    if (arg === '--out-dir') { out.outDir = next(); continue; }
    if (arg === '--port') { out.port = Number(next()); continue; }
    if (arg === '--kind') { out.kind = next(); continue; }
    if (arg === '--viewport') { out.viewport = parseViewport(next()); continue; }
    if (arg === '--playtest-duration-ms') { out.playtestDurationMs = Number(next()); continue; }
    if (arg === '--min-playtest-ms') { out.minPlaytestMs = Number(next()); continue; }
    if (arg === '--launch-settle-ms') { out.startOverrides.launchSettleMs = Number(next()); continue; }
    if (arg === '--base-url') { out.baseUrl = next(); continue; }
    if (arg === '--model') { out.model = next(); continue; }
    throw new Error(`unknown argument: ${arg}`);
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv);
  if (args.help) {
    process.stdout.write(`${usage()}\n`);
    return;
  }
  const missing = [];
  if (!args.gameDir) missing.push('--game-dir');
  if (!args.outDir) missing.push('--out-dir');
  if (!['linux', 'native'].includes(String(args.kind || 'web').toLowerCase()) && !args.port) missing.push('--port');
  if (!args.viewport) missing.push('--viewport');
  const resolutionEnv = { ...process.env };
  if (args.baseUrl) resolutionEnv.RUNWAVE_BASE_URL = args.baseUrl;
  const apiKey = resolveApiKey(resolutionEnv, args.baseUrl || null);
  if (!apiKey) missing.push('RUNWAVE_API_KEY (or legacy OPENROUTER_API_KEY)');
  if (missing.length) {
    process.stderr.write(`missing required: ${missing.join(', ')}\n\n${usage()}\n`);
    process.exit(2);
  }

  const summary = await runPlaytest({
    gameDir: path.resolve(args.gameDir),
    outDir: path.resolve(args.outDir),
    port: args.port,
    kind: args.kind,
    viewport: args.viewport,
    apiKey,
    baseUrl: args.baseUrl,
    playtestDurationMs: args.playtestDurationMs,
    minPlaytestMs: args.minPlaytestMs,
    model: args.model,
    verbose: args.verbose,
    startOverrides: args.startOverrides,
  });

  process.exit(summary.status === 'passed' ? 0 : 1);
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`${error.stack || error.message}\n`);
    process.exit(1);
  });
}
