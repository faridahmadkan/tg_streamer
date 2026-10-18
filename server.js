'use strict';

// Railway-friendly HTTP wrapper. The Python process remains the actual bot and
// streaming engine; this small Node.js server exposes a health endpoint without
// requiring Docker or changing any playback implementation.
const http = require('http');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const PORT = Number(process.env.PORT || 3000);
const pythonCandidates = [
  process.env.PYTHON,
  path.join(__dirname, '.venv', 'bin', 'python'),
  'python3',
].filter(Boolean);
const python = pythonCandidates.find((candidate) =>
  candidate === 'python3' || candidate === process.env.PYTHON || fs.existsSync(candidate)
) || 'python3';

let botProcess;
let botStartedAt = null;
let botExit = null;

const server = http.createServer((req, res) => {
  if (req.url === '/' || req.url === '/health' || req.url === '/healthz') {
    const healthy = Boolean(botProcess && botProcess.exitCode === null && botExit === null);
    res.writeHead(healthy ? 200 : 503, { 'content-type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({
      status: healthy ? 'ok' : 'starting_or_stopped',
      service: 'tg-streaming-bot',
      engine: 'python-pytgcalls',
      uptime_seconds: botStartedAt ? Math.floor((Date.now() - botStartedAt) / 1000) : 0,
      bot_exit_code: botExit,
    }));
    return;
  }
  res.writeHead(404, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify({ error: 'not_found' }));
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`Health server listening on 0.0.0.0:${PORT}`);
  console.log(`Python executable: ${python}`);
  botStartedAt = Date.now();
  botProcess = spawn(python, ['-u', 'main.py'], {
    cwd: __dirname,
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
    stdio: 'inherit',
  });
  botProcess.on('error', (err) => {
    console.error('Failed to start Python bot process:', err);
    botExit = -1;
  });
  botProcess.on('exit', (code, signal) => {
    botExit = code === null ? (signal ? `signal:${signal}` : -1) : code;
    console.error(`Python bot exited (code=${code}, signal=${signal || 'none'}).`);
    // Exit so the hosting platform can restart a crashed bot rather than
    // reporting a healthy web service with no Telegram worker.
    server.close(() => process.exit(typeof code === 'number' ? code : 1));
  });
});

function shutdown(signal) {
  console.log(`Received ${signal}; stopping bot and health server...`);
  if (botProcess && botProcess.exitCode === null) botProcess.kill('SIGTERM');
  const forceExit = setTimeout(() => process.exit(1), 10000);
  forceExit.unref();
  server.close(() => process.exit(0));
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
