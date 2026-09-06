'use strict';

const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  dockerRunArgs,
  loadRunwavePlaytest,
  loadPlaytestInstructions,
  modelEndpointConfig,
  shouldRunJobInContainer,
  signalLongProcess,
  startOverridesFromJob,
  stopLongProcess,
  xvfbCaptureConfig,
} = require('../remote/run-playtest');

function fakeChild(pid = 12345) {
  const child = new EventEmitter();
  child.pid = pid;
  child.exitCode = null;
  child.signalCode = null;
  return child;
}

function closeWithSignal(child, signal) {
  child.signalCode = signal;
  child.emit('close', null, signal);
}

test('remote process signaling targets the detached process group on unix', () => {
  const calls = [];
  const child = fakeChild(23456);

  const sent = signalLongProcess(child, 'SIGTERM', {
    platform: 'linux',
    kill: (target, signal) => calls.push({ target, signal }),
  });

  assert.equal(sent, true);
  assert.deepEqual(calls, [{ target: -23456, signal: 'SIGTERM' }]);
});

test('remote process cleanup stops after a clean SIGTERM close', async () => {
  const calls = [];
  const child = fakeChild(23456);

  const result = await stopLongProcess(child, {
    label: 'test-game',
    log: () => {},
    platform: 'linux',
    termWaitMs: 100,
    killWaitMs: 100,
    kill: (target, signal) => {
      calls.push({ target, signal });
      if (signal === 'SIGTERM') setImmediate(() => closeWithSignal(child, signal));
    },
  });

  assert.deepEqual(calls, [{ target: -23456, signal: 'SIGTERM' }]);
  assert.deepEqual(result, {
    stopped: true,
    reason: 'terminated',
    pid: 23456,
    signal: 'SIGTERM',
    escalated: false,
  });
});

test('remote process cleanup escalates to SIGKILL after SIGTERM timeout', async () => {
  const calls = [];
  const child = fakeChild(23456);

  const result = await stopLongProcess(child, {
    label: 'test-game',
    log: () => {},
    platform: 'linux',
    termWaitMs: 1,
    killWaitMs: 100,
    kill: (target, signal) => {
      calls.push({ target, signal });
      if (signal === 'SIGKILL') setImmediate(() => closeWithSignal(child, signal));
    },
  });

  assert.deepEqual(calls, [
    { target: -23456, signal: 'SIGTERM' },
    { target: -23456, signal: 'SIGKILL' },
  ]);
  assert.deepEqual(result, {
    stopped: true,
    reason: 'killed',
    pid: 23456,
    signal: 'SIGKILL',
    escalated: true,
  });
});

test('loads exact lowercase playtest.md instructions for a game', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-playtest-md-'));
  fs.writeFileSync(path.join(dir, 'playtest.md'), '# Playtest Controls\n\n- Start: Enter.\n');

  assert.equal(loadPlaytestInstructions(dir, 'example-game'), '# Playtest Controls\n\n- Start: Enter.\n');
});

test('rejects missing or mis-cased playtest.md instructions', () => {
  const missingDir = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-playtest-md-missing-'));
  assert.throws(
    () => loadPlaytestInstructions(missingDir, 'missing-game'),
    /game has no playtest\.md: missing-game/
  );

  const casedDir = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-playtest-md-cased-'));
  fs.writeFileSync(path.join(casedDir, 'PLAYTEST.md'), '# Playtest Controls\n');
  assert.throws(
    () => loadPlaytestInstructions(casedDir, 'cased-game'),
    /game has no playtest\.md: cased-game/
  );
});

test('remote playtest start overrides carry mark grid dimensions', () => {
  const overrides = startOverridesFromJob(
    {
      markGridRows: 16,
      markGridCols: 24,
      gridScreenshots: true,
      videoSize: { width: 1280, height: 720 },
      launchSettleMs: 30000,
    },
    { audioSource: 'pulse.monitor' }
  );

  assert.equal(overrides.markGridRows, 16);
  assert.equal(overrides.markGridCols, 24);
  assert.equal(overrides.gridScreenshots, true);
  assert.equal(overrides.launchSettleMs, 30000);
  assert.equal(overrides.audioSource, 'pulse.monitor');
  assert.deepEqual(overrides.videoSize, { width: 1280, height: 720 });
});

test('xvfb capture config leaves room for measured browser chrome', () => {
  const capture = xvfbCaptureConfig(
    {
      port: 9311,
      viewport: { width: 656, height: 496 },
      videoSize: { width: 656, height: 496 },
      xvfbWidth: 656,
      xvfbHeight: 496,
    },
    {}
  );

  assert.equal(capture.captureX, 0);
  assert.equal(capture.captureY, 0);
  assert.equal(capture.paddingX, 64);
  assert.equal(capture.screenWidth, 720);
  assert.equal(capture.screenHeight, 656);
  assert.equal(capture.screen, '720x656x24');
});

test('xvfb capture config pads exact-width viewports for measured X offsets', () => {
  const capture = xvfbCaptureConfig(
    {
      port: 9311,
      viewport: { width: 660, height: 1000 },
      videoSize: { width: 660, height: 1000 },
      xvfbWidth: 660,
      xvfbHeight: 1000,
    },
    {}
  );

  assert.equal(capture.screenWidth, 724);
  assert.equal(capture.screenHeight, 1160);
  assert.equal(capture.screen, '724x1160x24');
});

test('linux playtest jobs run in a container by default unless disabled or already inside one', () => {
  assert.equal(shouldRunJobInContainer({}, {}, 'linux'), true);
  assert.equal(shouldRunJobInContainer({}, { RUNWAVE_IN_CONTAINER: '1' }, 'linux'), false);
  assert.equal(shouldRunJobInContainer({}, { RUNWAVE_PLAYTEST_CONTAINER: '0' }, 'linux'), false);
  assert.equal(shouldRunJobInContainer({ containerized: false }, {}, 'linux'), false);
  assert.equal(shouldRunJobInContainer({}, {}, 'darwin'), false);
});

test('docker runner isolates games and job workspaces without exposing secret values in args', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-docker-args-'));
  const gamesRoot = path.join(root, 'games');
  const jobsRoot = path.join(root, 'jobs');
  const inputRoot = path.join(root, 'input');
  const script = path.join(root, 'run-playtest.js');
  const envFile = path.join(root, 'runner.env');
  fs.mkdirSync(gamesRoot);
  fs.mkdirSync(jobsRoot);
  fs.mkdirSync(inputRoot);
  fs.writeFileSync(path.join(inputRoot, 'job.json'), '{}');
  fs.writeFileSync(script, '#!/usr/bin/env node\n');
  fs.writeFileSync(envFile, "export AWS_SECRET_ACCESS_KEY='hidden'\n");

  const args = dockerRunArgs(
    { job: path.join(inputRoot, 'job.json') },
    { jobId: 'run-123-mario-attempt-001' },
    { gamesRoot, jobsRoot },
    {
      env: {
        AWS_ACCESS_KEY_ID: 'id-value',
        AWS_SECRET_ACCESS_KEY: 'secret-value',
        RUNWAVE_PLAYTEST_IMAGE: 'custom-runner:test',
      },
      scriptPath: script,
      envFile,
    }
  );

  assert.equal(args[0], 'run');
  assert.ok(args.includes('--rm'));
  assert.ok(args.includes('--init'));
  assert.ok(args.includes('--ipc=host'));
  assert.ok(args.includes('--shm-size=1g'));
  assert.ok(args.includes('custom-runner:test'));
  assert.ok(args.includes('/runwave/job/job.json'));
  assert.ok(args.includes(`${gamesRoot}:/opt/runwave/games:ro`));
  assert.ok(args.includes(`${jobsRoot}:/var/lib/runwave/jobs`));
  assert.ok(args.includes(`${inputRoot}:/runwave/job:ro`));
  assert.ok(args.includes(`${script}:/opt/runwave/bin/run-playtest.js:ro`));
  assert.ok(args.includes(`${envFile}:/etc/runwave-runner.env:ro`));
  assert.ok(args.includes('RUNWAVE_IN_CONTAINER=1'));
  assert.ok(args.includes('AWS_ACCESS_KEY_ID'));
  assert.ok(args.includes('AWS_SECRET_ACCESS_KEY'));
  assert.equal(args.includes('id-value'), false);
  assert.equal(args.includes('secret-value'), false);
});

test('docker runner forwards generic model endpoint credentials by name only', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-docker-model-'));
  const gamesRoot = path.join(root, 'games');
  const jobsRoot = path.join(root, 'jobs');
  const inputRoot = path.join(root, 'input');
  fs.mkdirSync(gamesRoot);
  fs.mkdirSync(jobsRoot);
  fs.mkdirSync(inputRoot);
  fs.writeFileSync(path.join(inputRoot, 'job.json'), '{}');

  const args = dockerRunArgs(
    { job: path.join(inputRoot, 'job.json') },
    { jobId: 'run-generic-model' },
    { gamesRoot, jobsRoot },
    {
      env: {
        RUNWAVE_API_KEY: 'generic-secret',
        RUNWAVE_BASE_URL: 'https://example.test/v1',
        RUNWAVE_PROMPT_MODE: 'webapp',
        GEMINI_API_KEY: 'gemini-secret',
      },
      scriptPath: path.join(root, 'run-playtest.js'),
      envFile: path.join(root, 'missing.env'),
    }
  );

  assert.ok(args.includes('RUNWAVE_API_KEY'));
  assert.ok(args.includes('RUNWAVE_BASE_URL'));
  assert.ok(args.includes('RUNWAVE_PROMPT_MODE'));
  assert.ok(args.includes('GEMINI_API_KEY'));
  assert.equal(args.includes('generic-secret'), false);
  assert.equal(args.includes('gemini-secret'), false);
});

test('remote model endpoint selection follows the configured provider', () => {
  assert.deepEqual(
    modelEndpointConfig({
      RUNWAVE_BASE_URL: 'https://generativelanguage.googleapis.com/v1beta/openai',
      GEMINI_API_KEY: 'gemini-key',
      OPENROUTER_API_KEY: 'router-key',
    }),
    {
      apiKey: 'gemini-key',
      baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai',
    }
  );
  assert.deepEqual(
    modelEndpointConfig({ OPENROUTER_API_KEY: 'router-key' }),
    { apiKey: 'router-key', baseUrl: null }
  );
  assert.deepEqual(
    modelEndpointConfig({
      RUNWAVE_BASE_URL: 'https://example.test/v1',
      OPENROUTER_API_KEY: 'router-key',
      OPENAI_API_KEY: 'openai-key',
    }),
    { apiKey: 'openai-key', baseUrl: 'https://example.test/v1' }
  );
});

test('docker runner mounts absolute local runwave repos into the container', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-docker-repo-'));
  const gamesRoot = path.join(root, 'games');
  const jobsRoot = path.join(root, 'jobs');
  const inputRoot = path.join(root, 'input');
  const repoRoot = path.join(root, 'runwave-source');
  fs.mkdirSync(gamesRoot);
  fs.mkdirSync(jobsRoot);
  fs.mkdirSync(inputRoot);
  fs.mkdirSync(repoRoot);
  fs.writeFileSync(path.join(inputRoot, 'job.json'), '{}');

  const args = dockerRunArgs(
    { job: path.join(inputRoot, 'job.json') },
    { jobId: 'run-123-local-repo-attempt-001', runwaveRepo: repoRoot },
    { gamesRoot, jobsRoot },
    { env: {}, scriptPath: path.join(root, 'run-playtest.js'), envFile: path.join(root, 'missing.env') }
  );

  assert.ok(args.includes(`${repoRoot}:${repoRoot}:ro`));
});

test('remote runner loads runPlaytest from current package layout', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-module-current-'));
  const packageDir = path.join(root, 'runwave');
  fs.mkdirSync(packageDir);
  fs.writeFileSync(
    path.join(packageDir, 'index.js'),
    'module.exports = { runPlaytest: () => "current" };\n'
  );

  assert.equal(loadRunwavePlaytest(root)(), 'current');
});

test('remote runner loads runPlaytest from GitHub main package layout', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-module-main-'));
  const packageDir = path.join(root, 'playtest');
  fs.mkdirSync(packageDir);
  fs.writeFileSync(
    path.join(packageDir, 'playtest.js'),
    'module.exports = { runPlaytest: () => "main" };\n'
  );

  assert.equal(loadRunwavePlaytest(root)(), 'main');
});
