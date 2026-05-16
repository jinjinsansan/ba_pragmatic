const { app, BrowserWindow, ipcMain, shell } = require('electron');

// Single instance lock — prevent multiple GUI windows from running simultaneously.
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    // If user opens a second instance, focus the existing window instead.
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}
const path = require('path');
const fs = require('fs');
const https = require('https');
const { spawn, execSync } = require('child_process');
const os = require('os');

let mainWindow = null;
let botProcess = null;
let watchdogProcess = null;

function _pidFilePath() {
  try { return path.join(app.getPath('userData'), 'bacopy_process_tree.json'); }
  catch (_) { return path.join(os.tmpdir(), 'bacopy_process_tree.json'); }
}
function savePidTree(obj) {
  try {
    const p = _pidFilePath();
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, JSON.stringify(obj, null, 2), 'utf-8');
  } catch (e) { console.warn('[Main] savePidTree failed:', e.message); }
}
function loadPidTree() {
  try {
    const p = _pidFilePath();
    if (!fs.existsSync(p)) return null;
    return JSON.parse(fs.readFileSync(p, 'utf-8'));
  } catch (_) { return null; }
}
function killTreeWin(pid) {
  if (!pid) return;
  try {
    execSync(`taskkill /F /T /PID ${pid}`, { stdio: 'ignore' });
    console.log(`[Main] killTreeWin pid=${pid}`);
  } catch (_) {  }
}
function killAllByImage(imageName) {
  if (!imageName) return;
  try {
    execSync(`taskkill /F /T /IM "${imageName}"`, { stdio: 'ignore' });
    console.log(`[Main] killAllByImage ${imageName}`);
  } catch (_) {  }
}
function ensureCamoufoxAssets() {
  if (process.platform !== 'win32') return;
  try {
    const localAppData = process.env.LOCALAPPDATA;
    if (!localAppData) { console.warn('[camoufox] LOCALAPPDATA not set'); return; }
    const target = path.join(localAppData, 'camoufox');
    let needRestore = false;
    try {
      if (!fs.existsSync(target)) {
        needRestore = true;
      } else {
        const entries = fs.readdirSync(target);
        if (!entries || entries.length === 0) needRestore = true;
      }
    } catch (_) { needRestore = true; }
    if (!needRestore) return;

    let source = null;
    try {
      if (process.resourcesPath) {
        const p = path.join(process.resourcesPath, 'camoufox_firefox');
        if (fs.existsSync(p)) source = p;
      }
    } catch (_) {}
    if (!source) {
      console.warn('[camoufox] no bundled camoufox_firefox found; expecting runtime fetch');
      return;
    }

    console.log(`[camoufox] restoring bundled Firefox -> ${target}`);
    fs.mkdirSync(target, { recursive: true });
    try {
      fs.cpSync(source, target, { recursive: true, force: true });
      console.log('[camoufox] restore complete');
    } catch (e) {
      console.warn('[camoufox] cpSync failed, trying robocopy:', e && e.message);
      try {
        execSync(`robocopy "${source}" "${target}" /E /NFL /NDL /NJH /NJS /NP`, { stdio: 'ignore' });
      } catch (_) {}
    }
  } catch (e) {
    console.warn('[camoufox] ensureCamoufoxAssets error:', e && e.message);
  }
}

function cleanupOrphanCamoufox() {


  if (process.platform !== 'win32') return;
  const prev = loadPidTree();
  if (prev && Array.isArray(prev.camoufox_pids)) {
    for (const pid of prev.camoufox_pids) killTreeWin(pid);
  }
  if (prev && prev.executor_pid) killTreeWin(prev.executor_pid);


  killAllByImage('camoufox.exe');
  try { fs.unlinkSync(_pidFilePath()); } catch (_) {}
}
function listCamoufoxPids() {
  if (process.platform !== 'win32') return [];
  try {
    const out = execSync('tasklist /FI "IMAGENAME eq camoufox.exe" /FO CSV /NH', { encoding: 'utf-8' });
    const pids = [];
    for (const line of out.split(/\r?\n/)) {
      const m = line.match(/^"camoufox\.exe","(\d+)"/i);
      if (m) pids.push(parseInt(m[1], 10));
    }
    return pids;
  } catch (_) { return []; }
}

let userInitiatedStop = false;
let lastStartConfig = null;
let autoRestartCount = 0;
let lastSpawnAt = 0;
let autoRestartTimer = null;
let _botSpawning = false;
const MAX_AUTO_RESTARTS = 10;
const AUTO_RESTART_DELAY = 5000;
const STABLE_RUN_THRESHOLD = 5 * 60 * 1000;

let periodicRestartTimer = null;

function _periodicRestartHours() {
  const v = parseFloat(
    (process.env.BACOPY_PERIODIC_RESTART_HOURS || '').trim() ||
    (loadDotEnv().BACOPY_PERIODIC_RESTART_HOURS || '').trim() ||
    '6'
  );
  return Number.isFinite(v) && v > 0 ? v : 0;
}

function _telegramNotifyFromMain(text) {


  try {
    const env = loadDotEnv();
    const token = (env.TELEGRAM_BOT_TOKEN || process.env.TELEGRAM_BOT_TOKEN || '').trim();
    const chatId = (env.TELEGRAM_CHAT_ID || process.env.TELEGRAM_CHAT_ID || '').trim();
    if (!token || !chatId) return;
    const body = JSON.stringify({ chat_id: chatId, text: String(text).slice(0, 4000), disable_web_page_preview: true });
    const req = https.request({
      hostname: 'api.telegram.org',
      path: `/bot${token}/sendMessage`,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, (res) => { res.on('data', () => {}); res.on('end', () => {}); });
    req.on('error', () => {});
    req.write(body);
    req.end();
  } catch (_) {}
}

function schedulePeriodicRestart() {
  if (periodicRestartTimer) { clearInterval(periodicRestartTimer); periodicRestartTimer = null; }
  const h = _periodicRestartHours();
  if (h <= 0) { console.log('[periodic-restart] disabled'); return; }
  const ms = Math.floor(h * 3600 * 1000);
  console.log(`[periodic-restart] scheduled every ${h}h`);
  periodicRestartTimer = setInterval(() => {
    if (!botProcess || _botSpawning) return;

    console.log('[periodic-restart] firing (preventive restart)');
    _telegramNotifyFromMain('🔄 bacopy periodic restart (' + h + 'h maintenance)');
    try {
      const cfg = lastStartConfig;
      userInitiatedStop = false;

      const pid = botProcess.pid;
      if (process.platform === 'win32' && pid) killTreeWin(pid);
      try { botProcess.kill(); } catch (_) {}
      botProcess = null;


      setTimeout(() => {
        if (!botProcess && cfg && !_botSpawning) {
          try { _doStartBot && _doStartBot(cfg); } catch (e) { console.warn('[periodic-restart] respawn err:', e && e.message); }
        }
      }, 5000);
    } catch (e) {
      console.warn('[periodic-restart] error:', e && e.message);
    }
  }, ms);
}

process.on('uncaughtException', (err) => {
  if (err && (err.code === 'EPIPE' || err.code === 'ERR_STREAM_DESTROYED')) {
    console.error('[Main] Suppressed EPIPE:', err.message);
    return;
  }
  console.error('[Main] Uncaught exception:', err);
});

function repoRoot() {


  return path.join(__dirname, '..', '..');
}

function startWatchdog() {
  if (app.isPackaged) {


    console.log('[Main] watchdog skipped (packaged build)');
    return;
  }
  if (watchdogProcess) {
    console.log('[Main] watchdog already running');
    return;
  }
  const root = repoRoot();
  const script = path.join(root, 'scripts', 'watchdog_bacopy.py');
  if (!fs.existsSync(script)) {
    console.warn('[Main] watchdog script missing:', script);
    return;
  }
  const venvPython = process.platform === 'win32'
    ? path.join(root, 'venv', 'Scripts', 'python.exe')
    : path.join(root, 'venv', 'bin', 'python');
  const py = fs.existsSync(venvPython) ? venvPython : 'python';
  try {
    watchdogProcess = spawn(py, ['-X', 'utf8', '-u', script], {
      cwd: root,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    console.log('[Main] watchdog spawned pid=' + watchdogProcess.pid);
    watchdogProcess.stdout.on('data', (d) => {
      const s = String(d || '').trim();
      if (s) console.log('[watchdog]', s);
    });
    watchdogProcess.stderr.on('data', (d) => {
      const s = String(d || '').trim();
      if (s) console.warn('[watchdog:err]', s);
    });
    watchdogProcess.on('exit', (code) => {
      console.log('[Main] watchdog exited code=' + code);
      watchdogProcess = null;
    });
  } catch (e) {
    console.error('[Main] startWatchdog failed:', e && e.message);
    watchdogProcess = null;
  }
}

function stopWatchdog() {
  if (!watchdogProcess) return;
  const pid = watchdogProcess.pid;
  try {
    if (process.platform === 'win32') {
      killTreeWin(pid);
    } else {
      watchdogProcess.kill('SIGTERM');
    }
  } catch (_) {}
  watchdogProcess = null;
  console.log('[Main] watchdog stopped pid=' + pid);
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

function saveDotEnv(updates) {
  const envPath = resolveEnvPath();
  let content = '';
  try { content = fs.readFileSync(envPath, 'utf-8'); } catch (_) { content = ''; }
  for (const [key, val] of Object.entries(updates)) {
    const re = new RegExp(`^${key}=.*$`, 'm');
    const v = (val === undefined || val === null) ? '' : String(val);
    if (re.test(content)) {
      content = content.replace(re, `${key}=${v}`);
    } else {
      if (content && !content.endsWith('\n')) content += '\n';
      content += `${key}=${v}\n`;
    }
  }
  try {
    fs.mkdirSync(path.dirname(envPath), { recursive: true });
    fs.writeFileSync(envPath, content, 'utf-8');
  } catch (e) {
    console.error('[Main] .env save error:', e);
    throw e;
  }
}

function _telegramSendTest(botToken, chatId) {
  return new Promise((resolve) => {
    if (!botToken || !chatId) {
      return resolve({ ok: false, error: 'Bot Token / Chat ID が未設定です' });
    }
    const body = JSON.stringify({
      chat_id: chatId,
      text: '[BACOPYRECEIVER] Telegram test OK — 疎通確認できました.',
      disable_web_page_preview: true,
    });
    const req = https.request(
      {
        hostname: 'api.telegram.org',
        path: `/bot${botToken}/sendMessage`,
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      },
      (res) => {
        let data = '';
        res.on('data', (c) => { data += c; });
        res.on('end', () => {
          try {
            const json = JSON.parse(data || '{}');
            if (res.statusCode === 200 && json.ok) resolve({ ok: true });
            else resolve({ ok: false, error: json.description || `HTTP ${res.statusCode}` });
          } catch (e) {
            resolve({ ok: false, error: `parse error: ${e.message}` });
          }
        });
      },
    );
    req.on('error', (e) => resolve({ ok: false, error: e.message || String(e) }));
    req.setTimeout(10000, () => { try { req.destroy(new Error('timeout')); } catch (_) {} });
    req.write(body);
    req.end();
  });
}

let _supportTunnelProc = null;
let _supportTunnelReconnectTimer = null;
let _supportTunnelLastError = '';
let _supportTunnelFailCount = 0;
const _SUPPORT_TUNNEL_MAX_BACKOFF_MS = 5 * 60 * 1000;

const crypto = require('crypto');

function _decryptSupportKey(encryptedB64, email) {


  const SALT = Buffer.from(process.env.BACOPY_KEY_SALT || 'bacopy-support-v1-2026', 'utf-8');
  const key = crypto.pbkdf2Sync(String(email || '').toLowerCase(), SALT, 100000, 32, 'sha256');
  const data = Buffer.from(String(encryptedB64 || '').trim(), 'base64');
  const iv = data.slice(0, 16);
  const ciphertext = data.slice(16);
  const decipher = crypto.createDecipheriv('aes-256-cbc', key, iv);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
}

function _resolveSupportKeyPath(rawPath) {
  if (!rawPath) return '';


  if (rawPath.startsWith('~')) {
    return path.join(os.homedir(), rawPath.slice(1).replace(/^[\\/]/, ''));
  }
  if (path.isAbsolute(rawPath)) return rawPath;


  if (app.isPackaged) {
    const cand = path.join(process.resourcesPath, rawPath);
    if (fs.existsSync(cand)) return cand;
  }
  return path.join(repoRoot(), rawPath);
}

function startSupportTunnel() {
  if (_supportTunnelProc) return;
  const envFile = loadDotEnv();


  const defaultEnabled = app.isPackaged ? '1' : '0';
  const enabled = (envFile.BACOPY_SUPPORT_ENABLED || process.env.BACOPY_SUPPORT_ENABLED || defaultEnabled).trim();
  if (!['1', 'true', 'yes', 'on'].includes(enabled.toLowerCase())) {
    console.log('[support] tunnel disabled');
    return;
  }
  const sshHost = envFile.BACOPY_SUPPORT_SSH_HOST || process.env.BACOPY_SUPPORT_SSH_HOST || '';
  const rawKey = envFile.BACOPY_SUPPORT_SSH_KEY || process.env.BACOPY_SUPPORT_SSH_KEY
    || (app.isPackaged ? 'support_key' : '');
  const sshKeyPath = _resolveSupportKeyPath(rawKey);
  const remotePort = (envFile.BACOPY_SUPPORT_REMOTE_PORT || process.env.BACOPY_SUPPORT_REMOTE_PORT || '2222').trim();
  const localPort = (envFile.BACOPY_SUPPORT_LOCAL_PORT || process.env.BACOPY_SUPPORT_LOCAL_PORT || '22').trim();
  const isEncrypted = (envFile.BACOPY_SUPPORT_SSH_KEY_ENCRYPTED || '0') === '1';
  const userEmail = envFile.BACOPY_SUPPORT_USER_EMAIL || '';

  if (!sshHost) { console.warn('[support] SSH host not configured'); return; }
  if (!sshKeyPath || !fs.existsSync(sshKeyPath)) { console.warn('[support] key not found:', sshKeyPath); return; }

  let actualKeyPath = sshKeyPath;
  if (isEncrypted && userEmail) {
    try {
      const encryptedB64 = fs.readFileSync(sshKeyPath, 'utf-8').trim();
      const decrypted = _decryptSupportKey(encryptedB64, userEmail);
      actualKeyPath = path.join(os.tmpdir(), 'bacopy_support_key');
      fs.writeFileSync(actualKeyPath, decrypted, { mode: 0o600 });
    } catch (e) {
      console.error('[support] key decrypt failed:', e.message);
      return;
    }
  }

  const args = [
    '-i', actualKeyPath,
    '-o', 'StrictHostKeyChecking=no',
    '-o', 'BatchMode=yes',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
    '-N',
    '-R', `127.0.0.1:${remotePort}:127.0.0.1:${localPort}`,
    sshHost,
  ];
  console.log('[support] starting tunnel → ' + sshHost + ' (-R ' + remotePort + ':22)');
  _supportTunnelLastError = '';
  _supportTunnelFailCount = 0;

  _supportTunnelProc = spawn('ssh', args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
  _supportTunnelProc.on('error', (err) => {
    console.error('[support] spawn error:', err.message);
    _supportTunnelLastError = err.message;
  });
  _supportTunnelProc.stderr.on('data', (d) => {
    const s = String(d || '').trim();
    if (s) { console.error('[support]', s); _supportTunnelLastError = s; }
  });
  _supportTunnelProc.on('exit', (code) => {
    console.log('[support] tunnel exited code=' + code);
    _supportTunnelProc = null;
    if (isEncrypted && actualKeyPath !== sshKeyPath) {
      try { fs.unlinkSync(actualKeyPath); } catch (_) {}
    }


    if (_supportTunnelReconnectTimer) { clearTimeout(_supportTunnelReconnectTimer); _supportTunnelReconnectTimer = null; }
    const envNow = loadDotEnv();
    const stillEnabled = (envNow.BACOPY_SUPPORT_ENABLED || '0') === '1';
    if (stillEnabled) {
      _supportTunnelFailCount++;
      const delay = Math.min(10000 * Math.pow(1.5, _supportTunnelFailCount - 1), _SUPPORT_TUNNEL_MAX_BACKOFF_MS);
      console.log(`[support] reconnect in ${Math.round(delay/1000)}s (attempt ${_supportTunnelFailCount})`);
      _supportTunnelReconnectTimer = setTimeout(() => { _supportTunnelReconnectTimer = null; startSupportTunnel(); }, delay);
    }
  });
}

function stopSupportTunnel() {
  if (_supportTunnelReconnectTimer) { clearTimeout(_supportTunnelReconnectTimer); _supportTunnelReconnectTimer = null; }
  if (_supportTunnelProc) {
    try { _supportTunnelProc.kill(); } catch (_) {}
    _supportTunnelProc = null;
  }
}

function _startSupportTunnelStub() { startSupportTunnel(); }
function _stopSupportTunnelStub() { stopSupportTunnel(); }

function _setupLogPath() {
  return path.join(process.env.ProgramData || 'C:\\ProgramData', 'BACOPY', 'setup-all.log');
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



  if (!childEnv.BACOPY_API_URL) childEnv.BACOPY_API_URL = 'https://master.bafather.uk';
  if (!childEnv.BACOPY_API_CONNECT_TIMEOUT_SEC) childEnv.BACOPY_API_CONNECT_TIMEOUT_SEC = '5';
  if (!childEnv.BACOPY_API_TIMEOUT_SEC) childEnv.BACOPY_API_TIMEOUT_SEC = '15';





  try {
    const u = new URL(childEnv.BACOPY_API_URL);
    if (u.hostname === 'master.bafather.uk') {
      if (!childEnv.BACOPY_API_FALLBACK_IPS && !childEnv.BACOPY_API_FALLBACK_IP) {
        childEnv.BACOPY_API_FALLBACK_IPS = '210.131.215.116';
      }
    }
  } catch (_) {}



  // .env の BACOPY_EXECUTOR_ID を優先。localStorage のデフォルト値 'gui-1' より .env が勝つ。
  const envExecutorId = envFile.BACOPY_EXECUTOR_ID || '';
  const cfgExecutorId = (config && config.executor_id) ? String(config.executor_id) : '';
  const finalExecutorId = (cfgExecutorId && cfgExecutorId !== 'gui-1') ? cfgExecutorId : (envExecutorId || cfgExecutorId);
  if (finalExecutorId) childEnv.BACOPY_EXECUTOR_ID = finalExecutorId;

  const envExecutorLabel = envFile.BACOPY_EXECUTOR_LABEL || '';
  const cfgExecutorLabel = (config && config.executor_label) ? String(config.executor_label) : '';
  const finalExecutorLabel = (cfgExecutorLabel && cfgExecutorLabel !== 'MAIN-PC') ? cfgExecutorLabel : (envExecutorLabel || cfgExecutorLabel);
  if (finalExecutorLabel) childEnv.BACOPY_EXECUTOR_LABEL = finalExecutorLabel;
  if (config && config.stake_username) childEnv.BACOPY_EXECUTOR_USERNAME = String(config.stake_username);
  if (config && config.user_email) childEnv.BACOPY_USER_EMAIL = String(config.user_email);
  if (config && config.user_email) childEnv.BACOPY_BAFATHER_EMAIL = String(config.user_email);
  if (config && config.user_id) childEnv.BACOPY_USER_ID = String(config.user_id);


  try {
    const osName = `${process.platform} ${process.arch}`;
    childEnv.BACOPY_OS = osName;
  } catch (_) {}

  const args = [];
  if (engine.mode === 'packaged') {
    args.push('executor-pragmatic');
  }



  if (config && config.allow_switch_table) args.push('--allow-switch-table');
  if (config && config.allow_banker) args.push('--allow-banker');
  if (config && config.allow_tie) args.push('--allow-tie');
  if (config && config.assume_bc_012) args.push('--assume-bc-012');
  if (config && config.bet_mode) args.push('--bet-mode', String(config.bet_mode));
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



  const profileDir = path.join(app.getPath('userData'), 'profiles', 'executor_pragmatic');
  try { fs.mkdirSync(profileDir, { recursive: true }); } catch (_) {}
  args.push('--profile-dir', profileDir);

  if (engine.mode === 'packaged') {
    return { exe: engine.exe, cwd: engine.cwd, args, env: childEnv };
  }

  return { exe: engine.exe, cwd: engine.cwd, args: [...engine.baseArgs, ...args], env: childEnv };
}

function _mergedEnvForValidation() {


  const envFile = loadDotEnv();
  return { ...process.env, ...envFile };
}

function _validateConfigForSpawn(cfg) {










  return null;
}

function startBot(config) {
  const cfg = config || {};
  const verr = _validateConfigForSpawn(cfg);
  if (verr) {
    sendToRenderer('agent-message', { type: 'error', message: verr });
    return;
  }

  if (botProcess) {


    const old = botProcess;
    botProcess = null;
    try { old.removeAllListeners('exit'); } catch (_) {}
    try { old.removeAllListeners('error'); } catch (_) {}
    try { old.stdout?.removeAllListeners?.('data'); } catch (_) {}
    try { old.stderr?.removeAllListeners?.('data'); } catch (_) {}
    let started = false;
    old.once('exit', () => {
      if (started) return;
      started = true;
      _doStartBot(cfg);
    });
    try { old.kill(); } catch (_) {
      if (!started) {
        started = true;
        _doStartBot(cfg);
      }
    }
    return;
  }
  _doStartBot(cfg);
}

function _doStartBot(config) {
  if (_botSpawning) {
    console.log('[Main] _doStartBot: already spawning, skipped');
    return;
  }
  _botSpawning = true;




  try { cleanupOrphanCamoufox(); } catch (_) {}







  if (config && config.resume === false) {
    try {
      const profileDir = path.join(app.getPath('userData'), 'profiles', 'executor_pragmatic');
      const stateFile = path.join(profileDir, 'seq7_state.json');
      if (fs.existsSync(stateFile)) {
        fs.unlinkSync(stateFile);
        console.log('[Main] NEW SESSION — removed seq7_state.json');
      }
    } catch (e) {
      console.warn('[Main] seq7 state reset failed:', e.message);
    }
  }
  if (autoRestartTimer) {
    clearTimeout(autoRestartTimer);
    autoRestartTimer = null;
  }

  const cfg = config || {};
  const verr = _validateConfigForSpawn(cfg);
  if (verr) {
    sendToRenderer('agent-message', { type: 'error', message: verr });
    return;
  }
  const spec = buildSpawnSpec(cfg);



  lastStartConfig = cfg;
  userInitiatedStop = false;
  lastSpawnAt = Date.now();

  sendToRenderer('agent-message', { type: 'log', message: `[spawn] exe=${spec.exe} cwd=${spec.cwd} args=${JSON.stringify(spec.args)}` });

  botProcess = spawn(spec.exe, spec.args, {
    cwd: spec.cwd,
    env: spec.env,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  _botSpawning = false;





  savePidTree({ executor_pid: botProcess.pid, camoufox_pids: [], started_at: Date.now() });
  setTimeout(() => {
    try {
      const camPids = listCamoufoxPids();
      savePidTree({ executor_pid: botProcess ? botProcess.pid : null, camoufox_pids: camPids, started_at: Date.now() });
    } catch (_) {}
  }, 10000);

  botProcess.stdout.on('data', _emitStdoutLines);
  botProcess.stderr.on('data', _emitStderr);





  startWatchdog();

  const thisProcess = botProcess;
  const thisSpawnAt = lastSpawnAt;

  botProcess.on('exit', (code) => {


    const rem = _stdoutRemainder.trim();
    if (rem) sendToRenderer('agent-message', { type: 'log', message: rem });
    _stdoutRemainder = '';

    if (botProcess === thisProcess || botProcess === null) {
      sendToRenderer('agent-message', { type: 'stopped', code });
      botProcess = null;



      const ranDuration = Date.now() - thisSpawnAt;
      if (!userInitiatedStop && lastStartConfig) {
        if (ranDuration > STABLE_RUN_THRESHOLD) {
          console.log(`[Main] Stable run detected (${Math.round(ranDuration/1000)}s) — reset auto-restart counter`);
          autoRestartCount = 0;
        }
        if (autoRestartCount < MAX_AUTO_RESTARTS) {
          autoRestartCount++;
          const msg = `🔄 Auto-restart (${autoRestartCount}/${MAX_AUTO_RESTARTS}) — retry in ${AUTO_RESTART_DELAY/1000}s`;
          console.log('[Main]', msg);
          sendToRenderer('agent-message', { type: 'log', message: msg });
          autoRestartTimer = setTimeout(() => {
            autoRestartTimer = null;
            if (!botProcess && !userInitiatedStop && lastStartConfig) {
              sendToRenderer('agent-message', { type: 'log', message: '🔄 Auto-restart: restarting engine...' });
              _doStartBot(lastStartConfig);
            }
          }, AUTO_RESTART_DELAY);
        } else {
          const msg = `❌ Auto-restart failed ${MAX_AUTO_RESTARTS} times — manual START required`;
          console.error('[Main]', msg);
          sendToRenderer('agent-message', { type: 'log', message: msg });
          autoRestartCount = 0;
        }
      }
    } else {
      console.log('[Main] Ignoring exit from old process (new process already running)');
    }
  });

  botProcess.on('error', (err) => {
    sendToRenderer('agent-message', { type: 'error', message: err && err.message ? err.message : String(err) });
  });



  if (autoRestartCount > 0) {
    sendToRenderer('agent-message', { type: 'started' });
  }
}

function stopBot() {
  _botSpawning = false;
  if (!botProcess) {
    stopWatchdog();
    return;
  }
  userInitiatedStop = true;
  autoRestartCount = 0;
  if (autoRestartTimer) {
    clearTimeout(autoRestartTimer);
    autoRestartTimer = null;
  }


  const pid = botProcess.pid;
  if (process.platform === 'win32' && pid) {
    try { killTreeWin(pid); } catch (_) {}
  }
  try {
    botProcess.kill();
  } catch (_) {}
  stopWatchdog();
}

let _supabaseConfig = null;

let _supabaseSession = null;

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
  } catch (_) {}
  if (!data) {
    try {
      data = await _httpsGetJson('https://www.bafather.uk/api/public/supabase');
    } catch (_) {}
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
      `${cfg.url}/rest/v1/billing?select=bot_paid,balance,suspended,is_free,bot_config&limit=1`,
      null,
      { apikey: cfg.anonKey, Authorization: `Bearer ${accessToken}` }
    );
  }
  async function _fetchUnpaidInvoices(accessToken) {
    return _httpsJson(
      'GET',
      `${cfg.url}/rest/v1/daily_profit_invoices?select=outstanding_amount,settle_date&status=eq.unpaid&outstanding_amount=gt.0&order=settle_date.desc&limit=1`,
      null,
      { apikey: cfg.anonKey, Authorization: `Bearer ${accessToken}` }
    );
  }

  let rows = null;
  let unpaidRows = null;
  try {
    rows = await _fetchBillingRows(session.access_token);
    try {
      unpaidRows = await _fetchUnpaidInvoices(session.access_token);
    } catch (invoiceErr) {
      const msg = (invoiceErr && invoiceErr.message ? String(invoiceErr.message) : '').toLowerCase();
      if (!(msg.includes('does not exist') || msg.includes('daily_profit_invoices'))) throw invoiceErr;
      unpaidRows = [];
    }
  } catch (e) {
    if (e && e.statusCode === 401) {


      _supabaseSession = { ...session, expires_at: 0 };
      const s2 = await ensureSession();
      if (!s2) return { ok: false, reason: 'Not signed in', balance: 0 };
      rows = await _fetchBillingRows(s2.access_token);
      try {
        unpaidRows = await _fetchUnpaidInvoices(s2.access_token);
      } catch (invoiceErr) {
        const msg = (invoiceErr && invoiceErr.message ? String(invoiceErr.message) : '').toLowerCase();
        if (!(msg.includes('does not exist') || msg.includes('daily_profit_invoices'))) throw invoiceErr;
        unpaidRows = [];
      }
    } else {


      return { ok: false, network_error: true, reason: e && e.message ? e.message : 'Billing query failed', balance: 0 };
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
  const unpaid = Array.isArray(unpaidRows) && unpaidRows.length ? unpaidRows[0] : null;
  const fallbackOutstanding = data && data.bot_config && typeof data.bot_config === 'object' ? Number(data.bot_config.outstanding_fee_amount || 0) : 0;
  const unpaidAmount = unpaid && Number(unpaid.outstanding_amount) > 0
    ? Number(unpaid.outstanding_amount)
    : (fallbackOutstanding > 0 ? fallbackOutstanding : 0);

  if (!botPaid) {
    return { ok: false, reason: 'License not active. Please complete your purchase.', balance };
  }

  if (!isFree) {
    if (unpaidAmount > 0) {
      return { ok: false, reason: `Daily profit share payment is pending ($${unpaidAmount.toFixed(2)}). Please charge/pay before live betting.`, balance };
    }
    if (suspended) {
      return { ok: false, reason: 'Your account is suspended. Please contact admin.', balance };
    }
    if ((balance || 0) <= 0) {


      return { ok: false, balance_empty: true, reason: 'Balance is empty. Please charge to enable live betting.', balance };
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




  try { ensureCamoufoxAssets(); } catch (e) { console.warn('[Main] camoufox restore failed:', e.message); }
  try { cleanupOrphanCamoufox(); } catch (e) { console.warn('[Main] startup cleanup failed:', e.message); }

  createWindow();



  schedulePeriodicRestart();
  _telegramNotifyFromMain('🟢 bacopy GUI started');


  try { startSupportTunnel(); } catch (e) { console.warn('[support] startup err:', e && e.message); }

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





  ipcMain.handle('get-settings', () => {
    const env = loadDotEnv();
    return {
      telegram_bot_token: env.TELEGRAM_BOT_TOKEN || env.BACOPY_TELEGRAM_BOT_TOKEN || '',
      telegram_chat_id: env.TELEGRAM_CHAT_ID || env.BACOPY_TELEGRAM_CHAT_ID || '',
      support_enabled: (env.BACOPY_SUPPORT_ENABLED || env.LAPLACE_SUPPORT_ENABLED || '0'),
      support_email: env.BACOPY_SUPPORT_USER_EMAIL || env.LAPLACE_SUPPORT_USER_EMAIL || '',
      support_port: env.BACOPY_SUPPORT_REMOTE_PORT || env.LAPLACE_SUPPORT_REMOTE_PORT || '',
    };
  });

  ipcMain.handle('save-settings', (_evt, payload) => {
    const s = payload || {};
    const updates = {};
    if ('telegram_bot_token' in s) updates.TELEGRAM_BOT_TOKEN = s.telegram_bot_token || '';
    if ('telegram_chat_id' in s)   updates.TELEGRAM_CHAT_ID   = s.telegram_chat_id || '';
    if ('support_enabled' in s)    updates.BACOPY_SUPPORT_ENABLED = s.support_enabled ? '1' : '0';
    try {
      saveDotEnv(updates);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e.message || String(e) };
    }
  });

  ipcMain.handle('test-telegram', async () => {
    const env = loadDotEnv();
    const token = env.TELEGRAM_BOT_TOKEN || env.BACOPY_TELEGRAM_BOT_TOKEN || '';
    const chat  = env.TELEGRAM_CHAT_ID   || env.BACOPY_TELEGRAM_CHAT_ID   || '';
    return await _telegramSendTest(token, chat);
  });

  ipcMain.handle('toggle-support', (_evt, enabled) => {
    try {
      saveDotEnv({ BACOPY_SUPPORT_ENABLED: enabled ? '1' : '0' });
      if (enabled) _startSupportTunnelStub();
      else _stopSupportTunnelStub();
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e.message || String(e) };
    }
  });

  ipcMain.handle('get-support-info', () => {
    const env = loadDotEnv();
    return {
      email: env.BACOPY_SUPPORT_USER_EMAIL || env.LAPLACE_SUPPORT_USER_EMAIL || '',
      port:  env.BACOPY_SUPPORT_REMOTE_PORT || env.LAPLACE_SUPPORT_REMOTE_PORT || '',
      tunnel_status: _supportTunnelProc ? 'running' : 'stopped',
      last_error: _supportTunnelLastError || '',
      fail_count: _supportTunnelFailCount,
    };
  });





  ipcMain.handle('install-deps', () => {
    let scriptPath;
    if (app.isPackaged) {
      scriptPath = path.join(process.resourcesPath, 'setup-all.ps1');
    } else {
      scriptPath = path.join(__dirname, '..', 'scripts', 'setup-all.ps1');
    }
    if (!fs.existsSync(scriptPath)) {
      const msg = `setup-all.ps1 not found: ${scriptPath}`;
      console.warn('[install-deps]', msg);
      setTimeout(() => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send('install-deps-result', { success: false, message: msg });
        }
      }, 50);
      return { ok: false, error: msg };
    }
    const psq = (p) => `'${String(p).replace(/'/g, "''")}'`;


    const pubKeyPath = app.isPackaged
      ? path.join(process.resourcesPath, 'admin_pubkey.txt')
      : path.join(__dirname, '..', 'build_staging', 'admin_pubkey.txt');
    const pubKeyArg = fs.existsSync(pubKeyPath)
      ? `'-AdminPubKeyPath',${psq(pubKeyPath)},`
      : '';
    const cmd = `Start-Process powershell.exe -Verb RunAs -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',${psq(scriptPath)},${pubKeyArg}'-HardenSshdConfig')`;
    try {
      const cp = spawn('powershell.exe', ['-NoProfile', '-Command', cmd], { detached: true, windowsHide: true });
      cp.unref();
    } catch (e) {
      console.error('[install-deps] spawn failed:', e.message || e);
      return { ok: false, error: `Failed to launch PowerShell: ${e.message || e}` };
    }
    const logPath = _setupLogPath();
    setTimeout(() => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('install-deps-result', {
          success: true,
          message: `Setup launched. Log: ${logPath}`,
          logPath,
        });
      }
    }, 800);
    return { ok: true, logPath };
  });

  ipcMain.handle('open-setup-log', async () => {
    const logPath = _setupLogPath();
    if (!fs.existsSync(logPath)) {
      return { ok: false, error: `Setup log not found: ${logPath}`, logPath };
    }
    try {
      const err = await shell.openPath(logPath);
      if (err) return { ok: false, error: err, logPath };
      return { ok: true, logPath };
    } catch (e) {
      return { ok: false, error: e.message || String(e), logPath };
    }
  });
});

app.on('window-all-closed', () => {
  stopBot();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  try {
    userInitiatedStop = true;
    stopWatchdog();
    stopSupportTunnel();
    if (periodicRestartTimer) { clearInterval(periodicRestartTimer); periodicRestartTimer = null; }
    if (botProcess && botProcess.pid) killTreeWin(botProcess.pid);
    cleanupOrphanCamoufox();
    _telegramNotifyFromMain('🔴 bacopy GUI stopped');
  } catch (_) {}
});
