'use strict';

function log(level, msg, fields = {}) {
  const line = {
    ts: new Date().toISOString(),
    level,
    service: 'victim-api',
    msg,
    ...fields,
  };
  process.stdout.write(`${JSON.stringify(line)}\n`);
}

module.exports = { log };
