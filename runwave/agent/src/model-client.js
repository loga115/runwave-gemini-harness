'use strict';

const fs = require('fs');
const path = require('path');

const DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1';
const DEFAULT_MODEL = 'google/gemini-3.8-flash';
const DEFAULT_GEMINI_MODEL = 'gemini-3.8-flash';

// Runwave keeps OpenRouter as the zero-configuration default, while allowing
// callers to point the same Chat Completions request at any OpenAI-compatible
// endpoint. New configurations use only RUNWAVE_* names. The original
// OpenRouter names remain supported so existing jobs keep working.
const GENERIC_API_KEY_ENV = 'RUNWAVE_API_KEY';
const GENERIC_BASE_URL_ENV = 'RUNWAVE_BASE_URL';
const GENERIC_MODEL_ENV = 'RUNWAVE_AGENT_MODEL';

function modelJsonParseError(message, text, cause = null) {
  const error = new Error(message);
  error.name = 'ModelJsonParseError';
  error.code = 'RUNWAVE_MODEL_JSON_PARSE';
  error.responseText = String(text || '').slice(0, 8000);
  if (cause) error.cause = cause;
  return error;
}

function isModelJsonParseError(error) {
  return Boolean(error && error.code === 'RUNWAVE_MODEL_JSON_PARSE');
}

function parseFlatYamlValue(file, key) {
  if (!file || !fs.existsSync(file)) return null;
  const lines = fs.readFileSync(file, 'utf8').split(/\r?\n/);
  const pattern = new RegExp(`^\\s*["']?${key}["']?\\s*:\\s*(.*?)\\s*$`);
  for (const line of lines) {
    const match = line.match(pattern);
    if (!match) continue;
    let value = match[1].trim();
    if (!value) return null;
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    return value;
  }
  return null;
}

function configPath(env = process.env) {
  const home = env.HOME || env.USERPROFILE || '';
  return env.RUNWAVE_AGENT_CONFIG || (home ? path.join(home, '.c.yaml') : '');
}

function firstValue(env, names) {
  for (const name of names) {
    const value = env && env[name];
    if (value !== undefined && value !== null && String(value).trim()) return String(value).trim();
  }
  return null;
}

function endpointHost(baseUrl) {
  try {
    return new URL(baseUrl).hostname.toLowerCase();
  } catch {
    return '';
  }
}

function isOpenRouterEndpoint(baseUrl) {
  const host = endpointHost(baseUrl);
  return host === 'openrouter.ai' || host.endsWith('.openrouter.ai');
}

function isGeminiEndpoint(baseUrl) {
  const host = endpointHost(baseUrl);
  return host === 'generativelanguage.googleapis.com' || host.endsWith('.generativelanguage.googleapis.com');
}

function chatCompletionsUrl(baseUrl) {
  const normalized = String(baseUrl).replace(/\/+$/, '');
  return normalized.endsWith('/chat/completions') ? normalized : `${normalized}/chat/completions`;
}

function endpointErrorLabel(baseUrl) {
  return isOpenRouterEndpoint(baseUrl) ? 'OpenRouter' : 'Model endpoint';
}

function resolveBaseUrl(env = process.env, explicitBaseUrl = null) {
  const value = explicitBaseUrl || firstValue(env, [
    GENERIC_BASE_URL_ENV,
    'OPENROUTER_BASE_URL',
  ]) || DEFAULT_BASE_URL;
  const normalized = String(value).trim().replace(/\/+$/, '');
  try {
    // Fail early with a useful message instead of producing an opaque fetch
    // error later.  URL is available in every supported Node release.
    new URL(normalized);
  } catch {
    throw new Error(`invalid Runwave model endpoint URL: ${value}`);
  }
  return normalized;
}

function apiKeyNamesForEndpoint(baseUrl, env = process.env) {
  if (isGeminiEndpoint(baseUrl)) {
    return [GENERIC_API_KEY_ENV, 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_API_KEY'];
  }
  if (isOpenRouterEndpoint(baseUrl)) {
    return [GENERIC_API_KEY_ENV, 'OPENROUTER_API_KEY', 'RUNWAVE_OPENROUTER_API_KEY'];
  }
  // OPENROUTER_BASE_URL is part of the original contract. Honor its matching
  // key even when it points at an OpenRouter proxy with a different hostname.
  if (!env.RUNWAVE_BASE_URL && env.OPENROUTER_BASE_URL && baseUrl === resolveBaseUrl(env)) {
    return [GENERIC_API_KEY_ENV, 'OPENROUTER_API_KEY', 'RUNWAVE_OPENROUTER_API_KEY'];
  }
  return [GENERIC_API_KEY_ENV];
}

function resolveApiKey(env = process.env, explicitBaseUrl = null) {
  const baseUrl = resolveBaseUrl(env, explicitBaseUrl);
  const names = apiKeyNamesForEndpoint(baseUrl, env);
  const fromEnv = firstValue(env, names);
  if (fromEnv) return fromEnv;

  const file = configPath(env);
  for (const name of names) {
    const value = parseFlatYamlValue(file, name);
    if (value) return value;
  }
  return null;
}

// Kept as a compatibility alias for callers that used the original
// OpenRouter-only helper.  It now follows the provider-neutral resolution
// rules above.
function openRouterApiKey(env = process.env) {
  return resolveApiKey(env);
}

function resolveModel(env = process.env, explicitModel = null, explicitBaseUrl = null) {
  if (explicitModel) return String(explicitModel).trim();
  const baseUrl = resolveBaseUrl(env, explicitBaseUrl);
  const providerModelNames = isOpenRouterEndpoint(baseUrl) ? ['OPENROUTER_MODEL'] : [];
  const configured = firstValue(env, [GENERIC_MODEL_ENV, ...providerModelNames]);
  if (configured) return String(configured).trim();
  return isGeminiEndpoint(baseUrl) ? DEFAULT_GEMINI_MODEL : DEFAULT_MODEL;
}

function responseText(message) {
  const content = message && message.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .map((item) => {
      if (typeof item === 'string') return item;
      if (item && typeof item === 'object') return item.text || item.content || '';
      return '';
    })
    .join('\n');
}

function balancedJsonObject(text) {
  const start = text.indexOf('{');
  if (start < 0) return null;

  let depth = 0;
  let inString = false;
  let escape = false;
  for (let i = start; i < text.length; i += 1) {
    const char = text[i];
    if (escape) {
      escape = false;
      continue;
    }
    if (char === '\\') {
      escape = true;
      continue;
    }
    if (char === '"') {
      inString = !inString;
      continue;
    }
    if (inString) continue;
    if (char === '{') depth += 1;
    if (char === '}') {
      depth -= 1;
      if (depth === 0) return text.slice(start, i + 1);
    }
  }
  return null;
}

function stripBareFragmentLines(text) {
  const lines = String(text || '').split(/\r?\n/);
  let changed = false;
  const kept = [];
  let droppingTail = false;

  for (const line of lines) {
    const trimmed = line.trim();
    if (droppingTail) {
      changed = true;
      continue;
    }
    if (!trimmed) {
      kept.push(line);
      continue;
    }

    const startsLikeJson =
      /^[{[]/.test(trimmed) ||
      /^[}\]],?$/.test(trimmed) ||
      /^"[^"]+"\s*:/.test(trimmed) ||
      /^,\s*$/.test(trimmed);

    if (!startsLikeJson && /"\s*,?$/.test(trimmed)) {
      changed = true;
      droppingTail = true;
      continue;
    }

    kept.push(line);
  }

  return changed ? kept.join('\n') : null;
}

function closeOpenJsonContainers(text) {
  const stack = [];
  let inString = false;
  let escape = false;

  for (const char of String(text || '')) {
    if (escape) {
      escape = false;
      continue;
    }
    if (char === '\\') {
      escape = true;
      continue;
    }
    if (char === '"') {
      inString = !inString;
      continue;
    }
    if (inString) continue;

    if (char === '{') stack.push('}');
    else if (char === '[') stack.push(']');
    else if (char === '}' || char === ']') {
      if (!stack.length || stack[stack.length - 1] !== char) return null;
      stack.pop();
    }
  }

  if (inString || !stack.length || stack.length > 4) return null;
  return `${text}\n${stack.reverse().join('')}`;
}

function parseJsonCandidate(text) {
  let lastParseError = null;
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === 'object') return { parsed, error: null };
  } catch (error) {
    lastParseError = error;
  }

  const repaired = stripBareFragmentLines(text);
  if (repaired) {
    try {
      const parsed = JSON.parse(repaired);
      if (parsed && typeof parsed === 'object') return { parsed, error: null };
    } catch (error) {
      lastParseError = error;
    }
  }

  const closed = repaired ? closeOpenJsonContainers(repaired) : null;
  if (closed) {
    try {
      const parsed = JSON.parse(closed);
      if (parsed && typeof parsed === 'object') return { parsed, error: null };
    } catch (error) {
      lastParseError = error;
    }
  }

  return { parsed: null, error: lastParseError };
}

function parseEmbeddedJsonCandidate(text) {
  const balanced = balancedJsonObject(text);
  if (balanced) {
    const parsed = parseJsonCandidate(balanced);
    if (parsed.parsed) return parsed;
    return { parsed: null, error: parsed.error, foundObject: true };
  }

  const repaired = stripBareFragmentLines(text);
  if (repaired) {
    const repairedBalanced = balancedJsonObject(repaired);
    if (repairedBalanced) {
      const parsed = parseJsonCandidate(repairedBalanced);
      if (parsed.parsed) return parsed;
      return { parsed: null, error: parsed.error, foundObject: true };
    }
  }

  return { parsed: null, error: null, foundObject: false };
}

function parseJsonResponse(text) {
  let lastParseError = null;
  const direct = parseJsonCandidate(text);
  if (direct.parsed) return direct.parsed;
  lastParseError = direct.error;

  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
  if (fenced) {
    const fencedBody = fenced[1].trim();
    const fencedParsed = parseJsonCandidate(fencedBody);
    if (fencedParsed.parsed) return fencedParsed.parsed;
    lastParseError = fencedParsed.error || lastParseError;

    const embeddedFenced = parseEmbeddedJsonCandidate(fencedBody);
    if (embeddedFenced.parsed) return embeddedFenced.parsed;
    if (embeddedFenced.foundObject) {
      throw modelJsonParseError('model response contained malformed JSON', text, embeddedFenced.error);
    }
  }

  const embedded = parseEmbeddedJsonCandidate(text);
  if (embedded.parsed) return embedded.parsed;
  if (embedded.foundObject) {
    throw modelJsonParseError('model response contained malformed JSON', text, embedded.error);
  }
  throw modelJsonParseError('model response did not contain a JSON object', text, lastParseError);
}

function mimeType(file) {
  const suffix = path.extname(file).toLowerCase();
  if (suffix === '.png') return 'image/png';
  if (suffix === '.jpg' || suffix === '.jpeg') return 'image/jpeg';
  if (suffix === '.webp') return 'image/webp';
  return 'application/octet-stream';
}

function dataUrl(file) {
  const encoded = fs.readFileSync(file).toString('base64');
  return `data:${mimeType(file)};base64,${encoded}`;
}

async function chatCompletion({
  messages,
  maxTokens = 2400,
  temperature = 0.2,
  timeoutMs = 120000,
  attempts = null,
  apiKey: explicitApiKey = null,
  baseUrl: explicitBaseUrl = null,
  model: explicitModel = null,
  env = process.env,
}) {
  const baseUrl = resolveBaseUrl(env, explicitBaseUrl);
  const apiKey = explicitApiKey || resolveApiKey(env, baseUrl);
  if (!apiKey) {
    if (isOpenRouterEndpoint(baseUrl)) {
      // Preserve the original diagnostic for existing OpenRouter jobs.
      throw new Error(`OPENROUTER_API_KEY not found in environment or ${configPath(env)}`);
    }
    throw new Error(
      `Runwave model API key not found; set RUNWAVE_API_KEY `
      + `in the environment or ${configPath(env)}`
    );
  }

  const model = resolveModel(env, explicitModel, baseUrl);
  const attemptCount = Math.max(1, Math.round(Number(attempts || env.RUNWAVE_AGENT_MODEL_ATTEMPTS || 3)));
  let lastError = null;
  let requestMessages = messages;

  for (let attempt = 1; attempt <= attemptCount; attempt += 1) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    const headers = {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
    };
    // These headers are useful metadata for OpenRouter, but some compatible
    // providers reject unknown provider-specific headers.  Send them only to
    // OpenRouter while keeping the request itself provider-neutral.
    if (isOpenRouterEndpoint(baseUrl)) {
      headers['HTTP-Referer'] = env.RUNWAVE_HTTP_REFERER || 'https://github.com/parsewave/runwave';
      headers['X-Title'] = 'Runwave agentic web-application tester';
    }

    const response = await fetch(chatCompletionsUrl(baseUrl), {
      method: 'POST',
      signal: controller.signal,
      headers,
      body: JSON.stringify({
        model,
        messages: requestMessages,
        temperature: attempt === 1 ? temperature : 0,
        max_tokens: maxTokens,
        stream: false,
        response_format: { type: 'json_object' },
      }),
    }).finally(() => clearTimeout(timeout));

    const body = await response.text();
    try {
      if (!response.ok) {
        throw new Error(`${endpointErrorLabel(baseUrl)} HTTP ${response.status}: ${body.slice(0, 1200)}`);
      }

      const payload = JSON.parse(body);
      const choice = (payload.choices || [])[0] || {};
      const text = responseText(choice.message || {});
      if (!text.trim()) throw new Error('empty model response');

      const json = parseJsonResponse(text);
      return {
        model,
        text,
        json,
        usage: payload.usage || null,
        raw: payload,
      };
    } catch (error) {
      lastError = error;
      const retryable = attempt < attemptCount;
      if (!retryable) {
        if (body && !String(error.message).startsWith(`${endpointErrorLabel(baseUrl)} HTTP`)) {
          error.message = `${error.message}; response excerpt=${body.slice(0, 1200)}`;
        }
        throw error;
      }
      if (isModelJsonParseError(error)) {
        requestMessages = [
          ...messages,
          {
            role: 'user',
            content:
              'The previous answer was invalid JSON. Return exactly one valid JSON object matching the requested schema. Do not include markdown, prose, comments, or trailing text.',
          },
        ];
      }
      await new Promise((resolve) => setTimeout(resolve, Math.min(1500 * attempt, 5000)));
    }
  }

  throw lastError || new Error(`${endpointErrorLabel(baseUrl)} request failed`);
}

module.exports = {
  chatCompletion,
  chatCompletionsUrl,
  dataUrl,
  isModelJsonParseError,
  isGeminiEndpoint,
  isOpenRouterEndpoint,
  openRouterApiKey,
  parseJsonResponse,
  resolveApiKey,
  resolveBaseUrl,
  resolveModel,
};
