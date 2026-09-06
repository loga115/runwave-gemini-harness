#!/usr/bin/env node
'use strict';

const fs = require('fs');
const http = require('http');
const path = require('path');
const { spawn } = require('child_process');

const DEFAULT_PROCESS_STOP_WAIT_MS = 5000;
const DEFAULT_PROCESS_KILL_WAIT_MS = 5000;
const DEFAULT_AUDIO_VIDEO_CAPTURE_Y = 0;
const DEFAULT_XVFB_CAPTURE_PADDING_X = 64;
const DEFAULT_XVFB_CAPTURE_PADDING_Y = 160;

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--job') out.job = argv[++i];
    else if (arg.startsWith('--job=')) out.job = arg.slice('--job='.length);
  }
  return out;
}

function loadEnvFile(file) {
  const env = {};
  if (!fs.existsSync(file)) return env;
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const match = line.match(/^export\s+([A-Za-z_][A-Za-z0-9_]*)='(.*)'$/);
    if (match) env[match[1]] = match[2].replace(/'"'"'/g, "'");
  }
  return env;
}

function log(event, fields = {}) {
  process.stdout.write(`${JSON.stringify({ ts: new Date().toISOString(), event, ...fields })}\n`);
}

function mkdirp(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, Number(ms) || 0)));
}

function safeName(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 120);
}

function positiveInteger(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.round(number) : fallback;
}

function nonNegativeInteger(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? Math.round(number) : fallback;
}

function runnerPaths(env = process.env) {
  return {
    gamesRoot: env.RUNWAVE_GAMES_ROOT || '/opt/runwave/games',
    jobsRoot: env.RUNWAVE_JOBS_ROOT || '/var/lib/runwave/jobs',
  };
}

function shouldRunJobInContainer(job = {}, env = process.env, platform = process.platform) {
  if (platform !== 'linux') return false;
  if (env.RUNWAVE_IN_CONTAINER === '1') return false;
  if (env.RUNWAVE_PLAYTEST_CONTAINER === '0') return false;
  if (job.containerized === false || job.runInContainer === false) return false;
  return true;
}

function dockerEnvNames(env = process.env) {
  const allowed = [
    /^AWS_/,
    /^OPENAI_/,
    /^ANTHROPIC_/,
    /^OPENROUTER_/,
    /^GEMINI_(API_KEY|MODEL)$/,
    /^PARSEWAVE_/,
    /^GITHUB_/,
    /^GH_TOKEN$/,
    /^RUNWAVE_AGENT_/,
    /^RUNWAVE_PROMPT_MODE$/,
    /^RUNWAVE_PROMPT_PROFILE$/,
    /^RUNWAVE_API_KEY$/,
    /^RUNWAVE_BASE_URL$/,
    /^RUNWAVE_HTTP_REFERER$/,
    /^RUNWAVE_OPENAI_BASE_URL$/,
    /^RUNWAVE_OPENROUTER_API_KEY$/,
    /^RUNWAVE_AUDIO_/,
    /^RUNWAVE_CHROMIUM_ARGS(_MODE)?$/,
    /^RUNWAVE_GSTREAMER$/,
    /^RUNWAVE_SKIP_PLAYWRIGHT_INSTALL$/,
    /^RUNWAVE_VERBOSE$/,
    /^RUNWAVE_VIDEO_/,
    /^RUNWAVE_XVFB_/,
    /^RUNWAVE_PROCESS_(STOP|KILL)_WAIT_MS$/,
    /^HTTP_PROXY$/i,
    /^HTTPS_PROXY$/i,
    /^NO_PROXY$/i,
    /^ALL_PROXY$/i,
  ];
  return Object.keys(env)
    .filter((name) => allowed.some((pattern) => pattern.test(name)))
    .sort();
}

function modelEndpointConfig(env = process.env) {
  const baseUrl = env.RUNWAVE_BASE_URL
    || env.OPENAI_BASE_URL
    || env.RUNWAVE_OPENAI_BASE_URL
    || env.OPENROUTER_BASE_URL
    || '';
  const lowerBaseUrl = String(baseUrl).toLowerCase();
  const providerOverride = !env.RUNWAVE_BASE_URL
    && (env.OPENROUTER_BASE_URL || env.OPENAI_BASE_URL || env.RUNWAVE_OPENAI_BASE_URL);
  const apiKey = env.RUNWAVE_API_KEY
    || (lowerBaseUrl.includes('generativelanguage.googleapis.com') ? env.GEMINI_API_KEY : null)
    || ((lowerBaseUrl.includes('openrouter.ai') || !baseUrl || (providerOverride && env.OPENROUTER_BASE_URL))
      ? (env.OPENROUTER_API_KEY || env.RUNWAVE_OPENROUTER_API_KEY)
      : null)
    || ((providerOverride && (env.OPENAI_BASE_URL || env.RUNWAVE_OPENAI_BASE_URL))
      ? env.OPENAI_API_KEY
      : null)
    // A caller using RUNWAVE_BASE_URL can select any OpenAI-compatible service.
    // In that case OPENAI_API_KEY is the conventional generic credential.
    || (baseUrl && !lowerBaseUrl.includes('openrouter.ai')
      && !lowerBaseUrl.includes('generativelanguage.googleapis.com')
      ? env.OPENAI_API_KEY
      : null);
  return { apiKey, baseUrl: baseUrl || null };
}

function dockerMountArgs(mounts) {
  const args = [];
  for (const mount of mounts) {
    args.push('-v', `${mount.source}:${mount.target}${mount.readonly ? ':ro' : ''}`);
  }
  return args;
}

function dockerRunArgs(args, job, runner, options = {}) {
  const jobPath = path.resolve(args.job);
  const jobDir = path.dirname(jobPath);
  const jobBase = path.basename(jobPath);
  const image = job.containerImage || options.env?.RUNWAVE_PLAYTEST_IMAGE || 'runwave-playtest-runner:latest';
  const jobsRoot = path.resolve(runner.jobsRoot);
  const gamesRoot = path.resolve(runner.gamesRoot);
  const scriptPath = path.resolve(options.scriptPath || __filename);
  const envFile = options.envFile || '/etc/runwave-runner.env';
  const containerName = `runwave-${safeName(job.jobId || `${job.game || 'job'}-${Date.now()}`)}`;
  const mounts = [
    { source: gamesRoot, target: '/opt/runwave/games', readonly: true },
    { source: jobsRoot, target: '/var/lib/runwave/jobs' },
    { source: jobDir, target: '/runwave/job', readonly: true },
    { source: scriptPath, target: '/opt/runwave/bin/run-playtest.js', readonly: true },
  ];
  if (job.runwaveRepo && path.isAbsolute(String(job.runwaveRepo))) {
    const runwaveRepo = path.resolve(String(job.runwaveRepo));
    mounts.push({ source: runwaveRepo, target: runwaveRepo, readonly: true });
  }
  if (fs.existsSync(envFile)) {
    mounts.push({ source: envFile, target: '/etc/runwave-runner.env', readonly: true });
  }

  const dockerArgs = [
    'run',
    '--rm',
    '--init',
    '--ipc=host',
    '--shm-size=1g',
    '--name',
    containerName,
    ...dockerMountArgs(mounts),
    '-e',
    'RUNWAVE_IN_CONTAINER=1',
    '-e',
    'RUNWAVE_GAMES_ROOT=/opt/runwave/games',
    '-e',
    'RUNWAVE_JOBS_ROOT=/var/lib/runwave/jobs',
  ];
  if (fs.existsSync('/dev/dri')) {
    dockerArgs.push('--device', '/dev/dri');
  }
  for (const name of dockerEnvNames(options.env || process.env)) {
    dockerArgs.push('-e', name);
  }
  dockerArgs.push(image, '--job', `/runwave/job/${jobBase}`);
  return dockerArgs;
}

async function runJobInContainer(args, job, runner, env = process.env) {
  if (!fs.existsSync(runner.gamesRoot)) {
    throw new Error(`games root does not exist for container mount: ${runner.gamesRoot}`);
  }
  mkdirp(runner.jobsRoot);
  await run('docker', dockerRunArgs(args, job, runner, { env }), { env });
}

function loadPlaytestInstructions(gameDir, game) {
  const entries = fs.readdirSync(gameDir, { withFileTypes: true });
  const playtestEntry = entries.find((entry) => entry.isFile() && entry.name === 'playtest.md');
  if (!playtestEntry) {
    throw new Error(`game has no playtest.md: ${game}`);
  }

  const playtestPath = path.join(gameDir, 'playtest.md');
  return fs.readFileSync(playtestPath, 'utf8');
}

function loadRunwavePlaytest(runwaveDir) {
  const candidates = [
    path.join(runwaveDir, 'runwave'),
    path.join(runwaveDir, 'runwave', 'index.js'),
    path.join(runwaveDir, 'playtest', 'playtest.js'),
  ];
  const attempted = [];
  for (const candidate of candidates) {
    attempted.push(candidate);
    try {
      const mod = require(candidate);
      if (typeof mod.runPlaytest === 'function') return mod.runPlaytest;
    } catch (error) {
      if (error.code !== 'MODULE_NOT_FOUND' || !String(error.message).includes(candidate)) {
        throw error;
      }
    }
  }
  throw new Error(`runwave checkout does not export runPlaytest; tried ${attempted.join(', ')}`);
}

function startOverridesFromJob(job = {}, audioCapture = {}) {
  const overrides = {
    audioSource: audioCapture.audioSource,
    gstreamerPath: job.gstreamerPath,
    channel: job.channel,
    executablePath: job.executablePath,
    chromiumArgs: job.chromiumArgs,
    chromiumArgsMode: job.chromiumArgsMode,
    keyAliases: job.keyAliases,
    gridScreenshots: job.gridScreenshots,
    markGridRows: job.markGridRows,
    markGridCols: job.markGridCols,
    launchSettleMs: job.launchSettleMs,
  };
  if (job.videoSize) overrides.videoSize = job.videoSize;
  return overrides;
}

function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    log('command.start', { command, args, cwd: options.cwd });
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env || process.env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const stdout = [];
    const stderr = [];
    child.stdout.on('data', (chunk) => {
      stdout.push(chunk);
      process.stdout.write(chunk);
    });
    child.stderr.on('data', (chunk) => {
      stderr.push(chunk);
      process.stderr.write(chunk);
    });
    child.on('error', reject);
    child.on('close', (code) => {
      log('command.end', { command, code });
      const result = {
        code,
        stdout: Buffer.concat(stdout).toString('utf8'),
        stderr: Buffer.concat(stderr).toString('utf8'),
      };
      if (code === 0) resolve(result);
      else {
        const error = new Error(`${command} exited ${code}`);
        error.result = result;
        reject(error);
      }
    });
  });
}

async function prepareAudioCaptureEnv(env, job) {
  if (process.platform !== 'linux') {
    throw new Error('runwave playtest requires linux for gstreamer audio capture');
  }

  const sink = job.audioSink || env.RUNWAVE_AUDIO_SINK || 'runwave_sink';
  const audioEnv = {
    ...env,
    PULSE_SINK: sink,
  };

  await run('pulseaudio', ['--start', '--exit-idle-time=-1'], { env: audioEnv });
  const modules = await run('pactl', ['list', 'short', 'modules'], { env: audioEnv });
  for (const line of modules.stdout.split(/\r?\n/)) {
    const fields = line.trim().split(/\s+/);
    if (fields[1] === 'module-suspend-on-idle') {
      await run('pactl', ['unload-module', fields[0]], { env: audioEnv });
    }
  }
  const activeModules = await run('pactl', ['list', 'short', 'modules'], { env: audioEnv });
  if (!activeModules.stdout.includes(`sink_name=${sink}`)) {
    await run('pactl', [
      'load-module',
      'module-null-sink',
      `sink_name=${sink}`,
      `sink_properties=device.description=${sink}`,
    ], { env: audioEnv });
  }
  await run('pactl', ['set-default-sink', sink], { env: audioEnv });

  return {
    env: audioEnv,
    audioSource: job.audioSource || `${sink}.monitor`,
  };
}

async function startXvfbForAudio(job, env) {
  if (job.audioXvfb === false) return { env, process: null, display: env.DISPLAY || null };
  const capture = xvfbCaptureConfig(job, env);
  const xvfb = spawnLong('Xvfb', [capture.display, '-screen', '0', capture.screen, '-nolisten', 'tcp'], {
    env,
  });
  const waitMs = Number(job.xvfbStartWaitMs ?? env.RUNWAVE_XVFB_START_WAIT_MS ?? 500);
  if (waitMs > 0) await sleep(waitMs);
  if (processHasClosed(xvfb)) {
    throw new Error(`Xvfb exited during startup for display ${capture.display}`);
  }
  return {
    env: {
      ...env,
      DISPLAY: capture.display,
      NO_AT_BRIDGE: env.NO_AT_BRIDGE || '1',
      RUNWAVE_VIDEO_X: String(capture.captureX),
      RUNWAVE_VIDEO_Y: String(capture.captureY),
    },
    process: xvfb,
    display: capture.display,
  };
}

function xvfbCaptureConfig(job = {}, env = process.env) {
  const display = job.xvfbDisplay || env.RUNWAVE_XVFB_DISPLAY || `:${100 + (Number(job.port || 0) % 500)}`;
  const captureSize = job.videoSize || job.viewport || { width: 1280, height: 720 };
  const captureWidth = positiveInteger(captureSize.width, 1280);
  const captureHeight = positiveInteger(captureSize.height, 720);
  const captureX = nonNegativeInteger(
    job.audioVideoCaptureX ?? env.RUNWAVE_AUDIO_VIDEO_CAPTURE_X ?? env.RUNWAVE_VIDEO_X,
    0
  );
  const captureY = nonNegativeInteger(
    job.audioVideoCaptureY ?? env.RUNWAVE_AUDIO_VIDEO_CAPTURE_Y ?? env.RUNWAVE_VIDEO_Y,
    DEFAULT_AUDIO_VIDEO_CAPTURE_Y
  );
  const paddingX = nonNegativeInteger(
    job.xvfbCapturePaddingX ?? env.RUNWAVE_XVFB_CAPTURE_PADDING_X,
    DEFAULT_XVFB_CAPTURE_PADDING_X
  );
  const paddingY = nonNegativeInteger(
    job.xvfbCapturePaddingY ?? env.RUNWAVE_XVFB_CAPTURE_PADDING_Y,
    DEFAULT_XVFB_CAPTURE_PADDING_Y
  );
  const configuredWidth = positiveInteger(job.xvfbWidth ?? env.RUNWAVE_XVFB_WIDTH, captureWidth + captureX);
  const configuredHeight = positiveInteger(job.xvfbHeight ?? env.RUNWAVE_XVFB_HEIGHT, captureHeight + captureY + paddingY);
  const screenWidth = Math.max(configuredWidth, captureWidth + captureX + paddingX);
  const screenHeight = Math.max(configuredHeight, captureHeight + captureY + paddingY);
  const screen = job.xvfbScreen || env.RUNWAVE_XVFB_SCREEN || `${screenWidth}x${screenHeight}x24`;
  return {
    display,
    captureX,
    captureY,
    captureWidth,
    captureHeight,
    paddingX,
    screenWidth,
    screenHeight,
    screen,
  };
}

function spawnLong(command, args, options = {}) {
  log('process.start', { command, args, cwd: options.cwd });
  const child = spawn(command, args, {
    cwd: options.cwd,
    env: options.env || process.env,
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: options.detached !== false,
  });
  child.stdout.on('data', (chunk) => process.stdout.write(chunk));
  child.stderr.on('data', (chunk) => process.stderr.write(chunk));
  child.on('close', (code) => log('process.end', { command, code }));
  return child;
}

function processHasClosed(child) {
  return Boolean(child && (child.exitCode !== null || child.signalCode !== null));
}

function signalLongProcess(child, signal, options = {}) {
  if (!child || !child.pid) return false;
  const kill = options.kill || process.kill;
  const platform = options.platform || process.platform;
  const target = platform === 'win32' ? child.pid : -child.pid;
  try {
    kill(target, signal);
    return true;
  } catch (error) {
    if (error && error.code === 'ESRCH') return false;
    throw error;
  }
}

function waitForProcessClose(child, timeoutMs) {
  if (!child || processHasClosed(child)) return Promise.resolve(true);
  return new Promise((resolve) => {
    let timer = null;
    const done = (closed) => {
      if (timer) clearTimeout(timer);
      child.off('close', onClose);
      resolve(closed);
    };
    const onClose = () => done(true);
    child.once('close', onClose);
    timer = setTimeout(() => done(processHasClosed(child)), Math.max(0, timeoutMs));
  });
}

async function stopLongProcess(child, options = {}) {
  if (!child) return { stopped: false, reason: 'no-process' };
  if (processHasClosed(child)) return { stopped: true, reason: 'already-closed', pid: child.pid };

  const label = options.label || 'process';
  const termWaitMs = Math.max(0, Number(options.termWaitMs ?? DEFAULT_PROCESS_STOP_WAIT_MS));
  const killWaitMs = Math.max(0, Number(options.killWaitMs ?? DEFAULT_PROCESS_KILL_WAIT_MS));
  const writeLog = options.log || log;
  writeLog('process.stop.start', { label, pid: child.pid, termWaitMs, killWaitMs });

  const termSent = signalLongProcess(child, 'SIGTERM', options);
  if (!termSent) {
    writeLog('process.stop.not_running', { label, pid: child.pid, signal: 'SIGTERM' });
    return { stopped: true, reason: 'not-running', pid: child.pid };
  }

  if (await waitForProcessClose(child, termWaitMs)) {
    writeLog('process.stop.end', { label, pid: child.pid, signal: 'SIGTERM', escalated: false });
    return { stopped: true, reason: 'terminated', pid: child.pid, signal: 'SIGTERM', escalated: false };
  }

  writeLog('process.stop.escalate', { label, pid: child.pid, signal: 'SIGKILL' });
  const killSent = signalLongProcess(child, 'SIGKILL', options);
  if (!killSent) {
    writeLog('process.stop.not_running', { label, pid: child.pid, signal: 'SIGKILL' });
    return { stopped: true, reason: 'not-running-after-term', pid: child.pid, signal: 'SIGTERM', escalated: true };
  }

  const stopped = await waitForProcessClose(child, killWaitMs);
  writeLog('process.stop.end', { label, pid: child.pid, signal: 'SIGKILL', escalated: true, stopped });
  return { stopped, reason: stopped ? 'killed' : 'kill-timeout', pid: child.pid, signal: 'SIGKILL', escalated: true };
}

function waitForHttp(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const check = () => {
      const req = http.get(url, (res) => {
        res.resume();
        if (res.statusCode >= 200 && res.statusCode < 500) resolve();
        else retry();
      });
      req.on('error', retry);
      req.setTimeout(1000, () => {
        req.destroy();
        retry();
      });
    };
    const retry = () => {
      if (Date.now() > deadline) reject(new Error(`timed out waiting for ${url}`));
      else setTimeout(check, 500);
    };
    check();
  });
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

const DEFAULT_CHROMIUM_ARGS = [
  '--no-sandbox',
  '--ignore-gpu-blocklist',
  '--enable-gpu',
  '--use-gl=angle',
  '--use-angle=swiftshader',
  '--enable-unsafe-swiftshader',
  '--autoplay-policy=no-user-gesture-required',
];

function parseArgList(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value.map(String).filter(Boolean);
  const text = String(value).trim();
  if (!text) return [];
  if (text.startsWith('[')) {
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean);
    } catch {
      // Fall through to whitespace parsing.
    }
  }
  return text.split(/\s+/).filter(Boolean);
}

function chromiumArgs(job = {}, env = process.env) {
  const configured = parseArgList(job.chromiumArgs ?? env.RUNWAVE_CHROMIUM_ARGS);
  const mode = String(job.chromiumArgsMode || env.RUNWAVE_CHROMIUM_ARGS_MODE || 'append').toLowerCase();
  if (mode === 'replace') return configured;
  return [...DEFAULT_CHROMIUM_ARGS, ...configured];
}

function chromiumLaunchOptions(job = {}, env = process.env, extra = {}) {
  const launchOptions = {
    ...extra,
    args: chromiumArgs(job, env),
  };
  if (job.channel) launchOptions.channel = String(job.channel);
  if (job.executablePath) launchOptions.executablePath = String(job.executablePath);
  return launchOptions;
}

function responseBody(response) {
  if (!response || typeof response !== 'object') return {};
  return response.output && typeof response.output === 'object' ? response.output : response;
}

function webglFromResponse(response) {
  const body = responseBody(response);
  const state = body.state || body.endState || {};
  return state.generic && state.generic.webgl ? state.generic.webgl : null;
}

function even(value) {
  const rounded = Math.round(value);
  return rounded % 2 === 0 ? rounded : rounded + 1;
}


async function checkoutRunwave(job, runwaveDir, env = process.env) {
  await run('git', ['clone', job.runwaveRepo || 'https://github.com/parsewave/runwave', runwaveDir], { env });
  if (job.runwaveRef) {
    await run('git', ['fetch', '--depth', '1', 'origin', job.runwaveRef], { cwd: runwaveDir, env });
    await run('git', ['checkout', '--detach', 'FETCH_HEAD'], { cwd: runwaveDir, env });
  }
  if (fs.existsSync(path.join(runwaveDir, 'package-lock.json'))) {
    await run('npm', ['ci'], { cwd: runwaveDir, env });
  } else {
    await run('npm', ['install'], { cwd: runwaveDir, env });
  }
  if (job.skipPlaywrightInstall || env.RUNWAVE_SKIP_PLAYWRIGHT_INSTALL === '1') {
    log('playwright.install.skip', { runwaveDir });
    return;
  }
  await run('npx', ['playwright', 'install', 'chromium'], { cwd: runwaveDir, env });
}

async function uploadWorkspace(job, dirs, env) {
  if (!job.s3Uri) return null;
  const s3Uri = job.s3Uri.replace(/\/+$/, '');
  await run('aws', ['s3', 'sync', dirs.workspace, s3Uri, '--only-show-errors'], {
    cwd: dirs.workspace,
    env: { ...process.env, ...env },
  });
  return s3Uri;
}


async function main() {
  const args = parseArgs(process.argv);
  if (!args.job) throw new Error('usage: run-playtest.js --job /path/job.json');

  const job = JSON.parse(fs.readFileSync(args.job, 'utf8'));
  const envFile = loadEnvFile('/etc/runwave-runner.env');
  Object.assign(process.env, envFile);
  const runnerEnv = { ...process.env, ...envFile };
  const runner = runnerPaths(runnerEnv);
  if (shouldRunJobInContainer(job, runnerEnv)) {
    await runJobInContainer(args, job, runner, runnerEnv);
    return;
  }

  const jobId = safeName(job.jobId || `${job.game}-attempt-${job.attempt || 1}-${Date.now()}`);
  const port = Number(job.port || 8800 + Math.floor(Math.random() * 800));
  const root = path.join(runner.jobsRoot, jobId);
  const dirs = {
    root,
    workspace: path.join(root, 'workspace'),
    runwave: path.join(root, 'runwave'),
  };
  mkdirp(dirs.workspace);

  const summary = {
    jobId,
    game: job.game,
    runwaveRepo: job.runwaveRepo,
    runwaveRef: job.runwaveRef,
    startedAt: new Date().toISOString(),
    status: 'running',
  };
  fs.writeFileSync(path.join(root, 'job.json'), JSON.stringify(job, null, 2));
  fs.writeFileSync(path.join(dirs.workspace, 'summary.json'), JSON.stringify(summary, null, 2));

  const gameDir = path.join(runner.gamesRoot, job.game);
  let xvfb = null;

  try {
    if (!fs.existsSync(path.join(gameDir, 'start.sh'))) {
      throw new Error(`game has no start.sh: ${job.game}`);
    }
    const metadataPath = path.join(gameDir, 'metadata.json');
    if (!fs.existsSync(metadataPath)) {
      throw new Error(`game has no metadata.json: ${job.game}`);
    }
    const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));
    const metaViewport = metadata && metadata.viewport;
    if (!metaViewport || !Number.isFinite(metaViewport.width) || !Number.isFinite(metaViewport.height)
      || metaViewport.width <= 0 || metaViewport.height <= 0) {
      throw new Error(`game ${job.game} metadata.json missing viewport {width,height}`);
    }
    job.viewport = { width: Math.round(metaViewport.width), height: Math.round(metaViewport.height) };
    job.videoSize = job.viewport;
    job.xvfbWidth = Math.max(Number(job.xvfbWidth) || 0, job.viewport.width);
    job.xvfbHeight = Math.max(Number(job.xvfbHeight) || 0, job.viewport.height);
    log('job.start', { jobId, game: job.game, port, viewport: job.viewport });
    await checkoutRunwave(job, dirs.runwave, runnerEnv);

    const audioCapture = await prepareAudioCaptureEnv(runnerEnv, job);
    let playtestEnv = audioCapture.env;
    const xvfbSession = await startXvfbForAudio(job, playtestEnv);
    playtestEnv = xvfbSession.env;
    xvfb = xvfbSession.process;
    if (xvfbSession.display) log('xvfb.ready', { display: xvfbSession.display });

    {
      const runPlaytest = loadRunwavePlaytest(dirs.runwave);

      const startOverrides = startOverridesFromJob(job, audioCapture);

      const viewport = job.viewport;
      summary.viewport = viewport;
      const modelConfig = modelEndpointConfig(runnerEnv);
      const legacyOpenRouterKey = !modelConfig.baseUrl
        || String(modelConfig.baseUrl).toLowerCase().includes('openrouter.ai')
        || (!runnerEnv.RUNWAVE_BASE_URL && runnerEnv.OPENROUTER_BASE_URL && modelConfig.apiKey);

      const onInitialResponse = (response) => {
        const webgl = webglFromResponse(response);
        if (webgl) summary.webgl = webgl;
      };

      const playtestResult = await runPlaytest({
        gameDir,
        outDir: dirs.workspace,
        port,
        apiKey: modelConfig.apiKey,
        // Keep old Runwave checkouts usable for the default OpenRouter path.
        // Custom endpoints require a checkout with the provider-neutral API.
        openRouterApiKey: legacyOpenRouterKey ? modelConfig.apiKey : undefined,
        baseUrl: modelConfig.baseUrl,
        playtestDurationMs: job.playtestDurationMs,
        minPlaytestMs: job.agentMinPlaytestMs,
        verbose: job.verboseRunwave || runnerEnv.RUNWAVE_VERBOSE === '1',
        sessionId: job.runwaveSessionId || job.sessionId || job.jobId,
        viewport,
        startOverrides,
        env: playtestEnv,
        onInitialResponse,
        onLog: (event, fields) => log(event, fields),
        processStopWaitMs: Number(job.processStopWaitMs ?? runnerEnv.RUNWAVE_PROCESS_STOP_WAIT_MS ?? DEFAULT_PROCESS_STOP_WAIT_MS),
        processKillWaitMs: Number(job.processKillWaitMs ?? runnerEnv.RUNWAVE_PROCESS_KILL_WAIT_MS ?? DEFAULT_PROCESS_KILL_WAIT_MS),
      });
      if (playtestResult?.playtest) summary.playtest = playtestResult.playtest;
    }
    summary.status = 'passed';
  } catch (error) {
    summary.status = 'failed';
    summary.error = error.message;
    summary.stack = error.stack;
    if (error.summary?.webgl) summary.webgl = error.summary.webgl;
    log('job.error', { jobId, error: error.message });
  } finally {
    summary.finishedAt = new Date().toISOString();
    fs.writeFileSync(path.join(dirs.workspace, 'summary.json'), JSON.stringify(summary, null, 2));
    if (xvfb) {
      summary.xvfbCleanup = await stopLongProcess(xvfb, {
        label: 'xvfb',
        termWaitMs: Number(job.processStopWaitMs ?? runnerEnv.RUNWAVE_PROCESS_STOP_WAIT_MS ?? DEFAULT_PROCESS_STOP_WAIT_MS),
        killWaitMs: Number(job.processKillWaitMs ?? runnerEnv.RUNWAVE_PROCESS_KILL_WAIT_MS ?? DEFAULT_PROCESS_KILL_WAIT_MS),
      }).catch((error) => {
        log('xvfb.stop.error', { jobId, error: error.message });
        return { stopped: false, reason: 'error', error: error.message };
      });
    }
    if (job.s3Uri) {
      summary.uploadTarget = job.s3Uri.replace(/\/+$/, '');
      summary.uploadStatus = 'pending';
    }
    fs.writeFileSync(path.join(dirs.workspace, 'summary.json'), JSON.stringify(summary, null, 2));
    const uploadedTo = await uploadWorkspace(job, dirs, runnerEnv).catch((error) => {
      log('upload.error', { jobId, error: error.message });
      summary.uploadStatus = 'failed';
      summary.uploadError = error.message;
      fs.writeFileSync(path.join(dirs.workspace, 'summary.json'), JSON.stringify(summary, null, 2));
      return null;
    });
    if (uploadedTo) {
      summary.uploadedTo = uploadedTo;
      summary.uploadStatus = 'uploaded';
    } else if (job.s3Uri && summary.uploadStatus !== 'failed') {
      summary.uploadStatus = 'skipped';
    }
    fs.writeFileSync(path.join(dirs.workspace, 'summary.json'), JSON.stringify(summary, null, 2));
    log('job.end', { jobId, status: summary.status, uploadedTo });
  }

  if (summary.status !== 'passed') process.exitCode = 1;
}

if (require.main === module) {
  main().catch((error) => {
    log('fatal', { error: error.message, stack: error.stack });
    process.exit(1);
  });
}

module.exports = {
  chromiumArgs,
  dockerRunArgs,
  loadRunwavePlaytest,
  loadPlaytestInstructions,
  modelEndpointConfig,
  shouldRunJobInContainer,
  signalLongProcess,
  startOverridesFromJob,
  stopLongProcess,
  waitForProcessClose,
  xvfbCaptureConfig,
};
