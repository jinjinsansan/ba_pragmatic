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
let activeBotConfigSignature = '';

// ── ログローテーション ───────────────────────────────────────────────
// engine_cli_capture.log / main_cli_capture.log は appendFileSync の追記専用で
// 上限が無く、放置すると数GBまで肥大する。bafather では 2.29GB まで膨れ、巨大
// ファイルへの追記+Defender スキャンで I/O が詰まり、エンジンの stdout 書込が
// ブロック→決済/追従のメインループが一瞬止まる回帰を起こした。
// 一定サイズを超えたら .1 へ退避(1世代だけ保持)し、新規ファイルで追記を続ける。
const CAPTURE_LOG_MAX_BYTES = 50 * 1024 * 1024; // 50MB
let _engineCaptureWrites = 0;
let _mainCaptureWrites = 0;
function _rotateIfTooBig(filePath, maxBytes) {
  try {
    const st = fs.statSync(filePath);
    if (st.size > maxBytes) {
      const bak = filePath + '.1';
      try { fs.rmSync(bak, { force: true }); } catch (_) {}
      // ロック中(Defender スキャン等)は rename が失敗するが、その場合は例外を
      // 飲んで次回チェック時に再試行する(追記は継続=最悪でも一時的な肥大のみ)。
      fs.renameSync(filePath, bak);
    }
  } catch (_) {}
}

function _mainLogPath() {
  try { return path.join(app.getPath('userData'), 'logs', 'main_cli_capture.log'); }
  catch (_) { return path.join(os.tmpdir(), 'bacopy-main-cli-capture.log'); }
}
function _appendMainCapture(level, args) {
  try {
    const p = _mainLogPath();
    fs.mkdirSync(path.dirname(p), { recursive: true });
    // 起動直後(カウンタ0)と以後64回ごとにサイズ確認→上限超なら退避。
    if ((_mainCaptureWrites++ & 63) === 0) _rotateIfTooBig(p, CAPTURE_LOG_MAX_BYTES);
    const msg = (args || []).map((v) => {
      if (typeof v === 'string') return v;
      try { return JSON.stringify(v); } catch (_) { return String(v); }
    }).join(' ');
    fs.appendFileSync(p, `${new Date().toISOString()} [${level}] ${msg}\n`, 'utf8');
  } catch (_) {}
}
for (const level of ['log', 'warn', 'error']) {
  const orig = console[level].bind(console);
  console[level] = (...args) => {
    _appendMainCapture(level, args);
    orig(...args);
  };
}

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
function listProcessesByImage(imageName) {
  if (process.platform !== 'win32' || !imageName) return [];
  try {
    const out = execSync(`tasklist /FI "IMAGENAME eq ${imageName}" /FO CSV /NH`, { encoding: 'utf-8' });
    return out.split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const cols = line.match(/("([^"]|"")*"|[^,]+)/g) || [];
        const pidText = (cols[1] || '').replace(/^"|"$/g, '').trim();
        return {
          pid: parseInt(pidText, 10),
          commandLine: '',
        };
      })
      .filter((p) => Number.isFinite(p.pid) && p.pid > 0);
  } catch (_) {
    return [];
  }
}
function killPidWin(pid) {
  if (!pid) return;
  try {
    execSync(`taskkill /F /T /PID ${pid}`, { stdio: 'ignore' });
    console.log(`[Main] killPidWin pid=${pid}`);
  } catch (_) {}
}
function waitNoProcessByImage(imageName, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (listProcessesByImage(imageName).length === 0) return true;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 250);
  }
  return listProcessesByImage(imageName).length === 0;
}
function enforceSingleEngineProcess(keepPid) {
  if (process.platform !== 'win32') return;
  const procs = listProcessesByImage('bacopy_engine.exe');
  for (const p of procs) {
    if (p.pid !== keepPid) {
      console.warn(`[Main] duplicate bacopy_engine.exe detected pid=${p.pid}; keeping pid=${keepPid}`);
      killPidWin(p.pid);
    }
  }
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

function cleanupStaleProfileLocks(profileDir) {

  if (!profileDir) return;
  const lockNames = ['parent.lock', 'lock', '.parentlock', 'lockfile'];
  for (const name of lockNames) {
    const p = path.join(profileDir, name);
    try {
      if (fs.existsSync(p)) {
        fs.unlinkSync(p);
        console.log(`[Main] removed stale profile lock: ${p}`);
      }
    } catch (e) {
      console.warn(`[Main] cleanupStaleProfileLocks failed for ${p}: ${e && e.message}`);
    }
  }
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
let _botGeneration = 0;
const MAX_AUTO_RESTARTS = 10;
const AUTO_RESTART_DELAY = 5000;
const STABLE_RUN_THRESHOLD = 5 * 60 * 1000;

let periodicRestartTimer = null;

function _periodicRestartHours() {
  const v = parseFloat(
    (process.env.BACOPY_PERIODIC_RESTART_HOURS || '').trim() ||
    (loadDotEnv().BACOPY_PERIODIC_RESTART_HOURS || '').trim() ||
    '6'  // 2026-06-23: 2.5h+Chromeリフレッシュは「再起動毎の48卓再センタリングで光り遅延」
         // を招いた(bafather実証)ので 6h・エンジンのみ再起動に戻した。Chrome膨張対策は別途。
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
      // 定期再起動は必ず resume=true で再spawnし SEQ を温存する。
      // (resume=false だと main が状態ファイルを削除し engine に --reset を渡すため、
      //  毎回 SEQ が消えてしまう。これが「勝手にリセット」の原因だった。)
      const cfg = Object.assign({}, lastStartConfig, { resume: true });
      const generation = ++_botGeneration;
      userInitiatedStop = false;

      const pid = botProcess.pid;
      if (process.platform === 'win32' && pid) killTreeWin(pid);
      try { botProcess.kill(); } catch (_) {}
      botProcess = null;

      // 2026-06-23: 賭けChrome(:9222)の kill(自動リフレッシュ)は撤去。再起動毎の
      // 48卓再センタリングで「決済→光り」が遅延する回帰を招いたため、エンジンのみ再起動に戻す。
      // (killCdpChrome 関数は将来のより穏当なリフレッシュ用に残置・未使用)

      setTimeout(() => {
        if (!botProcess && cfg && !_botSpawning) {
          try { _doStartBot && _doStartBot(cfg, generation); } catch (e) { console.warn('[periodic-restart] respawn err:', e && e.message); }
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
      stdio: ['pipe', 'pipe', 'pipe'],
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

// ── CDP Chrome (port 9222) one-click launcher ───────────────────────────────
// The distributed dual-line manual-assist build attaches the engine to a Chrome
// running with --remote-debugging-port=9222. We launch that Chrome here so the
// user only double-clicks the GUI icon (no separate task/script). No-op when a
// CDP endpoint is already listening on the port (e.g. an existing cdp_chrome
// task on the admin box) — it never double-binds the port.
function _cdpPortFromUrl(u) {
  try { const m = String(u || '').match(/:(\d{2,5})(?:\/|$)/); return m ? parseInt(m[1], 10) : 9222; } catch (_) { return 9222; }
}
function isCdpUp(port) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    try {
      const http = require('http');
      const req = http.get({ host: '127.0.0.1', port, path: '/json/version', timeout: 1500 }, (res) => {
        res.resume();
        finish(res.statusCode === 200);
      });
      req.on('error', () => finish(false));
      req.on('timeout', () => { try { req.destroy(); } catch (_) {} finish(false); });
    } catch (_) { finish(false); }
  });
}
// betting Chrome(CDP:port)に新規タブで url を開かせる。Chrome 111+ は PUT /json/new。
// 既存タブのナビゲーションでなく新タブを開く方式(ログイン中の他タブを潰さない)。
function openUrlInCdpChrome(port, url) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    try {
      const http = require('http');
      const req = http.request(
        { host: '127.0.0.1', port, path: '/json/new?' + String(url || ''), method: 'PUT', timeout: 4000 },
        (res) => {
          let body = '';
          res.on('data', (c) => { body += c; });
          res.on('end', () => finish({ ok: res.statusCode === 200, status: res.statusCode, body: body.slice(0, 200) }));
        }
      );
      req.on('error', (e) => finish({ ok: false, error: String(e && e.message || e) }));
      req.on('timeout', () => { try { req.destroy(); } catch (_) {} finish({ ok: false, error: 'timeout' }); });
      req.end();
    } catch (e) { finish({ ok: false, error: String(e && e.message || e) }); }
  });
}
function findChromeExe() {
  const cands = [
    process.env.BACOPY_CHROME_EXE,
    path.join(process.env['ProgramFiles'] || 'C:\\Program Files', 'Google', 'Chrome', 'Application', 'chrome.exe'),
    path.join(process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env['LOCALAPPDATA'] ? path.join(process.env['LOCALAPPDATA'], 'Google', 'Chrome', 'Application', 'chrome.exe') : '',
  ];
  for (const c of cands) { try { if (c && fs.existsSync(c)) return c; } catch (_) {} }
  return '';
}
let _cdpChromeLaunching = false;
async function ensureCdpChrome(envFile) {
  if (_cdpChromeLaunching) return false;
  _cdpChromeLaunching = true;
  try {
    const env = envFile || {};
    const cdpUrl = String(env.BACOPY_CHROME_CDP_URL || process.env.BACOPY_CHROME_CDP_URL || 'http://127.0.0.1:9222');
    const port = _cdpPortFromUrl(cdpUrl);
    if (await isCdpUp(port)) { console.log('[cdp-chrome] CDP already up on ' + port + ' — skip launch'); return true; }
    const exe = findChromeExe();
    if (!exe) {
      console.warn('[cdp-chrome] chrome.exe not found');
      try { sendToRenderer('agent-message', { type: 'log', message: '[起動] Google Chrome が見つかりません。インストールしてください。' }); } catch (_) {}
      return false;
    }
    const profileDir = String(
      env.BACOPY_CHROME_PROFILE_DIR || process.env.BACOPY_CHROME_PROFILE_DIR
      || path.join(app.getPath('userData'), 'cdp_chrome_profile')
    );
    try { fs.mkdirSync(profileDir, { recursive: true }); } catch (_) {}
    // プラットフォーム別の初期URL。hh88 はカジノのログインページを開く(以後ユーザーが
    // 手動で Pragmatic ライブバカラ→マルチエリアへ遷移する)。Stake は従来どおりロビー直行。
    const _isHh88 = String(env.BACOPY_PLATFORM || process.env.BACOPY_PLATFORM || 'stake').trim().toLowerCase() === 'hh88';
    const lobby = String(
      env.BACOPY_LOBBY_URL || process.env.BACOPY_LOBBY_URL
      || (_isHh88
        ? 'https://www.hh88vip5.com/en_hk/login'
        : 'https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat')
    );
    const args = [
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profileDir}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-session-crashed-bubble',
      '--restore-last-session=false',
      lobby,
    ];
    console.log('[cdp-chrome] launching ' + exe + ' port=' + port + ' profile=' + profileDir);
    const proc = spawn(exe, args, { detached: true, stdio: 'ignore', windowsHide: false });
    proc.unref();
    try { sendToRenderer('agent-message', { type: 'log', message: _isHh88 ? '[起動] CDP Chrome を起動しました（hh88 にログイン→ライブバカラのマルチエリアを開いてください）' : '[起動] CDP Chrome を起動しました（初回は Stake にログインしてください）' }); } catch (_) {}
    for (let i = 0; i < 15; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      if (await isCdpUp(port)) { console.log('[cdp-chrome] CDP up on ' + port); return true; }
    }
    console.warn('[cdp-chrome] CDP did not come up within 15s');
    return false;
  } catch (e) {
    console.warn('[cdp-chrome] ensure failed:', e && e.message);
    return false;
  } finally {
    _cdpChromeLaunching = false;
  }
}

// 賭け用Chrome(:9222・専用profileで起動した個体)だけを kill する。コマンドラインの
// --remote-debugging-port=<port> で識別するので、ユーザーの個人Chromeは残る。
// 定期リフレッシュで膨張(2GB+でセンタリング遅延→NOW取りこぼし)をクリアする用途。
function killCdpChrome(port) {
  if (process.platform !== 'win32') return 0;
  try {
    // ★シングルクォートのみで書く(ダブルクォート禁止)。`powershell -Command "..."` の
    //   外側ダブルクォートと入れ子になると壊れ、クエリが空振りして1個もkillできない
    //   (2026-06-22に実機で発覚)。賭けChromeの「メイン+全子プロセス」を、専用profile名
    //   または debug-port で一致させて確実にkillする(個人Chromeは別profileなので無傷)。
    const ps =
      `Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and ` +
      `($_.CommandLine -like '*cdp_chrome_profile*' -or $_.CommandLine -like '*remote-debugging-port=${port}*') } | ` +
      `ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }; ` +
      `(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*remote-debugging-port=${port}*' } | Measure-Object).Count`;
    const out = execSync(`powershell -NoProfile -Command "${ps}"`, { encoding: 'utf8', timeout: 20000 }).trim();
    console.log('[cdp-chrome] refresh: killed betting Chrome (port ' + port + '); remaining port-procs=' + out);
    return 1;
  } catch (e) { console.warn('[cdp-chrome] kill failed:', e && e.message); return 0; }
}

// ── 賭けChrome(:9222) 膨張モニタ ─────────────────────────────────────────────
// 賭けChromeは長時間で膨張(2GB+でセンタリング遅延→NOW取りこぼし/黄色枠握り・3.9GBで
// 詰まり実測)し、GUI/エンジン再起動では消えない(別プロセス)。OS再起動 or :9222 kill で
// しか消えない。そこで「合計RAM」を読み取って renderer にバッジ表示し、再起動の目安を
// 一目で分かるようにする。★kill はせず読むだけ=決済/賭け/光り経路に一切非接触。
// killCdpChrome と同じ CommandLine 一致で賭けChromeだけを集計(個人Chromeは別profileで無視)。
function getCdpChromeRam(port) {
  if (process.platform !== 'win32') return null;
  try {
    const ps =
      `$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and ` +
      `($_.CommandLine -like '*cdp_chrome_profile*' -or $_.CommandLine -like '*remote-debugging-port=${port}*') }; ` +
      `$mb = [math]::Round((($p | Measure-Object WorkingSetSize -Sum).Sum)/1MB); ` +
      `$cnt = @($p).Count; ` +
      `$old = ($p | Sort-Object CreationDate | Select-Object -First 1).CreationDate; ` +
      `$up = if ($old) { [math]::Round(((Get-Date) - $old).TotalMinutes) } else { 0 }; ` +
      `Write-Output ('' + $mb + '|' + $cnt + '|' + $up)`;
    const out = execSync(`powershell -NoProfile -Command "${ps}"`, { encoding: 'utf8', timeout: 15000 }).trim();
    const parts = out.split('|');
    const mb = parseInt(parts[0], 10) || 0;
    const cnt = parseInt(parts[1], 10) || 0;
    const up = parseInt(parts[2], 10) || 0;
    if (cnt <= 0 || mb <= 0) return null;
    return { mb: mb, count: cnt, uptimeMin: up };
  } catch (e) { console.warn('[chrome-bloat] query failed:', e && e.message); return null; }
}

// 60秒ごとに賭けChromeのRAMを読んで renderer に送る(読み取りのみ)。
// env BACOPY_CHROME_BLOAT_MONITOR=0 で無効・BACOPY_CHROME_BLOAT_SEC で間隔変更。
let _bloatTimer = null;
function startChromeBloatMonitor() {
  if (_bloatTimer) return;
  if (String(process.env.BACOPY_CHROME_BLOAT_MONITOR || '1').trim() === '0') return;
  const sec = Math.max(20, parseInt(process.env.BACOPY_CHROME_BLOAT_SEC || '60', 10) || 60);
  const poll = () => {
    try {
      const env = loadDotEnv();
      const port = _cdpPortFromUrl(env.BACOPY_CHROME_CDP_URL || process.env.BACOPY_CHROME_CDP_URL || 'http://127.0.0.1:9222');
      const r = getCdpChromeRam(port);
      if (r) sendToRenderer('chrome-bloat', r);
    } catch (_) {}
  };
  _bloatTimer = setInterval(poll, sec * 1000);
  setTimeout(poll, 8000);  // 初回は起動が落ち着いた頃
  console.log('[chrome-bloat] monitor started, interval=' + sec + 's');
}
function stopChromeBloatMonitor() {
  if (_bloatTimer) { clearInterval(_bloatTimer); _bloatTimer = null; }
}

// ── CDP Chrome watchdog (auto-recover :9222 death) ──────────────────────────
// 受け子GUIが「気づいたら停止」する最頻原因 = betting用デバッグChrome(:9222)が
// 死ぬ → engine connect_over_cdp ECONNREFUSED → fatal → 自動再起動もChrome不在で
// 連続失敗 → MAX_AUTO_RESTARTS到達で放置(実測8時間ダウン 2026-06-13 user02/恭平くん)。
// 本ウォッチドッグは bot稼働中に :9222 を定期死活監視し、落ちていれば Chrome を
// 自動再起動し、エンジンが落ちたまま諦めていれば再武装する(8時間ダウン→約1分で自動復帰)。
// env BACOPY_CDP_WATCHDOG=0 で無効化・BACOPY_CDP_WATCHDOG_SEC で間隔変更。
let _cdpWatchdogTimer = null;
let _cdpWatchdogBusy = false;
let _lastCdpNotifyAt = 0;
function _cdpNotifyThrottled(text) {
  const now = Date.now();
  if (now - _lastCdpNotifyAt < 5 * 60 * 1000) return;  // Telegram通知は最大5分に1回
  _lastCdpNotifyAt = now;
  try { _telegramNotifyFromMain(text); } catch (_) {}
}
function startCdpWatchdog() {
  if (_cdpWatchdogTimer) return;
  const sec = Math.max(20, parseInt(process.env.BACOPY_CDP_WATCHDOG_SEC || '45', 10) || 45);
  console.log('[cdp-watchdog] started, interval=' + sec + 's');
  _cdpWatchdogTimer = setInterval(async () => {
    if (_cdpWatchdogBusy) return;
    _cdpWatchdogBusy = true;
    try {
      // bot が動くべき状態(START済・ユーザ停止でない)でのみ作動
      if (userInitiatedStop || !lastStartConfig) return;
      const env = loadDotEnv();
      if (String(env.BACOPY_CDP_WATCHDOG || '1').trim() !== '1') return;
      const port = _cdpPortFromUrl(env.BACOPY_CHROME_CDP_URL || process.env.BACOPY_CHROME_CDP_URL || 'http://127.0.0.1:9222');
      if (!(await isCdpUp(port))) {
        console.warn('[cdp-watchdog] :' + port + ' DOWN — relaunching Chrome');
        _cdpNotifyThrottled('🩹 CDP Chrome(:' + port + ')が落ちていたため自動再起動しました');
        await ensureCdpChrome(env);
      }
      // エンジンが止まっているが本来動くべき(fast auto-restart が諦めた等)→
      // Chrome が生きていれば再武装(fast restart timer 進行中は触らない)
      if (!botProcess && !_botSpawning && !userInitiatedStop && lastStartConfig && !autoRestartTimer) {
        if (await isCdpUp(port)) {
          console.warn('[cdp-watchdog] engine down but should be running — restarting (resume)');
          _cdpNotifyThrottled('🔄 エンジンが停止していたため自動復帰しました(ウォッチドッグ)');
          autoRestartCount = 0;
          try {
            _doStartBot(Object.assign({}, lastStartConfig, { resume: true }), _botGeneration);
          } catch (e) { console.warn('[cdp-watchdog] restart err:', e && e.message); }
        }
      }
    } catch (e) {
      console.warn('[cdp-watchdog] error:', e && e.message);
    } finally {
      _cdpWatchdogBusy = false;
    }
  }, sec * 1000);
}
function stopCdpWatchdog() {
  if (_cdpWatchdogTimer) {
    clearInterval(_cdpWatchdogTimer);
    _cdpWatchdogTimer = null;
    console.log('[cdp-watchdog] stopped');
  }
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
function _appendEngineCapture(text) {
  try {
    const logDir = path.join(app.getPath('userData'), 'logs');
    fs.mkdirSync(logDir, { recursive: true });
    const p = path.join(logDir, 'engine_cli_capture.log');
    // 起動直後(カウンタ0)と以後64回ごとにサイズ確認→上限超なら退避。
    // 起動時に既存の巨大ログ(例: 2.29GB)があれば最初の書込で .1 へ追い出される。
    if ((_engineCaptureWrites++ & 63) === 0) _rotateIfTooBig(p, CAPTURE_LOG_MAX_BYTES);
    fs.appendFileSync(
      p,
      `${new Date().toISOString()} ${String(text || '')}`,
      'utf8'
    );
  } catch (_) {}
}

function _emitStdoutLines(chunk) {
  const text = (_stdoutRemainder + String(chunk || '')).replace(/\r/g, '');
  _appendEngineCapture(text);
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
  _appendEngineCapture(text);
  sendToRenderer('agent-log', text);
}

function buildSpawnSpec(config) {
  const engine = resolveEngine();
  const envFile = loadDotEnv();
  const childEnv = { ...process.env, ...envFile, PYTHONIOENCODING: 'utf-8' };

  const modeName = config && config.mode;
  const isDualLineAssist = modeName === 'dual_line_assist' || modeName === 'dual_line_manual';
  const isDualLineAuto = modeName === 'dual_line' || modeName === 'dual_line_auto';
  const isDualLine = isDualLineAssist || isDualLineAuto;

  // dual-line モード: dev 時はスクリプトパスを上書き
  if (isDualLine && engine.mode === 'dev') {
    engine.baseArgs = ['-X', 'utf8', '-u', path.join(engine.cwd, 'dual_line_pragmatic_bot.py')];
  }

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
    args.push(isDualLine ? 'dual-line' : 'executor-pragmatic');
  }

  // chipBase は dual-line ブロックより前に宣言して TDZ を回避
  const chipBase = (config && typeof config.chip_base === 'number') ? config.chip_base : 1;

  // dual-line 専用 args
  if (isDualLine) {
    // GUI live runs must place real bets. Diagnostic-only is opt-in via .env/launcher.
    if (!Object.prototype.hasOwnProperty.call(envFile, 'BACOPY_MULTI_DIAGNOSTIC_ONLY')) {
      childEnv.BACOPY_MULTI_DIAGNOSTIC_ONLY = '0';
    }
    // 受け子は常に実BET(--live)。GUIの LIVE Mode チェックは撤去した(チェック忘れで
    // 賭けない事故防止)。ドライランは管理者用 BACOPY_DUAL_DRYRUN=1 の時だけ。
    {
      const _dry = String(childEnv.BACOPY_DUAL_DRYRUN || '').trim().toLowerCase();
      const _forceDry = (_dry === '1' || _dry === 'true' || _dry === 'on' || _dry === 'yes');
      if (!_forceDry) args.push('--live');
    }
    if (isDualLineAssist || isDualLineAuto) {
      // 両モードとも manual-assist エンジンを使う(オーバーレイ + NOW)。
      // BET MODE プルダウンが唯一の切替: アシスト(手動) / オート(全自動WS BET)。
      args.push('--manual-assist');
      if (isDualLineAuto) {
        // デュアルラインオート: NOW→WS自動BET(チップ/ローディング不要)→dga勝者で
        // 自動決済→SEQ/ダランベール自動進行。auto-click + WS transport を強制。
        childEnv.BACOPY_MANUAL_ASSIST_AUTO_CLICK = '1';
        childEnv.BACOPY_MANUAL_NO_AUTOCLICK = '0';
        childEnv.BACOPY_MULTI_BET_TRANSPORT = 'ws';
        childEnv.BACOPY_ALLOW_WS_BET_TRANSPORT = '1';
        childEnv.BACOPY_ENABLE_WS_REAL_BET = '1';
        // オート追従: 勝った時だけ同卓追従(大路 telecho=逆張り / dragon=順張り)。
        if (config && config.dual_follow) childEnv.BACOPY_DUAL_FOLLOW = '1';
      } else {
        // デュアルラインアシスト: 人間が手動クリック + WIN/LOSE で進行。自動クリック
        // はしない(NO_AUTOCLICK=1)。チップ事前選択は$1基準固定(過大BET事故防止)。
        childEnv.BACOPY_MANUAL_NO_AUTOCLICK = '1';
        childEnv.BACOPY_MULTI_BET_TRANSPORT = 'click';
        childEnv.BACOPY_MANUAL_CHIP_BASE = String((config && config.manual_chip_base) || '1');
      }
    }
    // パターンモード v3(6) / v4(10)。エンジンは BACOPY_DUAL_MODE を読む(既定v3)。
    const dualMode = String((config && config.dual_mode) || 'v3').toLowerCase();
    childEnv.BACOPY_DUAL_MODE = (dualMode === 'v4') ? 'v4' : 'v3';

    // 安全モード初期値: ON のとき当日累計勝率>=50%の系統だけ BET(エンジンが両系統を
    // 受けてゲート)。起動後は GUI プルダウン→stdin で即時 ON/OFF 切替可。既定 OFF。
    childEnv.BACOPY_SAFETY_MODE = (config && config.safety_mode) ? '1' : '0';

    // SEQ 型(階段の上げ方): attack(現行=既定) / balance(CAND_A) / defense(CAND_B)。
    // エンジンは BACOPY_SEQ_SHAPE を読む。攻撃型は従来配列のまま(ゼロ回帰)。
    const seqShape = String((config && config.seq_shape) || 'attack').toLowerCase();
    childEnv.BACOPY_SEQ_SHAPE = (seqShape === 'balance' || seqShape === 'defense') ? seqShape : 'attack';

    // Kelly(比例)モード: 残高基準(フォールバック元本)。ライブ残高が取れれば engine が上書き。
    // 型(攻撃/バランス/守備)は BACOPY_SEQ_SHAPE を流用(上で設定済み)。
    if (config && config.kelly_bankroll) {
      const kb = parseFloat(config.kelly_bankroll);
      if (kb > 0) childEnv.BACOPY_KELLY_BANKROLL = String(kb);
    }

    // ── プラットフォーム切替 (Stake / hh88) ──────────────────────────────
    // 排他: GUI は常に片側のみ(オーナー指示・Pragmatic の異変検知回避)。既定 stake で
    // 従来挙動を一切変えない。hh88 は同一 Pragmatic バックエンドだが CDP注入WSブリッジ
    // 方式(engine 側 IS_HH88)なので、click では着弾しない → WS transport を強制する。
    const platform = String((config && config.platform) || childEnv.BACOPY_PLATFORM || 'stake').trim().toLowerCase();
    childEnv.BACOPY_PLATFORM = (platform === 'hh88') ? 'hh88' : 'stake';
    if (childEnv.BACOPY_PLATFORM === 'hh88') {
      // hh88 は WS BET 専用(CDP注入ブリッジ)。assist/auto に関わらず ws を強制し、
      // 実BET送信を有効化する。no-reload は engine 既定で hh88=ON(ここでは触らない)。
      childEnv.BACOPY_MULTI_BET_TRANSPORT = 'ws';
      childEnv.BACOPY_ALLOW_WS_BET_TRANSPORT = '1';
      childEnv.BACOPY_ENABLE_WS_REAL_BET = '1';
      childEnv.BACOPY_MANUAL_ASSIST_AUTO_CLICK = '1';
      childEnv.BACOPY_MANUAL_NO_AUTOCLICK = '0';
      // ★uId ピン留め: 上の方(L877)で BACOPY_USER_ID は Supabase の UUID で上書きされる。
      //   engine は WS lpbet に Pragmatic の ppc-id が必要で、UUID は弾く→毎回「手動BETで
      //   _own_user_id を学習」が必要になっていた。hh88 の ppc-id を .env(BACOPY_HH88_UID)で
      //   ピン留めできるようにし、ここで BACOPY_USER_ID を ppc 値に差し替える(UUIDより後勝ち)。
      //   ppc 形式のみ採用(UUID/空は無視=従来どおり _own_user_id 学習にフォールバック)。
      const _hkUid = String(childEnv.BACOPY_HH88_UID || envFile.BACOPY_HH88_UID || '').trim();
      if (_hkUid && !/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-/.test(_hkUid)) {
        childEnv.BACOPY_USER_ID = _hkUid;
        console.log('[AUTO-PROBE] hh88: pinned BACOPY_USER_ID to ppc uId from BACOPY_HH88_UID');
      }
    }
    if (config && config.no_v2_filter) args.push('--no-v2-filter');
    if (config && config.money_mode) args.push('--money-mode', String(config.money_mode));
    // money_unit 未設定時は chip_base にフォールバック
    args.push('--money-unit', String((config && config.money_unit) ? config.money_unit : chipBase));
    // SEQ セット長 (7=標準 / 5=5ターン制)
    if (config && Number(config.seq_turns) === 5) args.push('--seq-turns', '5');
    if (config && config.profit_target) args.push('--profit-target', String(config.profit_target));
    if (config && config.loss_cut) args.push('--loss-cut', String(config.loss_cut));
    if (config && config.on_limit) args.push('--on-limit', String(config.on_limit));
    // 新規リセットスタート: engine 側 state ファイルも削除する
    if (config && config.resume === false) args.push('--reset');

    const browserMode = String(
      childEnv.BACOPY_BROWSER ||
      childEnv.BACOPY_DUAL_LINE_BROWSER ||
      ''
    ).trim().toLowerCase();
    if (browserMode === 'chrome_attach' || browserMode === 'chrome-cdp' || browserMode === 'cdp') {
      childEnv.BACOPY_BROWSER = 'chrome_attach';
      const cdpUrl = String(
        childEnv.BACOPY_CHROME_CDP_URL ||
        childEnv.BACOPY_CHROME_DEBUG_URL ||
        'http://127.0.0.1:9222'
      ).trim();
      childEnv.BACOPY_CHROME_CDP_URL = cdpUrl;
      args.push('--browser', 'chrome_attach', '--chrome-cdp-url', cdpUrl);
    }
  }

  if (config && config.allow_switch_table) args.push('--allow-switch-table');
  if (config && config.allow_banker) args.push('--allow-banker');
  if (config && config.allow_tie) args.push('--allow-tie');
  if (config && config.assume_bc_012) args.push('--assume-bc-012');
  if (config && config.bet_mode) args.push('--bet-mode', String(config.bet_mode));
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

  // dual-line は手動でテーブルを選ぶため headless を禁止
  if (config && config.headless && !isDualLine) args.push('--headless');



  const profileDir = path.join(app.getPath('userData'), 'profiles', 'executor_pragmatic');
  try { fs.mkdirSync(profileDir, { recursive: true }); } catch (_) {}
  args.push('--profile-dir', profileDir);

  if (isDualLine) {
    console.log(
      '[AUTO-PROBE] spawn dual-line ' +
      `mode=${modeName || '-'} assist=${isDualLineAssist} auto=${isDualLineAuto} ` +
      `follow=${!!(config && config.dual_follow)} BACOPY_DUAL_FOLLOW=${childEnv.BACOPY_DUAL_FOLLOW || '-'} ` +
      `live=${!!(config && config.live)} browser=${childEnv.BACOPY_BROWSER || childEnv.BACOPY_DUAL_LINE_BROWSER || 'camoufox'} ` +
      `cdp=${childEnv.BACOPY_CHROME_CDP_URL || childEnv.BACOPY_CHROME_DEBUG_URL || '-'} ` +
      `engine=${engine.mode} exe=${engine.exe} args=${JSON.stringify(args)}`
    );
  }

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

async function startBot(config) {
  const cfg = config || {};
  const verr = _validateConfigForSpawn(cfg);
  if (verr) {
    sendToRenderer('agent-message', { type: 'error', message: verr });
    return;
  }
  const cfgSignature = JSON.stringify(cfg);
  if (botProcess && botProcess.killed) {
    console.log('[Main] clearing stale killed botProcess before start');
    botProcess = null;
    activeBotConfigSignature = '';
  }
  if (botProcess && activeBotConfigSignature === cfgSignature) {
    sendToRenderer('agent-message', { type: 'log', message: '[spawn] ignored duplicate start request for active config' });
    return;
  }
  // ── START時は :9222 Chrome を先に起動し、bind を待ってからエンジンを spawn する。
  // これをしないと engine が先に立ち上がり Chrome 未起動 → connect_over_cdp
  // ECONNREFUSED で数回即死し、起動直後に「クラッシュ→自動復帰」が見える
  // (2026-06-13 user05/梶原さん)。ensureCdpChrome は :9222 が既に上なら即スキップ。
  try {
    const _env = loadDotEnv();
    // UI のプラットフォーム選択(cfg.platform)を Chrome 起動 env に反映し、hh88 なら
    // hh88 ログインページを開かせる(.env に BACOPY_PLATFORM がある配布形態も尊重)。
    if (cfg && cfg.platform && !_env.BACOPY_PLATFORM) _env.BACOPY_PLATFORM = String(cfg.platform);
    const _port = _cdpPortFromUrl(_env.BACOPY_CHROME_CDP_URL || process.env.BACOPY_CHROME_CDP_URL || 'http://127.0.0.1:9222');
    if (!(await isCdpUp(_port))) {
      sendToRenderer('agent-message', { type: 'log', message: `[起動] CDP Chrome(:${_port}) を起動し接続を待っています…` });
      await ensureCdpChrome(_env);
    }
  } catch (e) { console.warn('[startBot] cdp ensure err:', e && e.message); }
  const generation = ++_botGeneration;
  if (autoRestartTimer) {
    clearTimeout(autoRestartTimer);
    autoRestartTimer = null;
  }

  if (botProcess) {


    const old = botProcess;
    const oldPid = old && old.pid;
    botProcess = null;
    try { old.removeAllListeners('exit'); } catch (_) {}
    try { old.removeAllListeners('error'); } catch (_) {}
    try { old.stdout?.removeAllListeners?.('data'); } catch (_) {}
    try { old.stderr?.removeAllListeners?.('data'); } catch (_) {}
    let started = false;
    old.once('exit', () => {
      if (started || generation !== _botGeneration) return;
      started = true;
      _doStartBot(cfg, generation);
    });
    try {
      if (process.platform === 'win32' && oldPid) killTreeWin(oldPid);
      else old.kill();
    } catch (_) {
      if (!started && generation === _botGeneration) {
        started = true;
        _doStartBot(cfg, generation);
      }
    }
    setTimeout(() => {
      if (!started && generation === _botGeneration) {
        started = true;
        console.log('[Main] old engine exit not observed; starting replacement');
        _doStartBot(cfg, generation);
      }
    }, 2500);
    return;
  }
  _doStartBot(cfg, generation);
}

function _doStartBot(config, generation = _botGeneration) {
  if (generation !== _botGeneration) {
    console.log(`[Main] _doStartBot: obsolete generation ${generation}, current=${_botGeneration}`);
    return;
  }
  if (_botSpawning) {
    console.log('[Main] _doStartBot: already spawning, skipped');
    return;
  }
  _botSpawning = true;




  try { cleanupOrphanCamoufox(); } catch (_) {}
  // Prevent orphan engine duplication (can happen after crash/restart races).
  try {
    killAllByImage('bacopy_engine.exe');
    if (!waitNoProcessByImage('bacopy_engine.exe', 12000)) {
      sendToRenderer('agent-message', { type: 'log', message: '[spawn] waiting for previous engine failed; start aborted to prevent duplicate engine' });
      _botSpawning = false;
      return;
    }
  } catch (_) {}
  // After confirming no engine/camoufox processes are alive, remove any
  // Firefox/Camoufox profile lock files left behind by a hard kill so the
  // next browser launch is not blocked by a stale parent.lock.
  try {
    const profileDir = path.join(app.getPath('userData'), 'profiles', 'executor_pragmatic');
    cleanupStaleProfileLocks(profileDir);
  } catch (e) {
    console.warn('[Main] profile lock cleanup failed:', e && e.message);
  }







  if (config && config.resume === false) {
    try {
      const profileDir = path.join(app.getPath('userData'), 'profiles', 'executor_pragmatic');
      const resetFiles = [
        'seq7_state.json',
        'last_table.json',
        'dual_line_pragmatic_state.json',
        'dual_line_money_state.json',
      ];
      for (const name of resetFiles) {
        const p = path.join(profileDir, name);
        if (fs.existsSync(p)) {
          fs.unlinkSync(p);
          console.log(`[Main] NEW SESSION — removed ${name}`);
        }
      }
    } catch (e) {
      console.warn('[Main] new session state reset failed:', e.message);
    }
  }
  if (autoRestartTimer) {
    clearTimeout(autoRestartTimer);
    autoRestartTimer = null;
  }

  const cfg = config || {};
  const cfgSignature = JSON.stringify(cfg);
  const verr = _validateConfigForSpawn(cfg);
  if (verr) {
    _botSpawning = false;
    sendToRenderer('agent-message', { type: 'error', message: verr });
    return;
  }
  const spec = buildSpawnSpec(cfg);



  lastStartConfig = cfg;
  activeBotConfigSignature = cfgSignature;
  userInitiatedStop = false;
  lastSpawnAt = Date.now();

  sendToRenderer('agent-message', { type: 'log', message: `[spawn] exe=${spec.exe} cwd=${spec.cwd} args=${JSON.stringify(spec.args)}` });

  try {
    botProcess = spawn(spec.exe, spec.args, {
      cwd: spec.cwd,
      env: spec.env,
      // stdin MUST be a writable pipe: the GUI delivers manual-assist commands
      // (WIN/LOSE result, set_dual_mode) by writing JSON lines to botProcess.stdin
      // (see the 'manual-assist-command' IPC handler). With stdin:'ignore' the
      // engine never received these → WIN/LOSE did nothing and SEQ never advanced.
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
  } catch (err) {
    _botSpawning = false;
    botProcess = null;
    activeBotConfigSignature = '';
    const msg = err && err.message ? err.message : String(err);
    console.error('[Main] spawn failed:', msg);
    sendToRenderer('agent-message', { type: 'error', message: `spawn failed: ${msg}` });
    throw err;
  }
  _botSpawning = false;

  if (process.platform === 'win32') {
    setTimeout(() => {
      try {
        if (botProcess && botProcess.pid) enforceSingleEngineProcess(botProcess.pid);
      } catch (_) {}
    }, 1500);
  }





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
  startCdpWatchdog();
  startChromeBloatMonitor();

  const thisProcess = botProcess;
  const thisSpawnAt = lastSpawnAt;
  const thisGeneration = generation;

  botProcess.on('exit', (code) => {


    const rem = _stdoutRemainder.trim();
    if (rem) sendToRenderer('agent-message', { type: 'log', message: rem });
    _stdoutRemainder = '';

    if (botProcess === thisProcess && thisGeneration === _botGeneration) {
      sendToRenderer('agent-message', { type: 'stopped', code });
      botProcess = null;
      activeBotConfigSignature = '';



      const ranDuration = Date.now() - thisSpawnAt;
      if (!userInitiatedStop && lastStartConfig) {
        const lastMode = lastStartConfig && lastStartConfig.mode;
        const isDualLine = lastMode === 'dual_line' || lastMode === 'dual_line_auto'
          || lastMode === 'dual_line_assist' || lastMode === 'dual_line_manual';
        const envNow = loadDotEnv();
        // 既定で dual-line のクラッシュ自動復旧を ON にする(従来は '0'=OFF)。
        // SEQ は resume=true + Supabase 復元で温存されるので「リセットされる危険」は解消済み。
        // 既存Chrome(:9222)へ再アタッチする方式のため再起動でブラウザは生き残る。
        // どうしても無効化したい時のみ .env で BACOPY_DUAL_LINE_AUTO_RESTART=0。
        const dualLineAutoRestart = String(envNow.BACOPY_DUAL_LINE_AUTO_RESTART || '1').trim() === '1';
        if (isDualLine && !dualLineAutoRestart) {
          const msg = `⚠️ Dual-line engine stopped (code=${code}); auto-restart is disabled (BACOPY_DUAL_LINE_AUTO_RESTART=0). Press START manually after checking the browser.`;
          console.warn('[Main]', msg);
          sendToRenderer('agent-message', { type: 'log', message: msg });
          autoRestartCount = 0;
          return;
        }
        if (ranDuration > STABLE_RUN_THRESHOLD) {
          console.log(`[Main] Stable run detected (${Math.round(ranDuration/1000)}s) — reset auto-restart counter`);
          autoRestartCount = 0;
        }
        if (autoRestartCount < MAX_AUTO_RESTARTS) {
          autoRestartCount++;
          const msg = `🔄 Auto-restart (${autoRestartCount}/${MAX_AUTO_RESTARTS}) — retry in ${AUTO_RESTART_DELAY/1000}s`;
          console.log('[Main]', msg);
          sendToRenderer('agent-message', { type: 'log', message: msg });
          autoRestartTimer = setTimeout(async () => {
            autoRestartTimer = null;
            if (!botProcess && !userInitiatedStop && lastStartConfig && thisGeneration === _botGeneration) {
              // 再起動前に :9222 Chrome の生存を確認し、死んでいれば先に復活させる。
              // これをしないと Chrome が落ちた時 engine が ECONNREFUSED で何度も即死し、
              // MAX_AUTO_RESTARTS を浪費して放置される(2026-06-13 user02 8時間ダウンの真因)。
              try {
                const _env = loadDotEnv();
                const _port = _cdpPortFromUrl(_env.BACOPY_CHROME_CDP_URL || process.env.BACOPY_CHROME_CDP_URL || 'http://127.0.0.1:9222');
                if (!(await isCdpUp(_port))) {
                  sendToRenderer('agent-message', { type: 'log', message: `🩹 CDP Chrome(:${_port}) down — relaunching before engine restart` });
                  await ensureCdpChrome(_env);
                }
              } catch (e) { console.warn('[auto-restart] cdp ensure err:', e && e.message); }
              if (!botProcess && !userInitiatedStop && lastStartConfig && thisGeneration === _botGeneration) {
                sendToRenderer('agent-message', { type: 'log', message: '🔄 Auto-restart: restarting engine...' });
                // クラッシュ自動復旧も resume=true で再spawn(SEQ温存+Supabase復元)。
                _doStartBot(Object.assign({}, lastStartConfig, { resume: true }), thisGeneration);
              }
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
    _botSpawning = false;
    if (botProcess === thisProcess) {
      botProcess = null;
      activeBotConfigSignature = '';
    }
    sendToRenderer('agent-message', { type: 'error', message: err && err.message ? err.message : String(err) });
  });



  if (autoRestartCount > 0) {
    sendToRenderer('agent-message', { type: 'started' });
  }
}

function stopBot() {
  _botGeneration++;
  _botSpawning = false;
  stopCdpWatchdog();  // ユーザ停止時はCDPウォッチドッグも止める(意図しない自動再起動を防ぐ)
  stopChromeBloatMonitor();
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
  botProcess = null;
  activeBotConfigSignature = '';
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

// ── 毎時勝率(時間帯別)パネル: master /api/hourly-stats を60秒毎に取得して
//    renderer へ転送する。罫線どおりに走る時間帯かの稼働判断材料(Telegram毎時
//    レポートと同じ集計のJSON版・VPS側cronが10分毎更新)。
let hourlyStatsTimer = null;
function startHourlyStatsPoller() {
  if (hourlyStatsTimer) return;
  const poll = () => {
    try {
      const envFile = loadDotEnv();
      const base = String(envFile.BACOPY_API_URL || process.env.BACOPY_API_URL || 'https://master.bafather.uk').replace(/\/$/, '');
      const key = String(envFile.BACOPY_API_KEY || process.env.BACOPY_API_KEY || '').trim();
      if (!key) return;
      const u = new URL(base + '/api/hourly-stats');
      const mod = u.protocol === 'http:' ? require('http') : require('https');
      const req = mod.get(u, { headers: { Authorization: `Bearer ${key}` }, timeout: 10000 }, (res) => {
        let body = '';
        res.on('data', (c) => { body += c; });
        res.on('end', () => {
          try { sendToRenderer('hourly-stats', JSON.parse(body)); } catch (_) {}
        });
      });
      req.on('timeout', () => req.destroy());
      req.on('error', () => {});
    } catch (_) {}
  };
  poll();
  hourlyStatsTimer = setInterval(poll, 60 * 1000);
}

// ── 勝率履歴(FX風チャート用): master /api/winrate-history を5分毎に取得して
//    renderer へ転送する。時間バケット(pattern別 date/hour/n/w)の永続蓄積を
//    VPS cronが貯めており、GUI側で 1H/4H/D/W にリサンプルして描画する。
//    ペイロードが hourly-stats より大きい(過去数週間分)ため低頻度ポーリング。
let winrateHistoryTimer = null;
function startWinrateHistoryPoller() {
  if (winrateHistoryTimer) return;
  const poll = () => {
    try {
      const envFile = loadDotEnv();
      const base = String(envFile.BACOPY_API_URL || process.env.BACOPY_API_URL || 'https://master.bafather.uk').replace(/\/$/, '');
      const key = String(envFile.BACOPY_API_KEY || process.env.BACOPY_API_KEY || '').trim();
      if (!key) return;
      const u = new URL(base + '/api/winrate-history');
      const mod = u.protocol === 'http:' ? require('http') : require('https');
      // Accept-Encoding: identity で proxy 圧縮を抑止(大きめのJSONをそのまま受ける)。
      const req = mod.get(u, { headers: { Authorization: `Bearer ${key}`, 'Accept-Encoding': 'identity' }, timeout: 15000 }, (res) => {
        let body = '';
        res.on('data', (c) => { body += c; });
        res.on('end', () => {
          try { sendToRenderer('winrate-history', JSON.parse(body)); } catch (_) {}
        });
      });
      req.on('timeout', () => req.destroy());
      req.on('error', () => {});
    } catch (_) {}
  };
  // 起動直後は renderer 準備とのレースを避けるため早期リトライを数回挟み、
  // 以後は 60s 間隔(hourly と同等の信頼性)で更新する。
  poll();
  setTimeout(poll, 3000);
  setTimeout(poll, 12000);
  winrateHistoryTimer = setInterval(poll, 60 * 1000);
}

// ── 勝率レンジ(累計勝率): master /api/winrate-trend を 60s 毎に取得して renderer へ。
//    チャンネル(6P/10P/追従込み)と byte一致する累計勝率の時系列+下限/上限。
//    VPS cron(winrate_trend.py)が貯める。表示専用(エンジン/賭け経路に非依存)。
let winrateTrendTimer = null;
function startWinrateTrendPoller() {
  if (winrateTrendTimer) return;
  const poll = () => {
    try {
      const envFile = loadDotEnv();
      const base = String(envFile.BACOPY_API_URL || process.env.BACOPY_API_URL || 'https://master.bafather.uk').replace(/\/$/, '');
      const key = String(envFile.BACOPY_API_KEY || process.env.BACOPY_API_KEY || '').trim();
      if (!key) return;
      const u = new URL(base + '/api/winrate-trend');
      const mod = u.protocol === 'http:' ? require('http') : require('https');
      const req = mod.get(u, { headers: { Authorization: `Bearer ${key}`, 'Accept-Encoding': 'identity' }, timeout: 15000 }, (res) => {
        let body = '';
        res.on('data', (c) => { body += c; });
        res.on('end', () => {
          try { sendToRenderer('winrate-trend', JSON.parse(body)); } catch (_) {}
        });
      });
      req.on('timeout', () => req.destroy());
      req.on('error', () => {});
    } catch (_) {}
  };
  poll();
  setTimeout(poll, 3000);
  setTimeout(poll, 12000);
  winrateTrendTimer = setInterval(poll, 60 * 1000);
}

function createWindow() {
  console.log('[Main] createWindow');
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
  mainWindow.on('closed', () => {
    console.log('[Main] mainWindow closed');
    mainWindow = null;
  });
}

app.whenReady().then(() => {
  console.log('[Main] app ready packaged=' + app.isPackaged + ' resourcesPath=' + process.resourcesPath);

  const startupEnv = loadDotEnv();
  const startupBrowser = String(
    startupEnv.BACOPY_BROWSER ||
    startupEnv.BACOPY_DUAL_LINE_BROWSER ||
    process.env.BACOPY_BROWSER ||
    process.env.BACOPY_DUAL_LINE_BROWSER ||
    ''
  ).trim().toLowerCase();
  if (startupBrowser === 'chrome_attach' || startupBrowser === 'chrome-cdp' || startupBrowser === 'cdp') {
    console.log('[Main] chrome_attach configured; skip camoufox asset restore');
    // One-click: launch the CDP Chrome (port 9222) the engine attaches to.
    // No-op if 9222 is already up (admin box runs it via a task).
    ensureCdpChrome(startupEnv).catch((e) => console.warn('[cdp-chrome] ensure error:', e && e.message));
  } else {
    try { ensureCamoufoxAssets(); } catch (e) { console.warn('[Main] camoufox restore failed:', e.message); }
  }
  try { cleanupOrphanCamoufox(); } catch (e) { console.warn('[Main] startup cleanup failed:', e.message); }

  createWindow();

  startHourlyStatsPoller();
  startWinrateHistoryPoller();
  startWinrateTrendPoller();

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

  // betting Chrome(:9222 等)に新規タブで URL を開く(hh88 ワンクリック起動)。
  // Chrome が未起動なら先に ensureCdpChrome で起こしてから開く。
  ipcMain.handle('open-betting-url', async (_evt, url) => {
    const target = String(url || '').trim();
    if (!target) return { ok: false, error: 'no_url' };
    try {
      const env = loadDotEnv();
      const port = _cdpPortFromUrl(env.BACOPY_CHROME_CDP_URL || process.env.BACOPY_CHROME_CDP_URL || 'http://127.0.0.1:9222');
      if (!(await isCdpUp(port))) {
        // hh88 を開きたいので、hh88 として Chrome を起こす(初期URLが hh88 になる)。
        env.BACOPY_PLATFORM = 'hh88';
        await ensureCdpChrome(env);
        // 初期URLが hh88 ログインなら新タブ不要だが、確実性のため下で new タブも試みる。
      }
      const res = await openUrlInCdpChrome(port, target);
      return res;
    } catch (e) {
      return { ok: false, error: String(e && e.message || e) };
    }
  });

  ipcMain.handle('start-bot', (_evt, config) => {
    console.log('[Main] start-bot requested mode=' + (config && config.mode) + ' live=' + !!(config && config.live));
    startBot(config || {});
    return { ok: true };
  });

  ipcMain.handle('stop-bot', () => {
    console.log('[Main] stop-bot requested');
    stopBot();
    return { ok: true };
  });

  ipcMain.handle('manual-assist-command', (_evt, payload) => {
    try {
      if (!botProcess || !botProcess.stdin || botProcess.killed) {
        return { ok: false, error: 'engine_not_running' };
      }
      const msg = { ...(payload || {}), type: 'manual_assist_command' };
      botProcess.stdin.write(JSON.stringify(msg) + '\n', 'utf-8');
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  });

  // 安全モード(当日勝率ゲート)の即時 ON/OFF をエンジンへ stdin で送る(再起動不要)。
  // 起動時の初期値は buildSpawnSpec の BACOPY_SAFETY_MODE で渡す。
  ipcMain.handle('set-safety-mode', (_evt, enabled) => {
    try {
      if (!botProcess || !botProcess.stdin || botProcess.killed) {
        return { ok: false, error: 'engine_not_running' };
      }
      const msg = { type: 'safety_mode', enabled: !!enabled };
      botProcess.stdin.write(JSON.stringify(msg) + '\n', 'utf-8');
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  });

  // 逆張り(reverse)モードの即時 ON/OFF をエンジンへ stdin で送る(再起動不要)。
  // ★永続化しない: 起動時は常に OFF(エンジン側 _reverse_bet=False 固定)。
  ipcMain.handle('set-reverse-bet', (_evt, on) => {
    try {
      if (!botProcess || !botProcess.stdin || botProcess.killed) {
        return { ok: false, error: 'engine_not_running' };
      }
      const msg = { type: 'set_reverse', on: !!on };
      botProcess.stdin.write(JSON.stringify(msg) + '\n', 'utf-8');
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
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
    // プラットフォーム選択を .env に永続化 → デスクトップアイコン起動時の
    // ensureCdpChrome が正しいカジノ(Stake/hh88)のURLでブラウザを開ける。
    if ('platform' in s)           updates.BACOPY_PLATFORM = (s.platform === 'hh88') ? 'hh88' : 'stake';
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
  console.log('[Main] window-all-closed');
  stopBot();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  console.log('[Main] before-quit');
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

process.on('exit', (code) => {
  _appendMainCapture('log', ['[Main] process exit code=' + code]);
});
