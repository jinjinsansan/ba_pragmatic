const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

let mainWindow = null;

const SERVICES = {
  executor_pragmatic: { label: 'Executor (Pragmatic Live)' },
  watch_pragmatic: { label: 'Watcher (Pragmatic)' },
  watch_evolution: { label: 'Watcher (Evolution)' },
};

const processes = new Map(); // name -> { proc, startedAt }

function _configPath() {
  return path.join(app.getPath('userData'), 'bacopy_config.json');
}

function _defaultConfig() {
  return {
    apiUrl: 'https://master.bafather.uk',
    apiKey: '',
    pushSnapshots: true,
    executor: {
      executorId: 'gui-1',
      label: 'MAIN-PC',
      username: '',
      tableNameSubstr: 'Speed Baccarat',
      autoClickWaitSec: 90,
      flatAmount: 1,
      allowSwitchTable: true,
      headless: false,
      profileDir: '',
      cookiesFile: '',
      onlyTableId: '',
    },
    watchPragmatic: {
      headless: true,
      profile: '',
      cookies: 'auth_state/stake_cookies.json',
      duration: 0,
    },
    watchEvolution: {
      headless: true,
      interval: 2.0,
    },
    pythonExe: 'python',
  };
}

function loadConfig() {
  const p = _configPath();
  if (!fs.existsSync(p)) return _defaultConfig();
  try {
    const raw = fs.readFileSync(p, 'utf-8');
    const j = JSON.parse(raw);
    return { ..._defaultConfig(), ...(j || {}) };
  } catch (_) {
    return _defaultConfig();
  }
}

function saveConfig(cfg) {
  const p = _configPath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(cfg, null, 2), 'utf-8');
}

function sendLog(name, line) {
  if (!mainWindow) return;
  mainWindow.webContents.send('service-log', { name, line: String(line || '') });
}

function repoRoot() {
  // copytrade_gui/src/main.js -> copytrade_gui/src -> copytrade_gui -> repoRoot
  return path.join(__dirname, '..', '..');
}

function resolveEngine() {
  if (app.isPackaged) {
    const engineDir = path.join(process.resourcesPath, 'engine');
    return { mode: 'packaged', exe: path.join(engineDir, 'bacopy_engine.exe'), cwd: engineDir };
  }
  return { mode: 'dev', exe: null, cwd: repoRoot() };
}

function buildSpawnSpec(serviceName, cfg) {
  const engine = resolveEngine();
  const env = { ...process.env };
  env.BACOPY_API_URL = cfg.apiUrl || '';
  env.BACOPY_API_KEY = cfg.apiKey || '';
  env.BACOPY_PUSH_SNAPSHOTS = cfg.pushSnapshots ? '1' : '0';

  if (serviceName === 'executor_pragmatic') {
    env.BACOPY_EXECUTOR_ID = (cfg.executor && cfg.executor.executorId) || '';
    env.BACOPY_EXECUTOR_LABEL = (cfg.executor && cfg.executor.label) || '';
    env.BACOPY_EXECUTOR_USERNAME = (cfg.executor && cfg.executor.username) || '';
  }

  if (engine.mode === 'packaged') {
    const args = [];
    if (serviceName === 'executor_pragmatic') {
      args.push('executor-pragmatic');
      if (cfg.executor.allowSwitchTable) args.push('--allow-switch-table');
      args.push('--flat-amount', String(cfg.executor.flatAmount || 1));
      if (cfg.executor.tableNameSubstr) args.push('--table-name-substr', String(cfg.executor.tableNameSubstr));
      args.push('--auto-click-wait-sec', String(cfg.executor.autoClickWaitSec || 90));
      if (cfg.executor.onlyTableId) args.push('--only-table-id', String(cfg.executor.onlyTableId));
      if (cfg.executor.headless) args.push('--headless');
      if (cfg.executor.profileDir) args.push('--profile-dir', String(cfg.executor.profileDir));
      if (cfg.executor.cookiesFile) args.push('--cookies-file', String(cfg.executor.cookiesFile));
    } else if (serviceName === 'watch_pragmatic') {
      args.push('watch-pragmatic');
      if (cfg.watchPragmatic.headless) args.push('--headless');
      if (cfg.watchPragmatic.duration) args.push('--duration', String(cfg.watchPragmatic.duration));
      if (cfg.watchPragmatic.profile) args.push('--profile', String(cfg.watchPragmatic.profile));
      if (cfg.watchPragmatic.cookies) args.push('--cookies', String(cfg.watchPragmatic.cookies));
    } else if (serviceName === 'watch_evolution') {
      args.push('watch-evolution');
      if (cfg.watchEvolution.headless) args.push('--headless');
      args.push('--interval', String(cfg.watchEvolution.interval || 2.0));
    } else {
      throw new Error(`unknown service: ${serviceName}`);
    }
    return { exe: engine.exe, cwd: engine.cwd, args, env };
  }

  const py = (cfg.pythonExe || 'python').trim() || 'python';
  const args = ['-u'];
  if (serviceName === 'executor_pragmatic') {
    args.push(path.join(repoRoot(), 'bacopy_executor_pragmatic_ws_live.py'));
    if (cfg.executor.allowSwitchTable) args.push('--allow-switch-table');
    args.push('--flat-amount', String(cfg.executor.flatAmount || 1));
    if (cfg.executor.tableNameSubstr) args.push('--table-name-substr', String(cfg.executor.tableNameSubstr));
    args.push('--auto-click-wait-sec', String(cfg.executor.autoClickWaitSec || 90));
    if (cfg.executor.onlyTableId) args.push('--only-table-id', String(cfg.executor.onlyTableId));
    if (cfg.executor.headless) args.push('--headless');
    if (cfg.executor.profileDir) args.push('--profile-dir', String(cfg.executor.profileDir));
    if (cfg.executor.cookiesFile) args.push('--cookies-file', String(cfg.executor.cookiesFile));
  } else if (serviceName === 'watch_pragmatic') {
    args.push(path.join(repoRoot(), 'bacopy_watch_pragmatic.py'));
    if (cfg.watchPragmatic.headless) args.push('--headless');
    if (cfg.watchPragmatic.duration) args.push('--duration', String(cfg.watchPragmatic.duration));
    if (cfg.watchPragmatic.profile) args.push('--profile', String(cfg.watchPragmatic.profile));
    if (cfg.watchPragmatic.cookies) args.push('--cookies', String(cfg.watchPragmatic.cookies));
  } else if (serviceName === 'watch_evolution') {
    args.push(path.join(repoRoot(), 'bacopy_watch_evolution.py'));
    if (cfg.watchEvolution.headless) args.push('--headless');
    args.push('--interval', String(cfg.watchEvolution.interval || 2.0));
  } else {
    throw new Error(`unknown service: ${serviceName}`);
  }
  return { exe: py, cwd: repoRoot(), args, env };
}

function startService(serviceName) {
  if (processes.has(serviceName)) return;
  const cfg = loadConfig();
  const spec = buildSpawnSpec(serviceName, cfg);
  const proc = spawn(spec.exe, spec.args, {
    cwd: spec.cwd,
    env: spec.env,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: false,
  });
  processes.set(serviceName, { proc, startedAt: Date.now() });
  sendLog(serviceName, `[spawn] ${spec.exe} ${spec.args.join(' ')}`);

  proc.stdout.on('data', (d) => sendLog(serviceName, d.toString('utf-8')));
  proc.stderr.on('data', (d) => sendLog(serviceName, d.toString('utf-8')));
  proc.on('exit', (code) => {
    sendLog(serviceName, `[exit] code=${code}`);
    processes.delete(serviceName);
    if (mainWindow) mainWindow.webContents.send('service-log', { name: serviceName, exit: true, code });
  });
  proc.on('error', (e) => {
    sendLog(serviceName, `[error] ${e.message || e}`);
  });
}

function stopService(serviceName) {
  const entry = processes.get(serviceName);
  if (!entry) return;
  try {
    entry.proc.kill();
  } catch (_) {}
}

function listServices() {
  const out = {};
  for (const name of Object.keys(SERVICES)) {
    out[name] = {
      label: SERVICES[name].label,
      running: processes.has(name),
    };
  }
  return out;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 800,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
}

app.whenReady().then(() => {
  createWindow();

  ipcMain.handle('get-config', () => loadConfig());
  ipcMain.handle('save-config', (_evt, cfg) => {
    saveConfig(cfg || {});
    return { ok: true };
  });
  ipcMain.handle('start-service', (_evt, name) => {
    startService(String(name || ''));
    return { ok: true, services: listServices() };
  });
  ipcMain.handle('stop-service', (_evt, name) => {
    stopService(String(name || ''));
    return { ok: true, services: listServices() };
  });
  ipcMain.handle('get-services', () => listServices());
});

app.on('window-all-closed', () => {
  for (const name of processes.keys()) stopService(name);
  if (process.platform !== 'darwin') app.quit();
});
