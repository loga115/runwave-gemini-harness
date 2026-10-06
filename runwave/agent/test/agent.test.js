'use strict';

const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const test = require('node:test');
const { PNG } = require('pngjs');

const { normalizeSequence } = require('../src/action-parser');
const { actionsWithinDuration, failedActionAfterInvalidJson, runAgenticPlaytest } = require('../src/agent-player');
const {
  chatCompletion,
  chatCompletionsUrl,
  parseJsonResponse,
  resolveApiKey,
  resolveBaseUrl,
  resolveModel,
} = require('../src/model-client');
const {
  buildPlaytesterPrompt,
  compactHistory,
  normalizePromptMode,
  sequenceSchemaGuide,
} = require('../src/prompt');
const { drawMarkGridOnScreenshot } = require('../../controller/src/grid-overlay');
const { gridSafeSampleRatio, randomPointInCells } = require('../../protocol/src/mark-grid');
const { normalizeStep } = require('../../controller/src/step-normalizer');

test('normalizes model sequences into controller steps', () => {
  const sequence = normalizeSequence(
    {
      actions: [
        { type: 'key', start: 0, end: 5000, key: 'ArrowRight' },
        { type: 'key', start: 100, end: 900, key: 'Shift+ArrowRight+Space' },
        { type: 'click', start: 50, x: 0.5, y: 0.25 },
        { type: 'drag', start: 100, from: { x: 0.2, y: 0.25 }, to: { x: 0.4, y: 0.25 }, mode: 'html5' },
        { type: 'view_move', start: 0, end: 500, dx: 50, dy: -10 },
      ],
      should_stop: true,
      summary: 'menu is visible',
      previous_sequence_outcome: 'Enter opened the menu.',
    },
    { viewport: { width: 1000, height: 600 } }
  );

  assert.equal(sequence.durationMs, 5000);
  assert.deepEqual(sequence.actions.filter((action) => action.type === 'key'), [
    { type: 'key', start: 0, end: 5000, key: 'ArrowRight' },
    { type: 'key', start: 100, end: 900, key: 'Shift' },
    { type: 'key', start: 100, end: 900, key: 'ArrowRight' },
    { type: 'key', start: 100, end: 900, key: 'Space' },
  ]);
  const click = sequence.actions.find((action) => action.type === 'click');
  const drag = sequence.actions.find((action) => action.type === 'drag');
  const viewMove = sequence.actions.find((action) => action.type === 'view_move');
  assert.equal(click.x, 500);
  assert.equal(click.y, 150);
  assert.equal(click.end, 100);
  assert.deepEqual(drag, {
    type: 'drag',
    start: 100,
    end: 150,
    from: { x: 200, y: 150 },
    to: { x: 400, y: 150 },
    button: 'left',
    mode: 'html5',
    steps: 12,
  });
  assert.equal(viewMove.dx, 50);
  assert.equal(sequence.shouldStop, true);
  assert.equal(sequence.previousSequenceOutcome, 'Enter opened the menu.');
});

test('normalizes grid-cell model actions into concrete pointer events', () => {
  const sequence = normalizeSequence(
    {
      actions: [
        { type: 'click', start: 100, cells: [9] },
        { type: 'multi_click', start: 200, cells: [18, 19], count: 10 },
        { type: 'drag', start: 300, from_cells: [34], to_cells: [35], mode: 'mouse' },
        { type: 'cursor_move', start: 400, cells: [27] },
      ],
    },
    { viewport: { width: 800, height: 800 }, config: { markGridRows: 20, markGridCols: 20 } }
  );

  const clicks = sequence.actions.filter((action) => action.type === 'click');
  const drag = sequence.actions.find((action) => action.type === 'drag');
  const cursorMove = sequence.actions.find((action) => action.type === 'cursor_move');
  assert.equal(clicks.length, 11);
  assert.ok(clicks.every((click) => !Object.hasOwn(click, 'clickMode')));
  assert.equal(clicks[0].cells[0], 9);
  assert.equal(clicks[0].end, 150);
  assert.ok(clicks[0].x >= 360 && clicks[0].x <= 399);
  assert.ok(clicks[0].y >= 0 && clicks[0].y <= 39);
  assert.deepEqual(clicks.slice(1).map((click) => click.start), [200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100]);
  assert.deepEqual(clicks.slice(1).map((click) => click.end), [250, 350, 450, 550, 650, 750, 850, 950, 1050, 1150]);
  assert.ok(clicks.slice(1).every((click) => click.x >= 720 && click.x <= 799));
  assert.ok(clicks.slice(1).every((click) => click.y >= 0 && click.y <= 39));
  assert.deepEqual(drag.from.cells, [34]);
  assert.deepEqual(drag.to.cells, [35]);
  assert.equal(drag.end, 350);
  assert.deepEqual(cursorMove.cells, [27]);
  assert.equal(cursorMove.end, 450);

  const step = normalizeStep({ actions: sequence.actions }, { viewport: { width: 800, height: 800 } }, 1);
  assert.equal(step.clicks.length, 11);
});

test('normalizes overlay row and column grid targets into concrete pointer events', () => {
  const sequence = normalizeSequence(
    {
      actions: [
        { type: 'click', start: 100, cell: { overlay_row: 1, overlay_col: 2 } },
        { type: 'multi_click', start: 200, cells: [{ overlay_row: 2, overlay_col: 0 }, { overlay_row: 2, overlay_col: 1 }], count: 2 },
        { type: 'drag', start: 300, from: { overlay_row: 3, overlay_col: 4 }, to: { overlay_row: 3, overlay_col: 5 }, mode: 'mouse' },
        { type: 'cursor_move', start: 400, cell: { overlay_row: 0, overlay_col: 7 } },
      ],
    },
    { viewport: { width: 800, height: 800 }, config: { markGridRows: 8, markGridCols: 8 } }
  );

  const clicks = sequence.actions.filter((action) => action.type === 'click');
  const drag = sequence.actions.find((action) => action.type === 'drag');
  const cursorMove = sequence.actions.find((action) => action.type === 'cursor_move');
  assert.equal(clicks[0].cells[0], 10);
  assert.ok(clicks[0].x >= 200 && clicks[0].x <= 299);
  assert.ok(clicks[0].y >= 100 && clicks[0].y <= 199);
  assert.equal(clicks.slice(1).length, 2);
  assert.ok(clicks.slice(1).every((click) => [16, 17].includes(click.cells[0])));
  assert.deepEqual(drag.from.cells, [28]);
  assert.deepEqual(drag.to.cells, [29]);
  assert.deepEqual(cursorMove.cells, [7]);
});

test('keeps legacy row and column grid targets compatible', () => {
  const sequence = normalizeSequence(
    {
      actions: [
        { type: 'click', start: 100, cell: { row: 1, col: 2 } },
        { type: 'drag', start: 300, from: { row: 3, col: 4 }, to: { row: 3, col: 5 }, mode: 'mouse' },
      ],
    },
    { viewport: { width: 800, height: 800 }, config: { markGridRows: 8, markGridCols: 8 } }
  );

  const click = sequence.actions.find((action) => action.type === 'click');
  const drag = sequence.actions.find((action) => action.type === 'drag');
  assert.equal(click.cells[0], 10);
  assert.deepEqual(drag.from.cells, [28]);
  assert.deepEqual(drag.to.cells, [29]);
});

test('samples grid-cell points away from cell boundaries by default', () => {
  const grid = { rows: 8, cols: 8 };
  const viewport = { width: 800, height: 800 };
  const lower = randomPointInCells([0], viewport, grid, () => 0);
  const upper = randomPointInCells([0], viewport, grid, () => 0.999999);

  assert.deepEqual(lower, { x: 5, y: 5, cells: [0] });
  assert.deepEqual(upper, { x: 95, y: 95, cells: [0] });
  assert.equal(gridSafeSampleRatio({}), 0.9);
  assert.equal(gridSafeSampleRatio({ markGridSafeSampleRatio: 0.95 }), 0.95);
  assert.equal(gridSafeSampleRatio({ gridSafeSampleRatio: 1 }), 1);
  assert.equal(gridSafeSampleRatio({ gridSafeSampleRatio: 0 }), 0.9);
});

test('rejects model sequence fields outside the canonical schema', () => {
  assert.throws(
    () => normalizeSequence({ summary: 'ok', actions: [], extra: true }, { viewport: { width: 800, height: 800 } }),
    /unknown field "extra"/
  );
  assert.throws(
    () => normalizeSequence({ actions: [{ type: 'key', start: 0, end: 100, key: 'Enter', power: 2 }] }, { viewport: { width: 800, height: 800 } }),
    /unknown field "power"/
  );
  assert.throws(
    () => normalizeSequence({ actions: [{ type: 'wait', start: 0 }] }, { viewport: { width: 800, height: 800 } }),
    /unknown sequence action type: wait/
  );
});

test('rejects model sequences that cannot produce application actions', () => {
  assert.throws(
    () => normalizeSequence([], { viewport: { width: 800, height: 800 } }),
    /model sequence must be a plain object/
  );
  assert.throws(
    () => normalizeSequence({ summary: 'bad stop', actions: [], should_stop: 'false' }, { viewport: { width: 800, height: 800 } }),
    /should_stop must be a boolean/
  );
  assert.throws(
    () => normalizeSequence({ summary: 'bad stop', actions: [], should_stop: 1 }, { viewport: { width: 800, height: 800 } }),
    /should_stop must be a boolean/
  );
  assert.throws(
    () => normalizeSequence({ summary: 'thinking only' }, { viewport: { width: 800, height: 800 } }),
    /actions must contain at least one action/
  );
  assert.throws(
    () => normalizeSequence({ summary: 'no action', actions: [] }, { viewport: { width: 800, height: 800 } }),
    /actions must contain at least one action/
  );

  const stopOnly = normalizeSequence(
    { summary: 'done', actions: [], should_stop: true },
    { viewport: { width: 800, height: 800 } }
  );
  assert.equal(stopOnly.shouldStop, true);
  assert.deepEqual(stopOnly.actions, []);
});

test('drops model actions with invalid short-action timing', () => {
  const sequence = normalizeSequence(
    {
      actions: [
        { type: 'click', start: 0, end: 500, x: 10, y: 10 },
        { type: 'drag', start: 0, end: 2501, from: { x: 0, y: 0 }, to: { x: 10, y: 10 } },
        { type: 'cursor_move', start: 0, end: 2501, to: { x: 10, y: 10 } },
        { type: 'key', start: 0, end: 5000, key: 'ArrowRight' },
      ],
    },
    { viewport: { width: 800, height: 800 } }
  );

  assert.deepEqual(sequence.actions.map((action) => action.type), ['key']);
  assert.equal(sequence.durationMs, 5000);
});

test('rejects model sequences when every requested action is dropped', () => {
  assert.throws(
    () => normalizeSequence(
      {
        actions: [
          { type: 'click', start: 0, end: 500, x: 10, y: 10 },
          { type: 'drag', start: 0, end: 2501, from: { x: 0, y: 0 }, to: { x: 10, y: 10 } },
        ],
      },
      { viewport: { width: 800, height: 800 } }
    ),
    /all model actions were invalid after normalization/
  );
});

test('clips agent actions to the shortened remaining step duration', () => {
  const actions = actionsWithinDuration(
    [
      { type: 'key', start: 0, end: 800, key: 'ArrowRight' },
      { type: 'drag', start: 100, end: 600, from: { x: 1, y: 1 }, to: { x: 2, y: 2 } },
      { type: 'click', start: 490, end: 540, x: 10, y: 10 },
      { type: 'click', start: 500, end: 550, x: 20, y: 20 },
      { type: 'click', start: 900, end: 950, x: 30, y: 30 },
    ],
    500
  );

  assert.deepEqual(actions, [
    { type: 'key', start: 0, end: 500, key: 'ArrowRight' },
    { type: 'drag', start: 100, end: 500, from: { x: 1, y: 1 }, to: { x: 2, y: 2 } },
    { type: 'click', start: 490, end: 500, x: 10, y: 10 },
  ]);
});

test('normalizes controller grid-cell steps into concrete pointer events', () => {
  const step = normalizeStep(
    {
      actions: [
        { type: 'click', start: 100, cells: [0] },
        { type: 'multi_click', start: 200, cells: [255], count: 3 },
        { type: 'drag', start: 300, from_cells: [20], to_cells: [39] },
        { type: 'cursor_move', start: 400, cells: [19] },
      ],
    },
    { viewport: { width: 800, height: 800 }, markGridRows: 20, markGridCols: 20 },
    1
  );

  assert.equal(step.duration, 450);
  assert.equal(step.clicks.length, 4);
  assert.deepEqual(step.clicks.slice(1).map((click) => click.start), [200, 300, 400]);
  assert.deepEqual(step.clicks.slice(1).map((click) => click.end), [250, 350, 450]);
  assert.ok(step.clicks[0].x >= 0 && step.clicks[0].x <= 39);
  assert.ok(step.clicks[0].y >= 0 && step.clicks[0].y <= 39);
  assert.ok(step.clicks.slice(1).every((click) => click.x >= 600 && click.x <= 639));
  assert.ok(step.clicks.slice(1).every((click) => click.y >= 480 && click.y <= 519));
  assert.deepEqual(step.drags[0].from.cells, [20]);
  assert.deepEqual(step.drags[0].to.cells, [39]);
  assert.deepEqual(step.cursorMoves[0].to.cells, [19]);
});

test('normalizes controller overlay row and column grid steps in strict mode', () => {
  const step = normalizeStep(
    {
      actions: [
        { type: 'click', start: 100, cell: { overlay_row: 1, overlay_col: 2 } },
        { type: 'multi_click', start: 200, cells: [{ overlay_row: 2, overlay_col: 0 }, { overlay_row: 2, overlay_col: 1 }], count: 2 },
        { type: 'drag', start: 300, from: { overlay_row: 3, overlay_col: 4 }, to: { overlay_row: 3, overlay_col: 5 } },
        { type: 'cursor_move', start: 400, cell: { overlay_row: 0, overlay_col: 7 } },
      ],
    },
    { viewport: { width: 800, height: 800 }, markGridRows: 8, markGridCols: 8 },
    1
  );

  assert.equal(step.duration, 450);
  assert.equal(step.clicks[0].cells[0], 10);
  assert.ok(step.clicks[0].x >= 200 && step.clicks[0].x <= 299);
  assert.ok(step.clicks[0].y >= 100 && step.clicks[0].y <= 199);
  assert.equal(step.clicks.slice(1).length, 2);
  assert.ok(step.clicks.slice(1).every((click) => [16, 17].includes(click.cells[0])));
  assert.deepEqual(step.drags[0].from.cells, [28]);
  assert.deepEqual(step.drags[0].to.cells, [29]);
  assert.deepEqual(step.cursorMoves[0].to.cells, [7]);
});

test('mark grid overlay keeps labels in the margins', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-grid-overlay-test-'));
  const file = path.join(dir, 'screen.png');
  const png = new PNG({ width: 240, height: 160 });
  for (let y = 0; y < png.height; y += 1) {
    for (let x = 0; x < png.width; x += 1) {
      const index = (png.width * y + x) << 2;
      png.data[index] = x % 256;
      png.data[index + 1] = y % 256;
      png.data[index + 2] = (x + y) % 256;
      png.data[index + 3] = 255;
    }
  }
  fs.writeFileSync(file, PNG.sync.write(png));

  drawMarkGridOnScreenshot(file, { markGridRows: 20, markGridCols: 20 });
  const output = PNG.sync.read(fs.readFileSync(file));
  const marginX = (output.width - png.width) / 2;
  const marginY = (output.height - png.height) / 2;
  const cellWidth = png.width / 20;
  const cellHeight = png.height / 20;

  assert.ok(output.width > png.width);
  assert.ok(output.height > png.height);
  assert.equal(marginX, marginY);
  assert.ok(marginX > 0);

  for (let y = 1; y < png.height - 1; y += 1) {
    for (let x = 1; x < png.width - 1; x += 1) {
      const nearVerticalLine = Math.abs((x / cellWidth) - Math.round(x / cellWidth)) < 0.08;
      const nearHorizontalLine = Math.abs((y / cellHeight) - Math.round(y / cellHeight)) < 0.12;
      if (nearVerticalLine || nearHorizontalLine) continue;

      const sourceIndex = (png.width * y + x) << 2;
      const outputIndex = (output.width * (y + marginY) + x + marginX) << 2;
      assert.equal(output.data[outputIndex], png.data[sourceIndex]);
      assert.equal(output.data[outputIndex + 1], png.data[sourceIndex + 1]);
      assert.equal(output.data[outputIndex + 2], png.data[sourceIndex + 2]);
    }
  }
});

test('infers controller duration from action timing fields only', () => {
  const step = normalizeStep(
    {
      actions: [
        { type: 'drag', start: 100, from: { x: 854, y: 156 }, to: { x: 1012, y: 158 }, mode: 'mouse' },
        { type: 'cursor_move', start: 500, to: { x: 347, y: 345 }, steps: 5 },
        { type: 'drag', start: 1000, from: { x: 609, y: 331 }, to: { x: 605, y: 202 }, mode: 'mouse' },
      ],
    },
    { viewport: { width: 1280, height: 720 } },
    1
  );

  assert.equal(step.duration, 1050);
  assert.deepEqual(step.captures, [1000, 1050]);
  assert.equal(step.drags.length, 2);
  assert.equal(step.cursorMoves.length, 1);
});

test('infers controller duration from key ends and multi-click intervals', () => {
  const keyStep = normalizeStep(
    { actions: [{ type: 'key', start: 0, end: 700, key: 'Space' }] },
    {},
    1
  );
  const multiClickStep = normalizeStep(
    { actions: [{ type: 'multi_click', start: 200, x: 100, y: 100, count: 4, intervalMs: 150 }] },
    { viewport: { width: 800, height: 800 } },
    2
  );

  assert.equal(keyStep.duration, 700);
  assert.equal(multiClickStep.duration, 700);
  assert.deepEqual(multiClickStep.clicks.map((click) => click.start), [200, 350, 500, 650]);
  assert.deepEqual(multiClickStep.clicks.map((click) => click.end), [250, 400, 550, 700]);
});

test('rejects controller short actions with impossible end timings', () => {
  assert.throws(
    () => normalizeStep({ actions: [{ type: 'click', start: 0, end: 500, x: 1, y: 1 }] }, { viewport: { width: 800, height: 800 } }, 1),
    /click action duration exceeds 100ms/
  );
  assert.throws(
    () => normalizeStep(
      { actions: [{ type: 'drag', start: 0, end: 2501, from: { x: 1, y: 1 }, to: { x: 2, y: 2 } }] },
      { viewport: { width: 800, height: 800 } },
      1
    ),
    /drag action duration exceeds 2000ms/
  );
});

test('rejects controller step fields outside the canonical schema', () => {
  const explicit = normalizeStep({ duration: 1000, actions: [], captures: [1000], autoCaptures: false }, {}, 1);
  const verbose = normalizeStep({ duration: 1000, actions: [], captures: [1000], __runwaveVerbose: true }, {}, 2);
  assert.equal(explicit.duration, 1000);
  assert.deepEqual(explicit.captures, [1000]);
  assert.equal(verbose.duration, 1000);
  assert.deepEqual(verbose.captures, [1000]);

  assert.throws(
    () => normalizeStep({ actions: [], surprise: true }, {}, 1),
    /unknown field "surprise"/
  );
  assert.throws(
    () => normalizeStep({ actions: [{ type: 'key', start: 100, end: 400, key: 'right', power: 2 }] }, {}, 1),
    /unknown field "power"/
  );
  assert.throws(
    () => normalizeStep({ actions: [{ type: 'wait', start: 0 }] }, {}, 1),
    /unknown sequence action type: wait/
  );
});

test('agent playtest loop calls model and executes returned sequence', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-agent-test-'));
  const screenshot = path.join(dir, 'screen.png');
  const afterScreenshot = path.join(dir, 'after-screen.png');
  fs.writeFileSync(
    screenshot,
    Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=', 'base64')
  );
  fs.writeFileSync(afterScreenshot, 'different screenshot bytes');

  const controllerSteps = [];
  const result = await runAgenticPlaytest({
    job: {
      playtestDurationMs: 7000,
      agentMinPlaytestMs: 0,
      viewport: { width: 640, height: 360 },
    },
    initialResponse: {
      screenshot,
      state: { url: 'http://example.test' },
    },
    outputDir: path.join(dir, 'agent'),
    modelClient: async () => ({
      model: 'fake-model',
      usage: { total_tokens: 1 },
      json: {
        summary: 'start screen is visible',
        actions: [
          { type: 'click', start: 0, x: 320, y: 180 },
          { type: 'drag', start: 50, from: { x: 100, y: 100 }, to: { x: 130, y: 100 }, mode: 'mouse' },
          { type: 'key', start: 0, end: 500, key: 'Enter' },
        ],
        should_stop: true,
      },
    }),
    runAction: async (step) => {
      controllerSteps.push(step);
      return { ok: true, captures: [{ path: afterScreenshot }], endState: { ok: true } };
    },
  });

  assert.equal(result.steps, 1);
  assert.equal(controllerSteps.length, 1);
  assert.equal(controllerSteps[0].action, 'step');
  assert.equal(controllerSteps[0].duration, 500);
  assert.deepEqual(controllerSteps[0].captures, [500]);
  assert.equal(controllerSteps[0].actions.find((action) => action.type === 'key').key, 'Enter');
  assert.equal(controllerSteps[0].actions.find((action) => action.type === 'drag').mode, 'mouse');
  assert.equal(controllerSteps[0].actions.find((action) => action.type === 'drag').end, 100);
  assert.equal(result.history[0].result.ok, true);
  assert.equal(result.history[0].result.screenshot, afterScreenshot);
  assert.equal(result.history[0].result.screenshotChanged, true);
  assert.deepEqual(result.history[0].result.state, { ok: true });
  const promptLog = fs
    .readFileSync(path.join(dir, 'agent', 'agent-prompts.jsonl'), 'utf8')
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line));
  assert.equal(promptLog.length, 1);
  assert.equal(promptLog[0].step, 1);
  assert.equal(promptLog[0].screenshot, screenshot);
  assert.match(promptLog[0].prompt, /You are an agentic game playtester/);
  assert.equal(fs.existsSync(path.join(dir, 'agent', 'agent-summary.json')), true);
});

test('agent loop records invalid JSON as a failed action and retries with the same screenshot', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-agent-json-failed-action-test-'));
  const screenshot = path.join(dir, 'screen.png');
  const afterScreenshot = path.join(dir, 'after-screen.png');
  fs.writeFileSync(screenshot, 'initial screenshot bytes');
  fs.writeFileSync(afterScreenshot, 'after screenshot bytes');

  const modelScreenshots = [];
  let calls = 0;
  const controllerActions = [];
  const result = await runAgenticPlaytest({
    job: {
      playtestDurationMs: 9000,
      agentMinPlaytestMs: 0,
      viewport: { width: 640, height: 360 },
      agentMaxModelFallbacks: 2,
    },
    initialResponse: {
      screenshot,
      state: { url: 'http://example.test', screen: 'initial' },
    },
    outputDir: path.join(dir, 'agent'),
    modelClient: async ({ messages }) => {
      calls += 1;
      const image = messages[0].content.find((item) => item.type === 'image_url');
      modelScreenshots.push(image.image_url.url);
      if (calls === 1) {
        const error = new Error('model response contained malformed JSON');
        error.code = 'RUNWAVE_MODEL_JSON_PARSE';
        error.responseText = '{"summary": "bad" trailing';
        throw error;
      }
      return {
        model: 'fake-model',
        usage: { total_tokens: 1 },
        json: {
          summary: 'fresh screen is visible',
          actions: [{ type: 'key', start: 0, end: 500, key: 'Enter' }],
          should_stop: true,
        },
      };
    },
    runAction: async (action) => {
      controllerActions.push(action);
      return { ok: true, action: 'step', captures: [{ path: afterScreenshot }], endState: { screen: 'after' } };
    },
  });

  assert.equal(result.steps, 2);
  assert.equal(result.modelErrorCount, 1);
  assert.equal(controllerActions.length, 1);
  assert.equal(controllerActions[0].action, 'step');
  assert.equal(result.history[0].failedAction, true);
  assert.equal(result.history[0].actions[0].type, 'failed_action');
  assert.equal(result.history[0].result.ok, false);
  assert.match(result.history[0].result.error, /malformed JSON/);
  assert.equal(result.history[0].result.screenshot, screenshot);
  assert.equal(result.history[0].result.screenshotChanged, false);
  assert.deepEqual(result.history[0].result.state, { url: 'http://example.test', screen: 'initial' });
  assert.equal(result.history[1].actions[0].key, 'Enter');
  assert.equal(modelScreenshots[0], modelScreenshots[1]);
});

test('agent loop records schema-invalid model sequences as failed actions without changing browser state', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-agent-schema-failed-action-test-'));
  const screenshot = path.join(dir, 'screen.png');
  const afterScreenshot = path.join(dir, 'after-screen.png');
  fs.writeFileSync(screenshot, 'initial screenshot bytes');
  fs.writeFileSync(afterScreenshot, 'after screenshot bytes');

  const modelScreenshots = [];
  let calls = 0;
  const controllerActions = [];
  const result = await runAgenticPlaytest({
    job: {
      playtestDurationMs: 9000,
      agentMinPlaytestMs: 0,
      viewport: { width: 640, height: 360 },
      agentMaxModelFallbacks: 2,
    },
    initialResponse: {
      screenshot,
      state: { screen: 'unchanged' },
    },
    outputDir: path.join(dir, 'agent'),
    modelClient: async ({ messages }) => {
      calls += 1;
      const image = messages[0].content.find((item) => item.type === 'image_url');
      modelScreenshots.push(image.image_url.url);
      if (calls === 1) {
        return {
          model: 'fake-model',
          text: '{"summary":"bad","actions":[],"the board.":"stray prose"}',
          usage: { total_tokens: 1 },
          json: {
            summary: 'bad',
            actions: [],
            'the board.': 'stray prose',
          },
        };
      }
      return {
        model: 'fake-model',
        text: '{"summary":"valid","actions":[{"type":"key","start":0,"end":500,"key":"Enter"}],"should_stop":true}',
        usage: { total_tokens: 1 },
        json: {
          summary: 'valid',
          actions: [{ type: 'key', start: 0, end: 500, key: 'Enter' }],
          should_stop: true,
        },
      };
    },
    runAction: async (action) => {
      controllerActions.push(action);
      return { ok: true, action: 'step', captures: [{ path: afterScreenshot }], endState: { screen: 'after' } };
    },
  });

  assert.equal(result.steps, 2);
  assert.equal(result.modelErrorCount, 1);
  assert.equal(controllerActions.length, 1);
  assert.equal(controllerActions[0].action, 'step');
  assert.equal(result.history[0].failedAction, true);
  assert.equal(result.history[0].result.ok, false);
  assert.match(result.history[0].result.error, /unknown field "the board\."/);
  assert.equal(result.history[0].result.screenshot, screenshot);
  assert.equal(result.history[0].result.screenshotChanged, false);
  assert.deepEqual(result.history[0].result.state, { screen: 'unchanged' });
  assert.equal(result.history[1].actions[0].key, 'Enter');
  assert.equal(modelScreenshots[0], modelScreenshots[1]);
});

test('agent loop retries when model actions all normalize away', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-agent-empty-normalized-action-test-'));
  const screenshot = path.join(dir, 'screen.png');
  const afterScreenshot = path.join(dir, 'after-screen.png');
  fs.writeFileSync(screenshot, 'initial screenshot bytes');
  fs.writeFileSync(afterScreenshot, 'after screenshot bytes');

  const modelScreenshots = [];
  let calls = 0;
  const controllerActions = [];
  const result = await runAgenticPlaytest({
    job: {
      playtestDurationMs: 9000,
      agentMinPlaytestMs: 0,
      viewport: { width: 640, height: 360 },
      agentMaxModelFallbacks: 2,
    },
    initialResponse: {
      screenshot,
      state: { screen: 'unchanged' },
    },
    outputDir: path.join(dir, 'agent'),
    modelClient: async ({ messages }) => {
      calls += 1;
      const image = messages[0].content.find((item) => item.type === 'image_url');
      modelScreenshots.push(image.image_url.url);
      if (calls === 1) {
        return {
          model: 'fake-model',
          text: '{"summary":"bad timing","actions":[{"type":"click","start":0,"end":500,"x":10,"y":10}]}',
          usage: { total_tokens: 1 },
          json: {
            summary: 'bad timing',
            actions: [{ type: 'click', start: 0, end: 500, x: 10, y: 10 }],
          },
        };
      }
      return {
        model: 'fake-model',
        text: '{"summary":"valid","actions":[{"type":"key","start":0,"end":500,"key":"Enter"}],"should_stop":true}',
        usage: { total_tokens: 1 },
        json: {
          summary: 'valid',
          actions: [{ type: 'key', start: 0, end: 500, key: 'Enter' }],
          should_stop: true,
        },
      };
    },
    runAction: async (action) => {
      controllerActions.push(action);
      return { ok: true, action: 'step', captures: [{ path: afterScreenshot }], endState: { screen: 'after' } };
    },
  });

  assert.equal(result.steps, 2);
  assert.equal(result.modelErrorCount, 1);
  assert.equal(controllerActions.length, 1);
  assert.equal(controllerActions[0].action, 'step');
  assert.equal(result.history[0].failedAction, true);
  assert.equal(result.history[0].actions[0].type, 'failed_action');
  assert.match(result.history[0].result.error, /all model actions were invalid after normalization/);
  assert.equal(result.history[0].result.screenshot, screenshot);
  assert.equal(result.history[0].result.screenshotChanged, false);
  assert.deepEqual(result.history[0].result.state, { screen: 'unchanged' });
  assert.equal(result.history[1].actions[0].key, 'Enter');
  assert.equal(modelScreenshots[0], modelScreenshots[1]);
});

test('agent loop retries when clipped timing removes every action', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-agent-empty-clipped-action-test-'));
  const screenshot = path.join(dir, 'screen.png');
  const afterScreenshot = path.join(dir, 'after-screen.png');
  fs.writeFileSync(screenshot, 'initial screenshot bytes');
  fs.writeFileSync(afterScreenshot, 'after screenshot bytes');

  const modelScreenshots = [];
  let calls = 0;
  const controllerActions = [];
  const result = await runAgenticPlaytest({
    job: {
      playtestDurationMs: 6000,
      agentMinPlaytestMs: 0,
      viewport: { width: 640, height: 360 },
      agentMaxModelFallbacks: 2,
    },
    initialResponse: {
      screenshot,
      state: { screen: 'unchanged' },
    },
    outputDir: path.join(dir, 'agent'),
    modelClient: async ({ messages }) => {
      calls += 1;
      const image = messages[0].content.find((item) => item.type === 'image_url');
      modelScreenshots.push(image.image_url.url);
      if (calls === 1) {
        return {
          model: 'fake-model',
          text: '{"summary":"starts too late","actions":[{"type":"key","start":2000,"end":2500,"key":"Enter"}]}',
          usage: { total_tokens: 1 },
          json: {
            summary: 'starts too late',
            actions: [{ type: 'key', start: 2000, end: 2500, key: 'Enter' }],
          },
        };
      }
      return {
        model: 'fake-model',
        text: '{"summary":"valid","actions":[{"type":"key","start":0,"end":100,"key":"Enter"}],"should_stop":true}',
        usage: { total_tokens: 1 },
        json: {
          summary: 'valid',
          actions: [{ type: 'key', start: 0, end: 100, key: 'Enter' }],
          should_stop: true,
        },
      };
    },
    runAction: async (action) => {
      controllerActions.push(action);
      return { ok: true, action: 'step', captures: [{ path: afterScreenshot }], endState: { screen: 'after' } };
    },
  });

  assert.equal(result.steps, 2);
  assert.equal(result.modelErrorCount, 1);
  assert.equal(controllerActions.length, 1);
  assert.equal(controllerActions[0].actions[0].key, 'Enter');
  assert.equal(result.history[0].failedAction, true);
  assert.match(result.history[0].result.error, /outside the remaining sequence duration/);
  assert.equal(result.history[0].result.screenshotChanged, false);
  assert.equal(result.history[1].actions[0].key, 'Enter');
  assert.equal(modelScreenshots[0], modelScreenshots[1]);
});

test('agent loop records action execution failures and retries with the same screenshot', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-agent-execution-failed-action-test-'));
  const screenshot = path.join(dir, 'screen.png');
  const afterScreenshot = path.join(dir, 'after-screen.png');
  fs.writeFileSync(screenshot, 'initial screenshot bytes');
  fs.writeFileSync(afterScreenshot, 'after screenshot bytes');

  const modelScreenshots = [];
  let calls = 0;
  const controllerActions = [];
  const logEvents = [];
  const result = await runAgenticPlaytest({
    job: {
      playtestDurationMs: 9000,
      agentMinPlaytestMs: 0,
      viewport: { width: 640, height: 360 },
    },
    initialResponse: {
      screenshot,
      state: { screen: 'unchanged' },
    },
    outputDir: path.join(dir, 'agent'),
    log: (event, payload) => logEvents.push({ event, payload }),
    modelClient: async ({ messages }) => {
      calls += 1;
      const image = messages[0].content.find((item) => item.type === 'image_url');
      modelScreenshots.push(image.image_url.url);
      return {
        model: 'fake-model',
        text: '{"summary":"try key","actions":[{"type":"key","start":0,"end":200,"key":"KeyF5"}],"should_stop":false}',
        usage: { total_tokens: 1 },
        json: calls === 1
          ? {
              summary: 'try key',
              actions: [{ type: 'key', start: 0, end: 200, key: 'KeyF5' }],
              should_stop: false,
            }
          : {
              summary: 'valid retry',
              actions: [{ type: 'key', start: 0, end: 500, key: 'Enter' }],
              should_stop: true,
            },
      };
    },
    runAction: async (action) => {
      controllerActions.push(action);
      if (controllerActions.length === 1) {
        throw new Error('runwave action failed for agent-step-001: keyboard.down: Unknown key: "KeyF5"');
      }
      return { ok: true, action: 'step', captures: [{ path: afterScreenshot }], endState: { screen: 'after' } };
    },
  });

  assert.equal(result.steps, 2);
  assert.equal(controllerActions.length, 2);
  assert.equal(controllerActions[0].actions[0].key, 'KeyF5');
  assert.equal(result.history[0].failedAction, true);
  assert.equal(result.history[0].actions[0].key, 'KeyF5');
  assert.equal(result.history[0].result.ok, false);
  assert.match(result.history[0].result.error, /Unknown key/);
  assert.equal(result.history[0].result.screenshot, screenshot);
  assert.equal(result.history[0].result.screenshotChanged, false);
  assert.equal(result.history[0].result.captureCount, 0);
  assert.deepEqual(result.history[0].result.state, { screen: 'unchanged' });
  assert.equal(result.history[1].actions[0].key, 'Enter');
  assert.equal(modelScreenshots[0], modelScreenshots[1]);
  assert.equal(logEvents.some((entry) => entry.event === 'agent.sequence_execution_error'), true);
  assert.equal(logEvents.some((entry) => entry.event === 'agent.failed_action'), true);
});

test('parses fenced nested JSON responses from vision models', () => {
  const parsed = parseJsonResponse(`
Here is the next sequence:
\`\`\`json
{
  "summary": "title screen is visible",
  "actions": [
    {"type": "key", "start": 0, "end": 500, "key": "Enter"},
    {"type": "click", "start": 100, "x": 320, "y": 180}
  ],
  "should_stop": false
}
\`\`\`
`);

  assert.equal(parsed.summary, 'title screen is visible');
  assert.equal(parsed.actions[0].key, 'Enter');
  assert.equal(parsed.actions[1].x, 320);
});

test('recovers JSON with bare repeated string fragments before the closing brace', () => {
  const parsed = parseJsonResponse(`{
  "summary": "The board changed.",
  "previous_sequence_outcome": "The previous ArrowRight move shifted the board.",
  "actions": [
    {"type": "key", "start": 0, "end": 300, "key": "ArrowRight"},
    {"type": "key", "start": 400, "end": 700, "key": "ArrowDown"}
  ],
  "should_stop": false,
  "rationale": "Sliding right and down will merge tiles in the bottom-right corner."
."
right corner."
 corner."
}`);

  assert.equal(parsed.summary, 'The board changed.');
  assert.equal(parsed.previous_sequence_outcome, 'The previous ArrowRight move shifted the board.');
  assert.equal(parsed.actions[0].key, 'ArrowRight');
  assert.equal(parsed.rationale, 'Sliding right and down will merge tiles in the bottom-right corner.');
});

test('recovers JSON with repeated bare rationale continuation lines', () => {
  const parsed = parseJsonResponse(`{
  "summary": "The menu is still visible.",
  "actions": [
    {
      "type": "click",
      "start": 100,
      "x": 500,
      "y": 660
    }
  ],
  "should_stop": false,
  "rationale": "I will try clicking it again."
clicking it again."
to see if it registers."
}`);

  assert.equal(parsed.summary, 'The menu is still visible.');
  assert.equal(parsed.actions[0].x, 500);
  assert.equal(parsed.rationale, 'I will try clicking it again.');
});

test('recovers JSON by dropping duplicated content after a bare fragment tail', () => {
  const parsed = parseJsonResponse(`{
  "summary": "The player ship is on the bottom right.",
  "actions": [
    {"type": "key", "start": 0, "end": 500, "key": "ArrowLeft"},
    {"type": "key", "start": 600, "end": 800, "key": "Space"}
  ],
  "should_stop": false,
  "rationale": "Moving left while shooting to clear them."
clear them."
[
  {
    "summary": "duplicate response",
    "actions": []
  }
]`);

  assert.equal(parsed.summary, 'The player ship is on the bottom right.');
  assert.equal(parsed.actions.length, 2);
  assert.equal(parsed.rationale, 'Moving left while shooting to clear them.');
});

test('tags malformed model JSON parse errors', () => {
  assert.throws(
    () => parseJsonResponse('{"summary": "almost valid", "actions": [] trailing'),
    (error) => {
      assert.equal(error.code, 'RUNWAVE_MODEL_JSON_PARSE');
      assert.match(error.message, /JSON/);
      return true;
    }
  );
});

test('builds a failed action after invalid model JSON', () => {
  const sequence = failedActionAfterInvalidJson({
    error: Object.assign(new Error('bad JSON'), { code: 'RUNWAVE_MODEL_JSON_PARSE' }),
  });

  assert.equal(sequence.durationMs, 0);
  assert.deepEqual(sequence.actions, []);
  assert.equal(sequence.failedAction, true);
  assert.match(sequence.error, /bad JSON/);
});

test('playtester prompt warns when recent sequences repeat', () => {
  const prompt = buildPlaytesterPrompt({
    job: {},
    elapsedMs: 10000,
    maxMs: 120000,
    viewport: { width: 1280, height: 720 },
    state: {},
    history: [
      { step: 1, summary: 'game is paused', actions: [{ type: 'click', x: 960, y: 719 }] },
      { step: 2, summary: 'game is paused', actions: [{ type: 'click', x: 963, y: 719 }] },
      { step: 3, summary: 'game is paused', actions: [{ type: 'click', x: 963, y: 719 }] },
    ],
  });

  assert.match(prompt, /Warning:/);
  assert.match(prompt, /Space, Enter, Escape, and P/);
  assert.match(prompt, /"type":"drag"/);
  assert.match(prompt, /Single Player/);
  assert.match(prompt, /Do not spend turns only describing or waiting on a menu/);
  assert.match(prompt, /light 16x16 red mark grid/);
  assert.match(prompt, /Overlay column labels are in the top\/bottom margins/);
  assert.match(prompt, /"type":"multi_click"/);
  assert.match(prompt, /JSON output contract/);
  assert.match(prompt, /Top-level keys must be exactly/);
  assert.match(prompt, /"actions"/);
  assert.match(prompt, /"start":0/);
  assert.match(prompt, /"end":500/);
  assert.match(prompt, /RunWave adds a short default/);
  assert.match(prompt, /"cell":\{"overlay_row":8,"overlay_col":8\}/);
  assert.match(prompt, /overlay grid targeting/);
  assert.match(prompt, /reason about that game structure separately/);
  assert.match(prompt, /failed about 3-5 times/);
  assert.match(prompt, /learn the pattern/);
  assert.match(prompt, /Every non-stop response must include at least one action/);
  assert.doesNotMatch(prompt, /"commands":/);
  assert.doesNotMatch(prompt, /duration_ms/);
  assert.doesNotMatch(prompt, /"clicks":/);
  assert.doesNotMatch(prompt, /"multi_clicks":/);
});

test('sequence schema guide uses configured overlay row and column grid examples', () => {
  const guide = sequenceSchemaGuide({ rows: 16, cols: 16 });

  assert.match(guide, /overlay_row\/overlay_col targets from the 16x16 overlay/);
  assert.match(guide, /"cell":\{"overlay_row":8,"overlay_col":8\}/);
  assert.match(guide, /"from":\{"overlay_row":8,"overlay_col":8\}/);
  assert.match(guide, /"to":\{"overlay_row":8,"overlay_col":9\}/);
});

test('playtester prompt stays platform-neutral even if the job has a target kind', () => {
  const prompt = buildPlaytesterPrompt({
    job: { targetKind: 'linux', promptMode: 'webapp' },
    elapsedMs: 10000,
    maxMs: 120000,
    viewport: { width: 1280, height: 720 },
    state: { targetKind: 'linux', window: { width: 1280, height: 720 } },
    history: [],
  });

  assert.match(prompt, /agentic interactive web-application tester/);
  assert.match(prompt, /Application state JSON/);
  assert.match(prompt, /active view/);
  assert.match(prompt, /normal RunWave key names/);
  assert.doesNotMatch(prompt, /native Linux/);
  assert.doesNotMatch(prompt, /browser-game/);
  assert.doesNotMatch(prompt, /Playwright/);
  assert.doesNotMatch(prompt, /xdotool/);
  assert.doesNotMatch(prompt, /\bgame\b/i);
});

test('prompt mode defaults to legacy web-game guidance and can be selected from the environment', () => {
  const originalMode = process.env.RUNWAVE_PROMPT_MODE;
  try {
    delete process.env.RUNWAVE_PROMPT_MODE;
    assert.equal(normalizePromptMode(), 'webgame');
    process.env.RUNWAVE_PROMPT_MODE = 'webapp';
    assert.equal(normalizePromptMode(), 'webapp');
    assert.equal(normalizePromptMode('website'), 'webapp');
    assert.equal(normalizePromptMode('unknown-mode'), 'webgame');
  } finally {
    if (originalMode === undefined) delete process.env.RUNWAVE_PROMPT_MODE;
    else process.env.RUNWAVE_PROMPT_MODE = originalMode;
  }
});

test('default prompt preserves the legacy web-game profile', () => {
  const prompt = buildPlaytesterPrompt({
    job: { playtestInstructions: '- Start: Enter.' },
    elapsedMs: 10000,
    maxMs: 120000,
    viewport: { width: 1280, height: 720 },
    state: {},
    history: [],
  });

  assert.match(prompt, /You are an agentic game playtester/);
  assert.match(prompt, /create a useful gameplay video/);
  assert.match(prompt, /Game-specific playtest\.md:/);
  assert.doesNotMatch(prompt, /interactive web-application tester/);
});

test('playtester prompt includes application-specific playtest instructions', () => {
  const prompt = buildPlaytesterPrompt({
    job: {
      promptMode: 'webapp',
      playtestInstructions: [
        '# Playtest Controls',
        '',
        '- Start: Enter.',
        '- Move: WASD.',
      ].join('\n'),
    },
    elapsedMs: 10000,
    maxMs: 120000,
    viewport: { width: 1280, height: 720 },
    state: {},
    history: [],
  });

  assert.match(prompt, /Application-specific playtest\.md:/);
  assert.match(prompt, /- Start: Enter\./);
  assert.match(prompt, /- Move: WASD\./);
  assert.match(prompt, /Use these application-specific controls/);
});

test('playtester prompt warns when recent sequences repeat a control cycle up to 5 steps', () => {
  const cycle = ['ArrowRight', 'ArrowUp', 'ArrowLeft', 'ArrowDown'];
  const prompt = buildPlaytesterPrompt({
    job: {},
    elapsedMs: 10000,
    maxMs: 120000,
    viewport: { width: 1280, height: 720 },
    state: {},
    history: cycle.concat(cycle).map((key, index) => ({
      step: index + 1,
      summary: 'ball is still in the central maze area',
      actions: [{ type: 'key', key }],
    })),
  });

  assert.match(prompt, /Warning: the recent sequences repeated a 4-step control cycle/);
  assert.match(prompt, /ArrowRight -> ArrowUp -> ArrowLeft -> ArrowDown/);
  assert.match(prompt, /Break the loop now/);
});

test('compact history includes post-sequence result signals', () => {
  const text = compactHistory([
    {
      step: 1,
      summary: 'ball moved through a corridor',
      actions: [{ type: 'key', key: 'ArrowRight' }],
      result: { ok: true, screenshotChanged: true, captureCount: 1 },
      outcomeSummary: 'The ball rolled right and the camera followed into a new corridor.',
    },
  ]);

  assert.match(text, /controls=ArrowRight/);
  assert.match(text, /post_sequence=ok=true,screenshot_changed=true,captures=1/);
  assert.match(text, /outcome="The ball rolled right and the camera followed into a new corridor\."/);
});

test('agent loop attaches previous sequence outcome to the prior history step', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'runwave-agent-outcome-test-'));
  const screenshot = path.join(dir, 'screen.png');
  fs.writeFileSync(
    screenshot,
    Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=', 'base64')
  );

  let calls = 0;
  const result = await runAgenticPlaytest({
    job: {
      playtestDurationMs: 9000,
      agentMinPlaytestMs: 0,
      viewport: { width: 640, height: 360 },
    },
    initialResponse: {
      screenshot,
      state: { url: 'http://example.test' },
    },
    outputDir: path.join(dir, 'agent'),
    modelClient: async () => {
      calls += 1;
      return {
        model: 'fake-model',
        usage: { total_tokens: 1 },
        json: calls === 1
          ? {
              summary: 'ball is in a corridor',
              previous_sequence_outcome: '',
              actions: [{ type: 'key', start: 0, end: 500, key: 'ArrowRight' }],
              should_stop: false,
            }
          : {
              summary: 'ball has moved into the next corridor',
              previous_sequence_outcome: 'ArrowRight rolled the ball into a new visible corridor.',
              actions: [{ type: 'key', start: 0, end: 500, key: 'ArrowRight' }],
              should_stop: true,
            },
      };
    },
    runAction: async () => ({ ok: true, captures: [{ path: screenshot }], endState: { ok: true } }),
  });

  assert.equal(result.steps, 2);
  assert.equal(result.history[0].outcomeSummary, 'ArrowRight rolled the ball into a new visible corridor.');
  assert.equal(result.history[1].outcomeSummary, undefined);
});

test('chat completion honors explicit retry attempts for malformed JSON', async () => {
  const originalFetch = global.fetch;
  const originalKey = process.env.OPENROUTER_API_KEY;
  let calls = 0;
  const requestBodies = [];
  process.env.OPENROUTER_API_KEY = 'test-key';
  global.fetch = async (_url, options) => {
    calls += 1;
    requestBodies.push(JSON.parse(options.body));
    return {
      ok: true,
      text: async () =>
        JSON.stringify({
          choices: [
            {
              message: {
                content: calls === 1 ? '{"summary": "bad" trailing' : '{"summary": "ok", "actions": []}',
              },
            },
          ],
          usage: { total_tokens: 1 },
        }),
    };
  };

  try {
    const result = await chatCompletion({
      messages: [{ role: 'user', content: 'return JSON' }],
      attempts: 2,
      timeoutMs: 1000,
    });

    assert.equal(calls, 2);
    assert.equal(result.json.summary, 'ok');
    assert.equal(requestBodies[1].messages.length, 2);
  } finally {
    global.fetch = originalFetch;
    if (originalKey === undefined) delete process.env.OPENROUTER_API_KEY;
    else process.env.OPENROUTER_API_KEY = originalKey;
  }
});

test('model client keeps OpenRouter as the default provider', () => {
  assert.equal(resolveBaseUrl({}), 'https://openrouter.ai/api/v1');
  assert.equal(resolveApiKey({ OPENROUTER_API_KEY: 'router-key' }), 'router-key');
  assert.equal(
    resolveApiKey({
      RUNWAVE_BASE_URL: 'https://example.test/v1',
      OPENROUTER_API_KEY: 'router-key',
    }),
    null
  );
  assert.equal(resolveModel({}), 'google/gemini-3.8-flash');
  assert.equal(
    resolveModel({ RUNWAVE_BASE_URL: 'https://generativelanguage.googleapis.com/v1beta/openai' }),
    'gemini-3.8-flash'
  );
  assert.equal(
    resolveModel({
      RUNWAVE_BASE_URL: 'https://generativelanguage.googleapis.com/v1beta/openai',
      OPENROUTER_MODEL: 'wrong-provider-model',
      RUNWAVE_AGENT_MODEL: 'generic-custom-model',
    }),
    'generic-custom-model'
  );
});

test('model client sends provider-neutral requests to an OpenAI-compatible endpoint', async () => {
  const originalFetch = global.fetch;
  const requests = [];
  const env = {
    RUNWAVE_BASE_URL: 'https://generativelanguage.googleapis.com/v1beta/openai',
    RUNWAVE_API_KEY: 'runwave-key',
    OPENROUTER_API_KEY: 'router-key-that-must-not-be-used',
    RUNWAVE_AGENT_MODEL: 'explicit-custom-model',
    RUNWAVE_AGENT_MODEL_ATTEMPTS: '1',
  };
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    return {
      ok: true,
      text: async () => JSON.stringify({
        choices: [{ message: { content: '{"summary":"ok","actions":[]}' } }],
      }),
    };
  };

  try {
    const result = await chatCompletion({
      messages: [{ role: 'user', content: 'return JSON' }],
      env,
      timeoutMs: 1000,
    });

    assert.equal(result.model, 'explicit-custom-model');
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions');
    assert.equal(requests[0].options.headers.Authorization, 'Bearer runwave-key');
    assert.equal(requests[0].options.headers['HTTP-Referer'], undefined);
    assert.equal(requests[0].options.headers['X-Title'], undefined);
    assert.equal(JSON.parse(requests[0].options.body).model, 'explicit-custom-model');
  } finally {
    global.fetch = originalFetch;
  }
});

test('model client accepts a full Gemini chat-completions URL and GEMINI_API_KEY', async () => {
  const originalFetch = global.fetch;
  const requests = [];
  const env = {
    RUNWAVE_BASE_URL: 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
    GEMINI_API_KEY: 'gemini-key',
    RUNWAVE_AGENT_MODEL: 'gemini-3.8-flash',
    RUNWAVE_AGENT_MODEL_ATTEMPTS: '1',
  };
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    return {
      ok: true,
      text: async () => JSON.stringify({
        choices: [{ message: { content: '{"summary":"ok","actions":[]}' } }],
      }),
    };
  };

  try {
    assert.equal(
      chatCompletionsUrl('https://generativelanguage.googleapis.com/v1beta/openai/'),
      'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    );
    assert.equal(
      chatCompletionsUrl('https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
      'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    );
    assert.equal(resolveApiKey(env), 'gemini-key');
    const result = await chatCompletion({
      messages: [{ role: 'user', content: 'return JSON' }],
      env,
      timeoutMs: 1000,
    });

    assert.equal(result.model, 'gemini-3.8-flash');
    assert.equal(requests.length, 1);
    assert.equal(
      requests[0].url,
      'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    );
    assert.equal(requests[0].options.headers.Authorization, 'Bearer gemini-key');
    assert.equal(JSON.parse(requests[0].options.body).model, 'gemini-3.8-flash');
  } finally {
    global.fetch = originalFetch;
  }
});
