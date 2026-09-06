'use strict';

const { markGridFromConfig } = require('../../protocol/src/mark-grid');

function gridExampleCells(grid) {
  const row = Math.min(grid.rows - 1, Math.max(0, Math.floor(grid.rows / 2)));
  const col = Math.min(grid.cols - 1, Math.max(0, Math.floor(grid.cols / 2)));
  const center = { overlay_row: row, overlay_col: col };
  const right = { overlay_row: row, overlay_col: Math.min(grid.cols - 1, col + 1) };
  const above = { overlay_row: Math.max(0, row - 1), overlay_col: col };
  return { center, right, above };
}

function cellJson(cell) {
  return `{"overlay_row":${cell.overlay_row},"overlay_col":${cell.overlay_col}}`;
}

function sequenceSchemaGuide(grid = markGridFromConfig({})) {
  const examples = gridExampleCells(grid);
  const rows = [
    'JSON output contract:',
    'Return exactly one JSON object. No markdown, no prose outside JSON.',
    'Top-level keys must be exactly: "summary", "previous_sequence_outcome", "actions", "should_stop", "rationale".',
    'Actions must match one of these shapes; do not add extra fields:',
    '{"type":"key","start":0,"end":500,"key":"ArrowRight"}',
    `{"type":"click","start":100,"cell":${cellJson(examples.center)}}`,
    `{"type":"multi_click","start":100,"cells":[${cellJson(examples.center)},${cellJson(examples.right)}],"count":8}`,
    `{"type":"drag","start":100,"from":${cellJson(examples.center)},"to":${cellJson(examples.right)},"mode":"mouse","steps":12}`,
    `{"type":"cursor_move","start":100,"cell":${cellJson(examples.above)},"steps":8}`,
    '{"type":"view_move","start":0,"end":800,"dx":120,"dy":0,"steps":8}',
    'Timing rules: use milliseconds; click <=100ms if end is provided; drag/cursor_move <=2000ms if end is provided; key/view_move may be longer; whole sequence must stay under 8000ms.',
    `Click, multi_click, drag, and cursor_move may omit end; RunWave adds a short default. Prefer overlay_row/overlay_col targets from the ${grid.rows}x${grid.cols} overlay over raw x/y.`,
  ];
  return rows.join('\n');
}

function compactPostSequenceResult(result) {
  if (!result || typeof result !== 'object') return '';
  const parts = [];
  if (typeof result.ok === 'boolean') parts.push(`ok=${result.ok}`);
  if (typeof result.screenshotChanged === 'boolean') parts.push(`screenshot_changed=${result.screenshotChanged}`);
  if (typeof result.captureCount === 'number') parts.push(`captures=${result.captureCount}`);
  if (result.error) parts.push(`error=${String(result.error).slice(0, 120)}`);
  return parts.length ? `; post_sequence=${parts.join(',')}` : '';
}

function compactOutcomeSummary(outcomeSummary) {
  const text = String(outcomeSummary || '').trim();
  return text ? `; outcome="${text.slice(0, 220)}"` : '';
}

function compactHistory(history, limit = 8) {
  return history
    .slice(-limit)
    .map((item) => {
      const controls = [
        ...sequenceActions(item).map(actionLabelPart),
      ];
      return `step ${item.step}: ${item.summary || item.rationale || 'no summary'}; controls=${controls.join(',') || 'none'}${compactPostSequenceResult(item.result)}${compactOutcomeSummary(item.outcomeSummary)}`;
    })
    .join('\n');
}

function roundedPoint(value, quantum = 20) {
  return Math.round(Number(value || 0) / quantum) * quantum;
}

function sequenceActions(item) {
  return Array.isArray(item.actions) ? item.actions : [];
}

function actionLabelPart(action) {
  if (action.type === 'key') return action.key;
  if (action.type === 'click') return `click(${action.x},${action.y})`;
  if (action.type === 'drag') return `drag(${action.from.x},${action.from.y}->${action.to.x},${action.to.y},${action.mode})`;
  if (action.type === 'cursor_move') return `cursor(${action.to.x},${action.to.y})`;
  if (action.type === 'view_move') return `view(${action.dx},${action.dy})`;
  if (action.type === 'failed_action') return `failed_action(${String(action.error || '').slice(0, 80)})`;
  return action.type || 'unknown';
}

function actionControls(item) {
  const actions = sequenceActions(item);
  return {
    keys: actions.filter((action) => action.type === 'key').map((action) => action.key).filter(Boolean),
    clicks: actions.filter((action) => action.type === 'click').map((click) => `${roundedPoint(click.x)},${roundedPoint(click.y)}`),
    drags: actions
      .filter((action) => action.type === 'drag')
      .map((drag) => `${roundedPoint(drag.from.x)},${roundedPoint(drag.from.y)}->${roundedPoint(drag.to.x)},${roundedPoint(drag.to.y)}`),
    cursorMoves: actions
      .filter((action) => action.type === 'cursor_move')
      .map((move) => `${roundedPoint(move.to.x)},${roundedPoint(move.to.y)}`),
    viewMoves: actions.filter((action) => action.type === 'view_move').map((move) => `${roundedPoint(move.dx)},${roundedPoint(move.dy)}`),
  };
}

function actionSignature(item) {
  const controls = actionControls(item);
  if (!Object.values(controls).some((values) => values.length)) return '';
  return [
    controls.keys.join(','),
    controls.clicks.join(','),
    controls.drags.join(','),
    controls.cursorMoves.join(','),
    controls.viewMoves.join(','),
  ].join('|');
}

function actionLabel(item) {
  const controls = actionControls(item);
  const labels = [
    ...controls.keys,
    ...controls.clicks.map((click) => `click(${click})`),
    ...controls.drags.map((drag) => `drag(${drag})`),
    ...controls.cursorMoves.map((move) => `cursor(${move})`),
    ...controls.viewMoves.map((move) => `view(${move})`),
  ];
  return labels.join('+') || 'no controls';
}

function repeatedControlCycle(history, maxPeriod = 5) {
  const signatures = history.map(actionSignature);
  const max = Math.min(maxPeriod, Math.floor(signatures.length / 2));
  for (let period = 1; period <= max; period += 1) {
    const repeatCount = period === 1 ? 3 : 2;
    const required = period * repeatCount;
    if (signatures.length < required) continue;

    const start = signatures.length - required;
    const cycle = signatures.slice(signatures.length - period);
    if (!cycle.some(Boolean)) continue;

    let matched = true;
    for (let offset = 0; offset < required; offset += 1) {
      if (signatures[start + offset] !== cycle[offset % period]) {
        matched = false;
        break;
      }
    }
    if (matched) {
      return {
        period,
        labels: history.slice(history.length - period).map(actionLabel),
      };
    }
  }
  return null;
}

const DEFAULT_PROMPT_MODE = 'webgame';

function normalizePromptMode(value) {
  const raw = String(
    value
    ?? process.env.RUNWAVE_PROMPT_MODE
    ?? process.env.RUNWAVE_PROMPT_PROFILE
    ?? ''
  ).trim().toLowerCase();
  if (['webapp', 'web-app', 'website', 'web', 'application', 'generic', 'interactive', 'interactive-web'].includes(raw)) {
    return 'webapp';
  }
  return DEFAULT_PROMPT_MODE;
}

function promptProfile(mode) {
  if (mode === 'webapp') {
    return {
      pausedWarning: 'Warning: the application still appears paused. Try a different visible resume/control input instead of repeating the same click.',
      entryWarning: 'Warning: the recent sequences did not clearly leave the landing/menu flow. Use the visible instruction or a different common confirm key.',
      instructionsTitle: 'Application-specific playtest.md:',
      controlsNote: 'Use these application-specific controls when they apply. If they conflict with visible instructions, follow the visible current screen.',
      dragGuidance: '- Use type "drag" for drag/swipe interactions with "from" and "to" overlay row/column objects. Use mode "mouse" for pointer/canvas-style dragging; use mode "html5" only when the visible application appears to use browser-native drag/drop elements.',
      viewportDescription: 'active view',
      intro: 'You are an agentic interactive web-application tester. Your job is to create a useful interaction video, not to judge the application.',
      goals: [
        '- If the application opens on a landing, welcome, setup, or menu screen, proceed to its main interactive content.',
        '- If movement or navigation is possible, explore as much of the application as possible.',
        '- If there is no movement, perform meaningful application interactions repeatedly.',
        '- Try to chain multiple sensible actions in one sequence rather than a single action each time.',
        '- Do not stop early unless the recording already shows enough meaningful interaction.',
      ],
      structure: '- If the application has its own board, tiles, cells, lanes, or grid, reason about that structure separately. Use the red overlay only to choose approximate mouse positions.',
      rawViewport: '- Only use raw x/y if the grid is not enough. Raw x/y coordinates are inside the inner active viewport only; ignore the label margins and do not use full-image pixel coordinates.',
      paused: '- If the application says paused, resume, continue, start, or shows tutorial controls, follow that visible instruction before doing anything else.',
      menu: '- On landing or menu screens, prefer options that expose the main interactive content: Play, Start, New, Continue, Resume, or a default view/workspace choice.',
      avoid: '- Prefer meaningful interactive content over static Settings, Credits, Help, or external-navigation screens unless the guide or visible UI identifies them as important features.',
      menuFallback: '- If one menu choice does not expose the main content, go back or try a different option on the next step. Do not spend turns only describing or waiting on a menu.',
      geometry: '- If the state JSON reports useful application geometry such as a canvas, treat that rectangle as the active area unless the screenshot clearly shows otherwise. Otherwise rely on the visible screenshot.',
      reset: '- If the application reloads, resets, or returns to a landing/menu screen, re-enter its main content and change strategy instead of repeating the same failed action.',
      repetition: '- If an action or strategy visibly worked, learn the pattern and try a similar follow-up, but do not blindly repeat the exact same input unless the application clearly needs repetition.',
      video: '- Avoid idle waiting. Each step should do something visible or useful for the interaction video.',
      outcome: '- In previous_sequence_outcome, summarize what visibly happened after the most recent prior sequence. If a prior control changed the view, object, board, metric, menu, or application state, say that clearly. On the first sequence, use an empty string.',
      stateLabel: 'Application state JSON:',
    };
  }

  // This is the original profile. It remains the default so existing
  // web-game tasks receive the same behavioral guidance as before.
  return {
    pausedWarning: 'Warning: the game still appears paused. Try a different visible resume/control input instead of repeating the same click.',
    entryWarning: 'Warning: the recent sequences did not clearly leave the menu/title flow. Use the visible start instruction or a different common confirm key.',
    instructionsTitle: 'Game-specific playtest.md:',
    controlsNote: 'Use these game-specific controls when they apply. If they conflict with visible in-game instructions, follow the visible current screen.',
    dragGuidance: '- Use type "drag" for drag/swipe games with "from" and "to" overlay row/column objects. Use mode "mouse" for pointer/canvas-style dragging; use mode "html5" only when the visible game appears to use browser-native drag/drop elements.',
    viewportDescription: 'playable view',
    intro: 'You are an agentic game playtester. Your job is to create a useful gameplay video, not to judge the game.',
    goals: [
      '- If the game is on a menu/title/start screen, get into real gameplay.',
      '- If movement is possible, always strive to explore as much as possible.',
      '- If there is no movement, perform meaningful game actions repeatedly.',
      '- Try to chain multiple sensible actions in one sequence rather than a single action each time.',
      '- Do not stop early unless the recording already shows enough real gameplay.',
    ],
    structure: '- If the game has its own board, tiles, cells, lanes, or grid, reason about that game structure separately. Use the red overlay only to choose approximate mouse positions.',
    rawViewport: '- Only use raw x/y if the grid is not enough. Raw x/y coordinates are inside the inner game viewport only; ignore the label margins and do not use full-image pixel coordinates.',
    paused: '- If the game says paused, resume, continue, start, or shows tutorial controls, follow that visible instruction before doing anything else.',
    menu: '- On menus, prefer options that clearly enter gameplay: Play, Start, New Game, Single Player, Campaign, Level 1, Continue, Resume, or a default character/level choice.',
    avoid: '- Avoid Options, Settings, Credits, Help, Leaderboard, and Multiplayer unless they are the only visible path into gameplay.',
    menuFallback: '- If one menu choice does not enter gameplay, go back or try a different start-like choice on the next step. Do not spend turns only describing or waiting on a menu.',
    geometry: '- If the state JSON reports useful game geometry such as a canvas, treat that rectangle as the active game area unless the screenshot clearly shows otherwise. Otherwise rely on the visible screenshot.',
    reset: '- If you die, reset, or return to a map/title screen, re-enter gameplay and change strategy instead of repeating the same failed action.',
    repetition: '- If an action or strategy visibly worked, learn the pattern and try a similar follow-up, but do not blindly repeat the exact same input unless the game clearly needs repetition.',
    video: '- Avoid idle waiting. Each step should do something visible or useful for the gameplay video.',
    outcome: '- In previous_sequence_outcome, summarize what visibly happened after the most recent prior sequence. If a prior control moved the player, camera, board, score, menu, or level state, say that clearly. On the first sequence, use an empty string.',
    stateLabel: 'Game state JSON:',
  };
}

function repeatedHistoryWarning(history, mode = normalizePromptMode()) {
  const profile = promptProfile(mode);
  const cycle = repeatedControlCycle(history, 5);
  if (cycle && cycle.period === 1) {
    return 'Warning: the recent sequences repeated the same controls. Switch strategy now instead of trying the same input again.';
  }
  if (cycle) {
    return `Warning: the recent sequences repeated a ${cycle.period}-step control cycle (${cycle.labels.join(' -> ')}). Break the loop now with a different route, a longer committed move, or a different strategy instead of continuing the cycle.`;
  }

  const recent = history.slice(-3);
  if (recent.length < 3) return '';
  const summaries = recent.map((item) => String(item.summary || '').toLowerCase());
  if (summaries.every((summary) => summary.includes('paused'))) {
    return profile.pausedWarning;
  }
  const entryTerms = mode === 'webapp'
    ? ['menu', 'title', 'start', 'landing', 'welcome', 'setup']
    : ['menu', 'title', 'start'];
  if (summaries.every((summary) => entryTerms.some((term) => summary.includes(term)))) {
    return profile.entryWarning;
  }
  return '';
}

function playtestInstructionsSection(job, mode = normalizePromptMode()) {
  const instructions = String(job.playtestInstructions || '').trim();
  if (!instructions) return [];
  const profile = promptProfile(mode);
  return [
    profile.instructionsTitle,
    instructions.slice(0, 8000),
    '',
    profile.controlsNote,
    '',
  ];
}

function buildPlaytesterPrompt({ job, elapsedMs, maxMs, viewport, state, history }) {
  const promptMode = normalizePromptMode(job && job.promptMode);
  const profile = promptProfile(promptMode);
  const secondsLeft = Math.max(0, Math.round((maxMs - elapsedMs) / 1000));
  const controls = job.agentControls || [
    'ArrowUp',
    'ArrowDown',
    'ArrowLeft',
    'ArrowRight',
    'Space',
    'Enter',
    'Escape',
    'KeyW',
    'KeyA',
    'KeyS',
    'KeyD',
  ];
  const warning = repeatedHistoryWarning(history, promptMode);
  const grid = markGridFromConfig(job || {});
  const viewportLine = `Viewport: ${viewport.width}x${viewport.height}. The screenshot has a light ${grid.rows}x${grid.cols} red mark grid over the ${profile.viewportDescription}. Overlay column labels are in the top/bottom margins and overlay row labels are in the left/right margins.`;

  return [
    profile.intro,
    '',
    'High-level goals:',
    ...profile.goals,
    '',
    sequenceSchemaGuide(grid),
    '',
    `Time remaining: ${secondsLeft}s.`,
    viewportLine,
    `Available common controls: ${controls.join(', ')}. You may use normal RunWave key names.`,
    '',
    ...playtestInstructionsSection(job, promptMode),
    'Sequence guidance:',
    profile.structure,
    '- Prefer overlay grid targeting over raw x/y coordinates. For a single pointer target use "cell": {"overlay_row": r, "overlay_col": c}; for up to 4 possible targets use "cells": [{"overlay_row": r, "overlay_col": c}].',
    profile.rawViewport,
    '- Put every input in the "actions" array. Each action must have a "type": "key", "click", "multi_click", "drag", "cursor_move", or "view_move".',
    '- Use "start" and "end" times in milliseconds. Instant actions such as click, multi_click, drag, and cursor_move only need "start".',
    '- The sequence duration is inferred from the latest action "end", or from "start" for instant actions. The runner will send that duration explicitly with the step request.',
    '- For pointer-only sequences, do not put the final pointer action exactly at the end of the sequence. Leave at least 100ms after the final click, drag, multi_click, or cursor_move by scheduling it before the latest action end/start.',
    '- Never use a pointer action start time beyond the sequence duration. Keep all click, multi_click, drag, and cursor_move start times strictly before the latest end/start in the sequence.',
    '- Use type "click" for a single click in the selected cell area. Use type "multi_click" when a target is imprecise or repeated clicking/tapping is useful; it sends quick clicks at random points inside the selected cells.',
    profile.dragGuidance,
    profile.paused,
    profile.menu,
    profile.avoid,
    profile.menuFallback,
    '- Common resume/confirm keys are Space, Enter, Escape, and P. Use them when the screen suggests a paused/menu state and clicks are not working.',
    profile.geometry,
    profile.reset,
    '- If the same thing has failed about 3-5 times with no visible progress, say that in previous_sequence_outcome and try a different target, route, control, or strategy.',
    profile.repetition,
    profile.video,
    profile.outcome,
    '',
    warning ? `${warning}\n` : '',
    'Recent history:',
    compactHistory(history) || 'none',
    '',
    profile.stateLabel,
    JSON.stringify(state || {}, null, 2).slice(0, 5000),
    '',
    'Return the next JSON sequence now. Every non-stop response must include at least one action. Use an empty actions array only when you are done and set "should_stop": true.',
  ].join('\n');
}

module.exports = {
  buildPlaytesterPrompt,
  compactHistory,
  normalizePromptMode,
  sequenceSchemaGuide,
};
