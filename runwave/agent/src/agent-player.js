'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { normalizeSequence } = require('./action-parser');
const { AgentRecorder } = require('./history');
const { chatCompletion, dataUrl, isModelJsonParseError } = require('./model-client');
const { buildPlaytesterPrompt } = require('./prompt');

const STOP_RESERVE_MS = 5000;
const MODEL_SEQUENCE_ERROR_CODE = 'RUNWAVE_MODEL_SEQUENCE_INVALID';

function responseBody(response) {
  if (!response || typeof response !== 'object') return {};
  return response.output && typeof response.output === 'object' ? response.output : response;
}

function latestScreenshot(response) {
  const body = responseBody(response);
  const captures = Array.isArray(body.captures) ? body.captures : [];
  const capture = captures.length ? captures[captures.length - 1] : null;
  return (capture && capture.path) || body.screenshot || null;
}

function responseState(response) {
  const body = responseBody(response);
  return body.endState || body.state || {};
}

function viewportFor(job) {
  return job.viewport || job.videoSize || { width: 1280, height: 720 };
}

function fileHash(file) {
  if (!file) return null;
  try {
    return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  } catch {
    return null;
  }
}

function screenshotChanged(beforeScreenshot, afterScreenshot) {
  if (!beforeScreenshot || !afterScreenshot) return null;
  const beforeHash = fileHash(beforeScreenshot);
  const afterHash = fileHash(afterScreenshot);
  if (!beforeHash || !afterHash) return null;
  return beforeHash !== afterHash;
}

function postSequenceResult(response, beforeScreenshot) {
  const body = responseBody(response);
  const captures = Array.isArray(body.captures) ? body.captures : [];
  const afterScreenshot = latestScreenshot(response);
  const result = {
    ok: typeof body.ok === 'boolean' ? body.ok : null,
    screenshot: afterScreenshot,
    screenshotChanged: screenshotChanged(beforeScreenshot, afterScreenshot),
    captureCount: captures.length,
    state: responseState(response),
  };
  if (body.error) result.error = String(body.error).slice(0, 500);
  return result;
}

async function decideNextSequence({ job, screenshot, state, history, elapsedMs, maxMs, modelClient, promptStep = null, onPrompt = null }) {
  if (!screenshot) throw new Error('agent cannot decide without a screenshot');
  const viewport = viewportFor(job);
  const prompt = buildPlaytesterPrompt({ job, elapsedMs, maxMs, viewport, state, history });
  if (typeof onPrompt === 'function') {
    onPrompt({
      step: promptStep,
      elapsedMs,
      screenshot,
      viewport,
      prompt,
    });
  }
  const modelStartedAt = Date.now();
  const result = await modelClient({
    messages: [
      {
        role: 'user',
        content: [
          { type: 'text', text: prompt },
          { type: 'image_url', image_url: { url: dataUrl(screenshot) } },
        ],
      },
    ],
    maxTokens: Number(job.agentMaxTokens || 2400),
    timeoutMs: Number(job.agentTimeoutMs || 120000),
    temperature: Number(job.agentTemperature ?? 0.2),
  });

  let sequence;
  try {
    sequence = normalizeSequence(result.json, {
      viewport,
      config: job,
      maxDurationMs: Number(job.agentMaxActionMs || 8000),
    });
  } catch (error) {
    error.code = MODEL_SEQUENCE_ERROR_CODE;
    error.responseText = String(result.text || '').slice(0, 8000);
    error.modelJson = result.json;
    throw error;
  }

  return {
    sequence,
    model: result.model,
    modelElapsedMs: Date.now() - modelStartedAt,
    rawText: String(result.text || '').slice(0, 8000),
    usage: result.usage,
  };
}

function isRecoverableModelSequenceError(error) {
  return isModelJsonParseError(error) || Boolean(error && error.code === MODEL_SEQUENCE_ERROR_CODE);
}

function failedActionAfterInvalidJson({ error }) {
  const schemaError = Boolean(error && error.code === MODEL_SEQUENCE_ERROR_CODE);
  const message = String(error && (error.message || error) || 'model returned invalid JSON').slice(0, 500);
  return {
    durationMs: 0,
    actions: [],
    shouldStop: false,
    summary: schemaError
      ? 'Failed action: the model returned a sequence that did not match the action schema, so no application input was sent.'
      : 'Failed action: the model returned invalid JSON, so no application input was sent.',
    previousSequenceOutcome: '',
    rationale: `Record the model output failure, keep the current screenshot and state unchanged, and ask for a new valid JSON action sequence. Error: ${message}`,
    failedAction: true,
    error: message,
    errorType: schemaError ? 'schema' : 'json',
  };
}

function failedActionResult({ screenshot, state, error }) {
  return {
    ok: false,
    screenshot,
    screenshotChanged: false,
    captureCount: 0,
    state,
    error: String(error && (error.message || error) || 'action failed').slice(0, 500),
  };
}

function actionsByType(actions, type) {
  return actions.filter((action) => action.type === type);
}

function sequenceBuckets(actions) {
  return {
    clicks: actionsByType(actions, 'click'),
    drags: actionsByType(actions, 'drag'),
    cursorMoves: actionsByType(actions, 'cursor_move'),
    viewMoves: actionsByType(actions, 'view_move'),
  };
}

function actionsWithinDuration(actions, duration) {
  const limit = Number(duration);
  if (!Number.isFinite(limit) || limit < 0) return [];

  return actions
    .filter((action) => Number(action.start) < limit)
    .map((action) => {
      if (!Object.prototype.hasOwnProperty.call(action, 'end') || Number(action.end) <= limit) return action;
      return { ...action, end: limit };
    })
    .filter((action) => !Object.prototype.hasOwnProperty.call(action, 'end') || Number(action.end) > Number(action.start));
}

async function runAgenticPlaytest({ job, initialResponse, runAction, outputDir, modelClient = chatCompletion, log = () => {} }) {
  const maxMs = Number(job.playtestDurationMs ?? 120000);
  const minMs = Number(job.agentMinPlaytestMs ?? Math.min(30000, maxMs));
  const recorder = new AgentRecorder(outputDir || path.join(process.cwd(), 'artifacts', 'agent'));
  const history = [];
  const startedAt = Date.now();
  let lastResponse = initialResponse;
  let step = 0;
  let stoppedByAgent = false;
  let modelErrorCount = 0;
  let consecutiveModelErrors = 0;
  const maxModelFallbacks = Math.max(1, Math.round(Number(job.agentMaxModelFallbacks || 3)));

  while (Date.now() - startedAt < maxMs - STOP_RESERVE_MS) {
    const elapsedMs = Date.now() - startedAt;
    const remainingMs = maxMs - elapsedMs - STOP_RESERVE_MS;
    if (remainingMs <= 0) break;

    const screenshot = latestScreenshot(lastResponse);
    const state = responseState(lastResponse);
    recorder.observation({ step, elapsedMs, screenshot, state });

    let sequence;
    let model;
    let modelElapsedMs;
    let rawText;
    let usage;
    try {
      ({ sequence, model, modelElapsedMs, rawText, usage } = await decideNextSequence({
        job,
        screenshot,
        state,
        history,
        elapsedMs,
        maxMs,
        modelClient,
        promptStep: step + 1,
        onPrompt: (payload) => recorder.prompt(payload),
      }));
      consecutiveModelErrors = 0;
    } catch (error) {
      if (!isRecoverableModelSequenceError(error)) throw error;
      modelErrorCount += 1;
      consecutiveModelErrors += 1;
      log('agent.model_sequence_error', {
        step: step + 1,
        consecutiveModelErrors,
        type: error.code === MODEL_SEQUENCE_ERROR_CODE ? 'schema' : 'json',
        error: String(error.message || error).slice(0, 500),
      });
      sequence = failedActionAfterInvalidJson({ error });
      model = error.code === MODEL_SEQUENCE_ERROR_CODE
        ? 'failed-action-after-invalid-model-sequence'
        : 'failed-action-after-invalid-json';
      modelElapsedMs = 0;
      rawText = String(error.responseText || error.message || '').slice(0, 8000);
      usage = null;
    }

    if (history.length && sequence.previousSequenceOutcome) {
      history[history.length - 1].outcomeSummary = sequence.previousSequenceOutcome;
    }

    const duration = Math.max(100, Math.min(sequence.durationMs, remainingMs));
    let stepActions = [];
    if (!sequence.failedAction) {
      stepActions = actionsWithinDuration(sequence.actions, duration);
      if (sequence.actions.length && !stepActions.length) {
        const error = new Error('all model actions were outside the remaining sequence duration after clipping');
        error.code = MODEL_SEQUENCE_ERROR_CODE;
        modelErrorCount += 1;
        consecutiveModelErrors += 1;
        log('agent.model_sequence_error', {
          step: step + 1,
          consecutiveModelErrors,
          type: 'schema',
          error: error.message,
        });
        sequence = failedActionAfterInvalidJson({ error });
        model = 'failed-action-after-invalid-model-sequence';
      }
    }
    step += 1;
    if (sequence.failedAction) {
      const actionName = `agent-step-${String(step).padStart(3, '0')}-failed-action`;
      const stepRequest = {
        action: 'none',
        action_name: actionName,
        name: 'failed-action',
        reason: sequence.error,
      };
      recorder.sequence({
        step,
        elapsedMs,
        screenshot,
        sequence,
        stepRequest,
        model,
        modelElapsedMs,
        rawText,
        usage,
      });
      log('agent.failed_action', {
        step,
        model,
        modelElapsedMs,
        error: sequence.error,
      });

      const failedResult = failedActionResult({ screenshot, state, error: sequence.error });
      history.push({
        step,
        summary: sequence.summary,
        rationale: sequence.rationale,
        actions: [{ type: 'failed_action', error: sequence.error }],
        clicks: [],
        drags: [],
        cursorMoves: [],
        viewMoves: [],
        failedAction: true,
        result: failedResult,
      });

      if (consecutiveModelErrors >= maxModelFallbacks) break;
      continue;
    }

    const stepRequest = {
      action: 'step',
      action_name: `agent-step-${String(step).padStart(3, '0')}`,
      duration,
      actions: stepActions,
      captures: [duration],
      autoCaptures: false,
    };
    const buckets = sequenceBuckets(stepRequest.actions);

    recorder.sequence({
      step,
      elapsedMs,
      screenshot,
      sequence: { ...sequence, actions: stepRequest.actions },
      stepRequest,
      model,
      modelElapsedMs,
      rawText,
      usage,
    });
    log('agent.sequence', {
      step,
      duration,
      model,
      modelElapsedMs,
      actionCount: stepRequest.actions.length,
      keyActionCount: actionsByType(stepRequest.actions, 'key').length,
      clickCount: buckets.clicks.length,
      dragCount: buckets.drags.length,
      cursorMoveCount: buckets.cursorMoves.length,
      viewMoveCount: buckets.viewMoves.length,
    });

    let stepResponse;
    try {
      stepResponse = await runAction(stepRequest);
    } catch (error) {
      const message = String(error && (error.message || error) || 'runwave action failed').slice(0, 500);
      log('agent.sequence_execution_error', {
        step,
        error: message,
      });
      log('agent.failed_action', {
        step,
        model,
        modelElapsedMs,
        error: message,
      });
      history.push({
        step,
        summary: sequence.summary || 'Failed action: the action sequence could not be executed.',
        rationale: `Record the action execution failure, keep the current screenshot and state unchanged, and ask for a new action sequence. Error: ${message}`,
        actions: stepRequest.actions,
        clicks: buckets.clicks,
        drags: buckets.drags,
        cursorMoves: buckets.cursorMoves,
        viewMoves: buckets.viewMoves,
        failedAction: true,
        result: failedActionResult({ screenshot, state, error: message }),
      });
      continue;
    }

    lastResponse = stepResponse;
    const result = postSequenceResult(lastResponse, screenshot);
    history.push({
      step,
      summary: sequence.summary,
      rationale: sequence.rationale,
      actions: stepRequest.actions,
      clicks: buckets.clicks,
      drags: buckets.drags,
      cursorMoves: buckets.cursorMoves,
      viewMoves: buckets.viewMoves,
      result,
    });

    if (consecutiveModelErrors >= maxModelFallbacks) break;

    if (sequence.shouldStop && Date.now() - startedAt >= minMs) {
      stoppedByAgent = true;
      break;
    }
  }

  const summary = {
    mode: 'agent',
    steps: step,
    elapsedMs: Date.now() - startedAt,
    stoppedByAgent,
    maxMs,
    minMs,
    modelErrorCount,
    history,
  };
  recorder.summary(summary);
  return {
    ...summary,
    lastResponse,
    outputDir: recorder.outputDir,
  };
}

module.exports = {
  decideNextSequence,
  actionsWithinDuration,
  failedActionAfterInvalidJson,
  isRecoverableModelSequenceError,
  latestScreenshot,
  postSequenceResult,
  responseState,
  runAgenticPlaytest,
};
