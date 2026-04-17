const $ = (id) => document.getElementById(id);

const logs = {
  executor_pragmatic: [],
  watch_pragmatic: [],
  watch_evolution: [],
};

function appendLog(name, line) {
  if (!logs[name]) logs[name] = [];
  const s = String(line || '').replace(/\r/g, '');
  for (const part of s.split('\n')) {
    if (!part.trim()) continue;
    logs[name].push(part);
  }
  if (logs[name].length > 2000) logs[name] = logs[name].slice(-2000);
  renderLogs();
}

function renderLogs() {
  const sel = $('logSel').value;
  $('logBox').textContent = (logs[sel] || []).slice(-500).join('\n');
}

function setStatusPill(el, running) {
  el.textContent = running ? 'RUNNING' : 'STOPPED';
  el.style.background = running ? '#12321f' : '#1f2937';
  el.style.color = running ? '#86efac' : '#9fb0c5';
}

function readCfgFromUI(cfg) {
  cfg.apiUrl = $('apiUrl').value.trim();
  cfg.apiKey = $('apiKey').value;
  cfg.pushSnapshots = $('pushSnapshots').checked;
  cfg.pythonExe = $('pythonExe').value.trim() || 'python';

  cfg.executor = cfg.executor || {};
  cfg.executor.executorId = $('executorId').value.trim();
  cfg.executor.label = $('executorLabel').value.trim();
  cfg.executor.username = $('executorUsername').value.trim();
  cfg.executor.tableNameSubstr = $('tableSubstr').value.trim();
  cfg.executor.onlyTableId = $('onlyTableId').value.trim();
  cfg.executor.flatAmount = Number($('flatAmount').value || '1');
  cfg.executor.autoClickWaitSec = Number($('autoClickWaitSec').value || '90');
  cfg.executor.allowSwitchTable = $('allowSwitchTable').checked;
  cfg.executor.headless = $('executorHeadless').checked;
  cfg.executor.profileDir = $('executorProfileDir').value.trim();
  cfg.executor.cookiesFile = $('executorCookiesFile').value.trim();

  cfg.watchPragmatic = cfg.watchPragmatic || {};
  cfg.watchPragmatic.headless = $('watchPragHeadless').checked;
  cfg.watchPragmatic.cookies = $('watchPragCookies').value.trim();
  cfg.watchPragmatic.profile = $('watchPragProfile').value.trim();
  cfg.watchPragmatic.duration = Number($('watchPragDuration').value || '0');

  cfg.watchEvolution = cfg.watchEvolution || {};
  cfg.watchEvolution.headless = $('watchEvoHeadless').checked;
  cfg.watchEvolution.interval = Number($('watchEvoInterval').value || '2');

  return cfg;
}

function applyCfgToUI(cfg) {
  $('apiUrl').value = cfg.apiUrl || '';
  $('apiKey').value = cfg.apiKey || '';
  $('pushSnapshots').checked = !!cfg.pushSnapshots;
  $('pythonExe').value = cfg.pythonExe || 'python';

  const ex = cfg.executor || {};
  $('executorId').value = ex.executorId || '';
  $('executorLabel').value = ex.label || '';
  $('executorUsername').value = ex.username || '';
  $('tableSubstr').value = ex.tableNameSubstr || '';
  $('onlyTableId').value = ex.onlyTableId || '';
  $('flatAmount').value = String(ex.flatAmount || 1);
  $('autoClickWaitSec').value = String(ex.autoClickWaitSec || 90);
  $('allowSwitchTable').checked = !!ex.allowSwitchTable;
  $('executorHeadless').checked = !!ex.headless;
  $('executorProfileDir').value = ex.profileDir || '';
  $('executorCookiesFile').value = ex.cookiesFile || '';

  const wp = cfg.watchPragmatic || {};
  $('watchPragHeadless').checked = wp.headless !== false;
  $('watchPragCookies').value = wp.cookies || 'auth_state/stake_cookies.json';
  $('watchPragProfile').value = wp.profile || '';
  $('watchPragDuration').value = String(wp.duration || 0);

  const we = cfg.watchEvolution || {};
  $('watchEvoHeadless').checked = we.headless !== false;
  $('watchEvoInterval').value = String(we.interval || 2);
}

async function refreshStatuses() {
  const s = await window.bacopy.getServices();
  setStatusPill($('stExecutor'), !!(s.executor_pragmatic && s.executor_pragmatic.running));
  setStatusPill($('stWatchPrag'), !!(s.watch_pragmatic && s.watch_pragmatic.running));
  setStatusPill($('stWatchEvo'), !!(s.watch_evolution && s.watch_evolution.running));
}

async function save() {
  const cur = await window.bacopy.getConfig();
  const cfg = readCfgFromUI(cur);
  await window.bacopy.saveConfig(cfg);
  appendLog('executor_pragmatic', '[ui] saved config');
}

async function main() {
  const cfg = await window.bacopy.getConfig();
  applyCfgToUI(cfg);
  await refreshStatuses();
  renderLogs();

  $('btnSave').onclick = save;

  $('btnStartExecutor').onclick = async () => { await save(); await window.bacopy.startService('executor_pragmatic'); await refreshStatuses(); };
  $('btnStopExecutor').onclick = async () => { await window.bacopy.stopService('executor_pragmatic'); await refreshStatuses(); };

  $('btnStartWatchPrag').onclick = async () => { await save(); await window.bacopy.startService('watch_pragmatic'); await refreshStatuses(); };
  $('btnStopWatchPrag').onclick = async () => { await window.bacopy.stopService('watch_pragmatic'); await refreshStatuses(); };

  $('btnStartWatchEvo').onclick = async () => { await save(); await window.bacopy.startService('watch_evolution'); await refreshStatuses(); };
  $('btnStopWatchEvo').onclick = async () => { await window.bacopy.stopService('watch_evolution'); await refreshStatuses(); };

  $('btnClear').onclick = () => {
    const sel = $('logSel').value;
    logs[sel] = [];
    renderLogs();
  };
  $('logSel').onchange = renderLogs;

  window.bacopy.onServiceLog((msg) => {
    if (msg && msg.name && msg.line) appendLog(msg.name, msg.line);
    if (msg && msg.name && msg.exit) appendLog(msg.name, `[exit] code=${msg.code}`);
  });

  setInterval(refreshStatuses, 2000);
}

main();
