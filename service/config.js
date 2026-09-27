'use strict';

const API_KEY_PATTERN = /^[0-9a-f]{32}$/;

function requireValue(env, key) {
  const value = env[key];
  if (value === undefined || value === '') {
    throw new Error(`Missing required config ${key}`);
  }
  return value;
}

function parsePort(raw) {
  const port = Number.parseInt(raw, 10);
  if (!Number.isInteger(port) || String(port) !== String(raw).trim() || port < 1 || port > 65535) {
    throw new Error(`Invalid VICTIM_PORT: ${JSON.stringify(raw)} (expected integer 1-65535)`);
  }
  return port;
}

function validateApiKey(raw) {
  if (!API_KEY_PATTERN.test(raw)) {
    throw new Error(
      `Invalid VICTIM_API_KEY: expected 32-char lowercase hex, got ${JSON.stringify(raw)}`,
    );
  }
  return raw;
}

function loadConfig(env) {
  return {
    port: parsePort(env.VICTIM_PORT ?? '8080'),
    apiKey: validateApiKey(requireValue(env, 'VICTIM_API_KEY')),
  };
}

module.exports = { loadConfig, parsePort, validateApiKey };
