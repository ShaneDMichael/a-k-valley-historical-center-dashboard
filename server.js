import 'dotenv/config';
import crypto from 'crypto';
import express from 'express';
import path from 'path';
import { fileURLToPath } from 'url';
import axios from 'axios';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const port = process.env.PORT ? Number(process.env.PORT) : 5173;

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Serve Three.js ESM modules from node_modules
app.use('/vendor/three', express.static(path.join(__dirname, 'node_modules', 'three')));

function requireViewerToken(req, res, next) {
  const expected = process.env.VIEWER_TOKEN;
  if (!expected) return next();

  const provided =
    (typeof req.query?.token === 'string' ? req.query.token : null) ||
    (typeof req.header('x-viewer-token') === 'string' ? req.header('x-viewer-token') : null);

  if (provided !== expected) {
    return res.status(401).json({ error: 'unauthorized' });
  }

  return next();
}

app.use('/api', requireViewerToken);

function buildSwitchBotHeaders() {
  const token = typeof process.env.SWITCHBOT_TOKEN === 'string' ? process.env.SWITCHBOT_TOKEN.trim() : process.env.SWITCHBOT_TOKEN;
  const secret = typeof process.env.SWITCHBOT_SECRET === 'string' ? process.env.SWITCHBOT_SECRET.trim() : process.env.SWITCHBOT_SECRET;

  if (!token || !secret) {
    const missing = [!token ? 'SWITCHBOT_TOKEN' : null, !secret ? 'SWITCHBOT_SECRET' : null].filter(Boolean);
    const err = new Error(`Missing env vars: ${missing.join(', ')}`);
    err.statusCode = 500;
    throw err;
  }

  const t = Date.now();
  const nonce = crypto.randomUUID();

  const data = `${token}${t}${nonce}`;
  const sign = crypto.createHmac('sha256', secret).update(data).digest('base64');

  return {
    Authorization: token,
    t: String(t),
    nonce,
    sign,
    'Content-Type': 'application/json',
  };
}

app.get('/api/devices', async (req, res) => {
  try {
    const headers = buildSwitchBotHeaders();
    const url = 'https://api.switch-bot.com/v1.1/devices';
    const response = await axios.get(url, { headers, timeout: 10_000 });

    const list = response?.data?.body?.deviceList;
    return res.json({ devices: Array.isArray(list) ? list : [], fetchedAt: new Date().toISOString() });
  } catch (e) {
    const status = e?.statusCode || e?.response?.status || 500;
    const message = e?.response?.data || e?.message || 'Unknown error';
    return res.status(status).json({ error: message });
  }
});

app.get('/api/temperature', async (req, res) => {
  try {
    const requestedId = typeof req.query?.deviceId === 'string' ? req.query.deviceId.trim() : null;
    const deviceId = requestedId || process.env.SWITCHBOT_DEVICE_ID;
    if (!deviceId) {
      return res.status(500).json({ error: 'Missing env var: SWITCHBOT_DEVICE_ID' });
    }

    if (!/^[A-Za-z0-9]{12,32}$/.test(deviceId)) {
      return res.status(400).json({ error: 'Invalid deviceId' });
    }

    const headers = buildSwitchBotHeaders();
    const url = `https://api.switch-bot.com/v1.1/devices/${encodeURIComponent(deviceId)}/status`;

    const response = await axios.get(url, { headers, timeout: 10_000 });

    const payload = response?.data;

    if (payload?.statusCode && payload.statusCode !== 100) {
      return res.status(502).json({ error: payload?.message || 'switchbot_error', statusCode: payload.statusCode });
    }

    // SwitchBot wraps device status under `body` for this endpoint
    const body = payload?.body;

    // Meter-like devices generally report temperature / humidity
    const temperature = body?.temperature ?? body?.temp ?? body?.tempC;
    const humidity = body?.humidity ?? body?.humid;
    const battery = body?.battery;

    return res.json({
      temperature,
      humidity,
      battery,
      fetchedAt: new Date().toISOString(),
    });
  } catch (e) {
    const status = e?.statusCode || e?.response?.status || 500;
    const message = e?.response?.data || e?.message || 'Unknown error';
    return res.status(status).json({ error: message });
  }
});

app.listen(port, () => {
  console.log(`Server running on http://localhost:${port}`);
});
