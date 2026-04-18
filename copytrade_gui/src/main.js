const { app, BrowserWindow, ipcMain, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const https = require('https');
const { spawn } = require('child_process');

let mainWindow = null;
let botProcess = null;

process.on('uncaughtException', (err) => {
  if (err && (err.code === 'EPIPE' || err.code === 'ERR_STREAM_DESTROYED')) {
    console.error('[Main] Suppressed EPIPE:', err.message);
    return;
  }
  console.error('[Main] Uncaught exception:', err);
});

function repoRoot() {
  // copytrade_gui/src -> bacopy repo root
  return path.join(__dirname, '..', '..', '..');
}

function resolveEnvPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, '.env');
  }
  return path.join(repoRoot(), '.env');
}

function loadDotEnv() {
  const envPath = resolveEnvPath();
  const env = {};
  if (!fs.existsSync(envPath)) return env;
  try {
    const content = fs.readFileSync(envPath, 'utf-8');
    for (const raw of content.split(/\r?\n/)) {
      const line = raw.trim();
      if (!line || line.startsWith('#')) continue;
      const m = line.match(/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
      if (!m) continue;
      env[m[1]] = m[2];
    }
  } catch (e) {
    console.error('[Main] .env load error:', e);
  }
  return env;
}

function resolveEngine() {
  if (app.isPackaged) {
    const engineDir = path.join(process.resourcesPath, 'engine');
    return {
      mode: 'packaged',
      exe: path.join(engineDir, 'bacopy_engine.exe'),
      cwd: engineDir,
      baseArgs: [],
    };
  }

  const root = repoRoot();
  const venvPython = process.platform === 'win32'
    ? path.join(root, 'venv', 'Scripts', 'python.exe')
    : path.join(root, 'venv', 'bin', 'python');
  const py = fs.existsSync(venvPython) ? venvPython : 'python';
  return {
    mode: 'dev',
    exe: py,
    cwd: root,
    baseArgs: ['-X', 'utf8', '-u', path.join(root, 'bacopy_executor_pragmatic_ws_live.py')],
  };
}

function sendToRenderer(channel, payload) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send(channel, payload);
}

let _stdoutRemainder = '';
function _emitStdoutLines(chunk) {
  const text = (_stdoutRemainder + String(chunk || '')).replace(/\r/g, '');
  const parts = text.split('\n');
  _stdoutRemainder = parts.pop() || '';

  for (const rawLine of parts) {
    const line = rawLine.trim();
    if (!line) continue;
    try {
      const msg = JSON.parse(line);
      sendToRenderer('agent-message', msg);
    } catch {
      sendToRenderer('agent-message', { type: 'log', message: line });
    }
  }
}

function _emitStderr(chunk) {
  const text = String(chunk || '');
  if (!text) return;
  sendToRenderer('agent-log', text);
}

function buildSpawnSpec(config) {
  const engine = resolveEngine();
  const envFile = loadDotEnv();
  const childEnv = { ...process.env, ...envFile, PYTHONIOENCODING: 'utf-8' };

  // Hidden master config (no GUI inputs)
  if (!childEnv.BACOPY_API_URL) childEnv.BACOPY_API_URL = 'https://master.bafather.uk';

  // Per-user/executor config (from GUI settings)
  if (config && config.executor_id) childEnv.BACOPY_EXECUTOR_ID = String(config.executor_id);
  if (config && config.executor_label) childEnv.BACOPY_EXECUTOR_LABEL = String(config.executor_label);
  if (config && config.stake_username) childEnv.BACOPY_EXECUTOR_USERNAME = String(config.stake_username);

  const args = [];
  if (engine.mode === 'packaged') {
    args.push('executor-pragmatic');
  }

  // SEQ7 config
  if (config && config.allow_switch_table) args.push('--allow-switch-table');
  const chipBase = (config && typeof config.chip_base === 'number') ? config.chip_base : 1;
  args.push('--chip-base', String(chipBase));

  const profitTarget = (config && typeof config.profit_target === 'number') ? config.profit_target : 50;
  args.push('--profit-target', String(profitTarget));

  const lossCut = (config && typeof config.loss_cut === 'number') ? config.loss_cut : 200;
  args.push('--loss-cut', String(lossCut));

  const profitSessionLimit = (config && Number.isFinite(Number(config.profit_session_limit))) ? Number(config.profit_session_limit) : 0;
  args.push('--profit-session-limit', String(profitSessionLimit));

  if (config && config.table_name_substr) args.push('--table-name-substr', String(config.table_name_substr));
  const waitSec = (config && Number.isFinite(Number(config.auto_click_wait_sec))) ? Number(config.auto_click_wait_sec) : 90;
  args.push('--auto-click-wait-sec', String(waitSec));

  if (config && config.headless) args.push('--headless');

  // Persistent browser profile (manual Stake login is stored here)
  const profileDir = path.join(app.getPath('userData'), 'profiles', 'executor_pragmatic');
  try { fs.mkdirSync(profileDir, { recursive: true }); } catch (_) {}
  args.push('--profile-dir', profileDir);

  if (engine.mode === 'packaged') {
    return { exe: engine.exe, cwd: engine.cwd, args, env: childEnv };
  }

  return { exe: engine.exe, cwd: engine.cwd, args: [...engine.baseArgs, ...args], env: childEnv };
}

function startBot(config) {
  if (botProcess) return;

  const spec = buildSpawnSpec(config || {});
  sendToRenderer('agent-message', { type: 'log', message: `[spawn] exe=${spec.exe} cwd=${spec.cwd} args=${JSON.stringify(spec.args)}` });

  botProcess = spawn(spec.exe, spec.args, {
    cwd: spec.cwd,
    env: spec.env,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });

  botProcess.stdout.on('data', _emitStdoutLines);
  botProcess.stderr.on('data', _emitStderr);

  botProcess.on('exit', (code) => {
    // flush remainder
    const rem = _stdoutRemainder.trim();
    if (rem) sendToRenderer('agent-message', { type: 'log', message: rem });
    _stdoutRemainder = '';

    sendToRenderer('agent-message', { type: 'stopped', code });
    botProcess = null;
  });

  botProcess.on('error', (err) => {
    sendToRenderer('agent-message', { type: 'error', message: err && err.message ? err.message : String(err) });
  });
}

function stopBot() {
  if (!botProcess) return;
  try {
    botProcess.kill();
  } catch (_) {}
}

// === Supabase Auth (bafather.uk) ===
let _supabaseConfig = null; // { url, anonKey }
let _supabaseSession = null; // { access_token, refresh_token, expires_at, user:{id,email} }

function _sessionPath() {
  return path.join(app.getPath('userData'), 'bafather_supabase_session.json');
}

function _loadSavedSession() {
  const p = _sessionPath();
  if (!fs.existsSync(p)) return null;
  try {
    return JSON.parse(fs.readFileSync(p, 'utf-8'));
  } catch {
    return null;
  }
}

function _saveSession(session) {
  if (!session) return;
  const payload = {
    access_token: session.access_token,
    refresh_token: session.refresh_token,
    expires_at: session.expires_at,
    user: session.user ? { id: session.user.id, email: session.user.email } : null,
    saved_at: Date.now(),
  };
  try {
    fs.mkdirSync(path.dirname(_sessionPath()), { recursive: true });
    fs.writeFileSync(_sessionPath(), JSON.stringify(payload, null, 2), 'utf-8');
  } catch (e) {
    console.error('[auth] failed to save session:', e);
  }
}

function _httpsGetJson(url) {
  return _httpsGetJsonFollow(url, 3);
}

function _httpsGetJsonFollow(url, redirectsLeft) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const req = https.request(
      {
        protocol: u.protocol,
        hostname: u.hostname,
        port: u.port || 443,
        path: u.pathname + (u.search || ''),
        method: 'GET',
        headers: {
          'User-Agent': 'BACOPYRECEIVER/1.0',
          'Accept': 'application/json',
        },
      },
      (res) => {
        const code = res.statusCode || 0;
        const loc = res.headers && res.headers.location ? String(res.headers.location) : '';
        if ([301, 302, 307, 308].includes(code) && loc && redirectsLeft > 0) {
          try {
            const next = new URL(loc, u).toString();
            res.resume();
            return resolve(_httpsGetJsonFollow(next, redirectsLeft - 1));
          } catch (e) {
            res.resume();
            return reject(e);
          }
        }

        let data = '';
        res.on('data', (c) => { data += c; });
        res.on('end', () => {
          try {
            resolve(JSON.parse(data));
          } catch (e) {
            reject(e);
          }
        });
      }
    );
    req.on('error', reject);
    req.end();
  });
}

function _loadSupabaseFromWebEnvLocal() {
  // Dev-only convenience: use web/.env.local NEXT_PUBLIC_* when root .env is missing.
  if (app.isPackaged) return null;
  const p = path.join(repoRoot(), 'web', '.env.local');
  if (!fs.existsSync(p)) return null;
  try {
    const out = {};
    const content = fs.readFileSync(p, 'utf-8');
    for (const raw of content.split(/\r?\n/)) {
      const line = raw.trim();
      if (!line || line.startsWith('#')) continue;
      const m = line.match(/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
      if (!m) continue;
      const k = m[1];
      const v = m[2];
      if (k === 'NEXT_PUBLIC_SUPABASE_URL') out.url = v;
      if (k === 'NEXT_PUBLIC_SUPABASE_ANON_KEY') out.anonKey = v;
    }
    if (out.url && out.anonKey) return out;
  } catch (_) {}
  return null;
}

function _httpsJson(method, url, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const payload = body ? Buffer.from(JSON.stringify(body), 'utf-8') : null;

    const req = https.request(
      {
        protocol: u.protocol,
        hostname: u.hostname,
        port: u.port || 443,
        path: u.pathname + (u.search || ''),
        method: String(method || 'GET').toUpperCase(),
        headers: {
          'User-Agent': 'BACOPYRECEIVER/1.0',
          'Accept': 'application/json',
          ...(payload ? { 'Content-Type': 'application/json', 'Content-Length': String(payload.length) } : {}),
          ...headers,
        },
      },
      (res) => {
        let data = '';
        res.on('data', (c) => { data += c; });
        res.on('end', () => {
          let parsed = null;
          try {
            parsed = data ? JSON.parse(data) : {};
          } catch (e) {
            return reject(e);
          }
          const ok = res.statusCode >= 200 && res.statusCode < 300;
          if (ok) return resolve(parsed);
          const msg = (parsed && (parsed.error_description || parsed.msg || parsed.error || parsed.message)) || `HTTP ${res.statusCode}`;
          const err = new Error(msg);
          err.statusCode = res.statusCode;
          err.payload = parsed;
          return reject(err);
        });
      }
    );

    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

async function getSupabaseConfig() {
  if (_supabaseConfig) return _supabaseConfig;

  const envFile = loadDotEnv();
  const url = envFile.NEXT_PUBLIC_SUPABASE_URL || envFile.BAFATHER_SUPABASE_URL || process.env.NEXT_PUBLIC_SUPABASE_URL || process.env.BAFATHER_SUPABASE_URL;
  const key = envFile.NEXT_PUBLIC_SUPABASE_ANON_KEY || envFile.BAFATHER_SUPABASE_ANON_KEY || process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || process.env.BAFATHER_SUPABASE_ANON_KEY;
  if (url && key) {
    _supabaseConfig = { url, anonKey: key };
    return _supabaseConfig;
  }

  const webLocal = _loadSupabaseFromWebEnvLocal();
  if (webLocal) {
    _supabaseConfig = { url: webLocal.url, anonKey: webLocal.anonKey };
    return _supabaseConfig;
  }

  let data = null;
  try {
    data = await _httpsGetJson('https://bafather.uk/api/public/supabase');
  } catch (_) {
    // Some deployments redirect root ↔ www (Node https does not auto-follow)
    data = await _httpsGetJson('https://www.bafather.uk/api/public/supabase');
  }
  if (!data || !data.ok || !data.supabase_url || !data.supabase_anon_key) {
    throw new Error('Failed to load Supabase public config');
  }
  _supabaseConfig = { url: data.supabase_url, anonKey: data.supabase_anon_key };
  return _supabaseConfig;
}

async function signInWithPassword(email, password) {
  const cfg = await getSupabaseConfig();
  const nowSec = Math.floor(Date.now() / 1000);
  const data = await _httpsJson(
    'POST',
    `${cfg.url}/auth/v1/token?grant_type=password`,
    { email, password },
    { apikey: cfg.anonKey }
  );

  const expiresIn = Number(data.expires_in || 0) || 3600;
  const session = {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: nowSec + expiresIn,
    user: data.user ? { id: data.user.id, email: data.user.email } : null,
  };

  _supabaseSession = session;
  _saveSession(session);
  return session;
}

async function ensureSession() {
  if (!_supabaseSession) {
    const saved = _loadSavedSession();
    if (saved && saved.access_token && saved.refresh_token) {
      _supabaseSession = saved;
    }
  }
  if (!_supabaseSession) return null;

  const nowSec = Math.floor(Date.now() / 1000);
  const exp = Number(_supabaseSession.expires_at || 0) || 0;
  if (exp === 0 || (exp - nowSec) < 60) {
    const cfg = await getSupabaseConfig();
    const ref = await _httpsJson(
      'POST',
      `${cfg.url}/auth/v1/token?grant_type=refresh_token`,
      { refresh_token: _supabaseSession.refresh_token },
      { apikey: cfg.anonKey }
    );
    const expiresIn = Number(ref.expires_in || 0) || 3600;
    _supabaseSession = {
      access_token: ref.access_token,
      refresh_token: ref.refresh_token || _supabaseSession.refresh_token,
      expires_at: nowSec + expiresIn,
      user: ref.user ? { id: ref.user.id, email: ref.user.email } : _supabaseSession.user,
    };
    _saveSession(_supabaseSession);
  }

  return _supabaseSession;
}

async function billingStatus() {
  const session = await ensureSession();
  if (!session || !session.user) {
    return { ok: false, reason: 'Not signed in', balance: 0 };
  }

  const cfg = await getSupabaseConfig();

  async function _fetchBillingRows(accessToken) {
    return _httpsJson(
      'GET',
      `${cfg.url}/rest/v1/billing?select=bot_paid,balance,suspended,is_free&limit=1`,
      null,
      { apikey: cfg.anonKey, Authorization: `Bearer ${accessToken}` }
    );
  }

  let rows = null;
  try {
    rows = await _fetchBillingRows(session.access_token);
  } catch (e) {
    if (e && e.statusCode === 401) {
      // force refresh and retry once
      _supabaseSession = { ...session, expires_at: 0 };
      const s2 = await ensureSession();
      if (!s2) return { ok: false, reason: 'Not signed in', balance: 0 };
      rows = await _fetchBillingRows(s2.access_token);
    } else {
      return { ok: false, reason: e && e.message ? e.message : 'Billing query failed', balance: 0 };
    }
  }

  const data = Array.isArray(rows) && rows.length ? rows[0] : null;
  if (!data) {
    return { ok: false, reason: 'No subscription found. Please purchase a plan at bafather.uk', balance: 0 };
  }

  const balance = typeof data.balance === 'number' ? data.balance : Number(data.balance || 0);
  const isFree = !!data.is_free;
  const botPaid = !!data.bot_paid;
  const suspended = !!data.suspended;

  if (!botPaid) {
    return { ok: false, reason: 'License not active. Please complete your purchase.', balance };
  }

  if (!isFree) {
    if (suspended) {
      return { ok: false, reason: 'Your account is suspended. Please contact admin.', balance };
    }
    if ((balance || 0) <= 0) {
      return { ok: false, reason: 'Balance is empty. Please charge to enable live betting.', balance };
    }
  }

  return {
    ok: true,
    balance,
    is_free: isFree,
    bot_paid: botPaid,
    suspended,
    email: session.user.email,
    user_id: session.user.id,
  };
}

// === Window ===
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 820,
    frame: false,
    backgroundColor: '#0f1117',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
}

app.whenReady().then(() => {
  createWindow();

  ipcMain.handle('window-minimize', () => { if (mainWindow) mainWindow.minimize(); return { ok: true }; });
  ipcMain.handle('window-maximize', () => {
    if (!mainWindow) return { ok: false };
    if (mainWindow.isMaximized()) mainWindow.unmaximize();
    else mainWindow.maximize();
    return { ok: true };
  });
  ipcMain.handle('window-close', () => { if (mainWindow) mainWindow.close(); return { ok: true }; });

  ipcMain.handle('open-external', (_evt, url) => shell.openExternal(String(url || '')));

  ipcMain.handle('start-bot', (_evt, config) => {
    startBot(config || {});
    return { ok: true };
  });

  ipcMain.handle('stop-bot', () => {
    stopBot();
    return { ok: true };
  });

  ipcMain.handle('auth-signin', async (_evt, payload) => {
    const email = String(payload && payload.email ? payload.email : '').trim();
    const password = String(payload && payload.password ? payload.password : '').trim();
    if (!email || !password) return { ok: false, reason: 'Email and password are required' };

    try {
      const session = await signInWithPassword(email, password);
      if (!session || !session.user) return { ok: false, reason: 'Sign-in failed' };
      return { ok: true, email: session.user.email, user_id: session.user.id };
    } catch (e) {
      return { ok: false, reason: e && e.message ? e.message : String(e) };
    }
  });

  ipcMain.handle('auth-session', async () => {
    try {
      const session = await ensureSession();
      if (!session || !session.user) return { ok: false };
      return { ok: true, email: session.user.email, user_id: session.user.id };
    } catch (e) {
      return { ok: false, reason: e && e.message ? e.message : String(e) };
    }
  });

  ipcMain.handle('billing-status', async () => {
    try {
      return await billingStatus();
    } catch (e) {
      return { ok: false, reason: e && e.message ? e.message : String(e), balance: 0 };
    }
  });
});

app.on('window-all-closed', () => {
  stopBot();
  if (process.platform !== 'darwin') app.quit();
});
