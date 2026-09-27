'use strict';

const http = require('http');
const { loadConfig } = require('./config');
const { log } = require('./logger');

const config = loadConfig(process.env);

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(body);
}

function readBody(req, callback) {
  let body = '';
  req.on('data', (chunk) => {
    body += chunk.toString('utf8');
  });
  req.on('end', () => callback(body));
}

function createItem(body, res) {
  const payload = JSON.parse(body);
  const name = payload.name.trim();
  const quantity = payload.quantity.toFixed(2);
  const tags = payload.tags.map((tag) => String(tag).toUpperCase());
  sendJson(res, 201, {
    id: `${name}-${quantity}`,
    name,
    quantity: Number(quantity),
    tags,
  });
}

function bulkImport(body, res) {
  const items = JSON.parse(body);
  const total = items.reduce((sum, value) => sum + Number(value), 0);
  sendJson(res, 200, { accepted: items.length, total });
}

function slow(res, ms) {
  const started = Date.now();
  while (Date.now() - started < ms) {}
  sendJson(res, 200, { sleptMs: ms });
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  log('info', 'request.received', { method: req.method, path: url.pathname });

  if (req.method === 'GET' && url.pathname === '/health') {
    sendJson(res, 200, { status: 'ok', uptimeSeconds: Math.round(process.uptime()) });
    return;
  }

  if (req.method === 'POST' && url.pathname === '/api/items') {
    readBody(req, (body) => createItem(body, res));
    return;
  }

  if (req.method === 'POST' && url.pathname === '/api/bulk') {
    readBody(req, (body) => bulkImport(body, res));
    return;
  }

  if (req.method === 'GET' && url.pathname === '/api/slow') {
    const requested = Number.parseInt(url.searchParams.get('ms') || '30000', 10);
    slow(res, Number.isInteger(requested) && requested >= 0 ? requested : 30000);
    return;
  }

  sendJson(res, 404, { error: 'not found' });
});

server.listen(config.port, () => {
  log('info', 'server.listening', { port: config.port });
});
