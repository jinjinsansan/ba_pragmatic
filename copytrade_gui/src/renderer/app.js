// BACOPYRECEIVER — Renderer (Receiver GUI)

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let isRunning = false;
let logVisible = true;
let _billingOk = false;

// 周回ネオンアニメーションは GUI ウィンドウがアクティブな時のみ動かす。
// Camoufox (Evolution動画) と GPU を競合させないため。
// ウィンドウフォーカス → body.animations-on / blur → 解除
function _setAnimationsActive(active) {
  document.body.classList.toggle('animations-on', active);
}
window.addEventListener('focus', () => _setAnimationsActive(true));
window.addEventListener('blur', () => _setAnimationsActive(false));
// 初期状態: 起動直後はフォーカス想定で ON
if (document.hasFocus()) _setAnimationsActive(true);

// --- Title Bar ---
$('#btnMinimize').addEventListener('click', () => window.valhalla.windowMinimize());
$('#btnMaximize').addEventListener('click', () => window.valhalla.windowMaximize());
$('#btnClose').addEventListener('click', () => window.valhalla.windowClose());

// --- Setup / Auth Screen ---
function showSetup(errorMsg) {
  const scr = $('#setupScreen');
  const main = $('#mainContent');
  if (scr) scr.classList.remove('hidden');
  if (main) main.classList.add('hidden');
  const err = $('#setupError');
  if (err) {
    if (errorMsg) {
      err.classList.remove('hidden');
      err.textContent = errorMsg;
    } else {
      err.classList.add('hidden');
      err.textContent = '';
    }
  }
}

function showMain() {
  const scr = $('#setupScreen');
  const main = $('#mainContent');
  if (scr) scr.classList.add('hidden');
  if (main) main.classList.remove('hidden');
}

// --- Master Status ---
let _masterStatus = {
  connected: null,
  active: false,
  pending: 0,
  last_ok_at: '',
  last_error: '',
  last_decision_id: '',
  last_decision_action: '',
  last_decision_at: '',
};

function _renderMasterStatus() {
  const pill = $('#masterPill');
  const textEl = $('#masterPillText');
  if (!pill || !textEl) return;

  pill.classList.remove('master-online', 'master-active', 'master-offline', 'master-unknown');
  let label = 'MASTER: --';
  if (_masterStatus.connected === true) {
    if (_masterStatus.active) {
      pill.classList.add('master-active');
      label = 'MASTER: ACTIVE';
    } else {
      pill.classList.add('master-online');
      label = 'MASTER: ONLINE';
    }
  } else if (_masterStatus.connected === false) {
    pill.classList.add('master-offline');
    label = 'MASTER: OFFLINE';
  } else {
    pill.classList.add('master-unknown');
  }
  textEl.textContent = label;
}

function _renderMasterModal() {
  const conn = $('#masterConnVal');
  const act = $('#masterActVal');
  const pending = $('#masterPendingVal');
  const lastOk = $('#masterLastOkVal');
  const lastAction = $('#masterLastActionVal');
  const lastErr = $('#masterLastErrVal');

  if (conn) conn.textContent = (_masterStatus.connected === true) ? 'ONLINE' : (_masterStatus.connected === false) ? 'OFFLINE' : '--';
  if (act) act.textContent = _masterStatus.active ? 'ACTIVE' : 'IDLE';
  if (pending) pending.textContent = Number.isFinite(Number(_masterStatus.pending)) ? String(_masterStatus.pending) : '--';
  if (lastOk) lastOk.textContent = _masterStatus.last_ok_at || '--';
  if (lastErr) lastErr.textContent = _masterStatus.last_error || '--';
  if (lastAction) {
    if (_masterStatus.last_decision_id) {
      const a = _masterStatus.last_decision_action || 'DECISION';
      const t = _masterStatus.last_decision_at || '';
      lastAction.textContent = t ? `${a} ${_masterStatus.last_decision_id} @ ${t}` : `${a} ${_masterStatus.last_decision_id}`;
    } else {
      lastAction.textContent = '--';
    }
  }
}

$('#masterPill')?.addEventListener('click', () => {
  $('#masterModal')?.classList.remove('hidden');
  _renderMasterModal();
});
$('#masterClose')?.addEventListener('click', () => $('#masterModal')?.classList.add('hidden'));

function _msUntilNextJstMidnight() {
  const now = Date.now();
  const jst = new Date(now + 9 * 60 * 60 * 1000);
  const next = new Date(jst);
  next.setUTCHours(0, 0, 5, 0); // 00:00:05 JST (avoid exact boundary)
  next.setUTCDate(next.getUTCDate() + 1);
  const nextUtcMs = next.getTime() - 9 * 60 * 60 * 1000;
  return Math.max(1000, nextUtcMs - now);
}

async function refreshBilling({ silent = false } = {}) {
  if (!window.valhalla.getBillingStatus) {
    _billingOk = false;
    const el = $('#creditBalance');
    if (el) el.textContent = '-';
    return { ok: false, reason: 'Billing API unavailable' };
  }
  try {
    const b = await window.valhalla.getBillingStatus();
    _billingOk = !!(b && b.ok);
    const el = $('#creditBalance');
    if (el) {
      if (b && typeof b.balance === 'number') el.textContent = `$${b.balance.toFixed(2)}`;
      else el.textContent = '-';
      el.className = 'stat-value ' + (_billingOk ? 'positive' : 'negative');
    }
    // Disable START if billing is not OK
    if (!isRunning) {
      const startBtn = $('#btnStart');
      if (startBtn) startBtn.disabled = !_billingOk;
    }
    if (!_billingOk && !silent && b && b.reason) {
      addLog(`CREDIT: ${b.reason}`, 'warn');
    }
    // Enforce stop if credit becomes invalid
    if (!_billingOk && isRunning) {
      await stopBotFlow({ forced: true, reason: b && b.reason ? b.reason : 'Credit is not active' });
      showSetup(b && b.reason ? b.reason : 'Credit is not active');
    }
    return b || { ok: false, reason: 'Unknown billing status' };
  } catch (e) {
    _billingOk = false;
    if (!silent) addLog(`Billing check failed: ${e.message || e}`, 'warn');
    return { ok: false, reason: 'Billing check failed' };
  }
}

let _billingTimer = null;
let _midnightTimer = null;

function startBillingMonitors() {
  if (_billingTimer) clearInterval(_billingTimer);
  _billingTimer = setInterval(() => refreshBilling({ silent: true }), 60 * 1000);

  if (_midnightTimer) clearTimeout(_midnightTimer);
  const wait = _msUntilNextJstMidnight();
  _midnightTimer = setTimeout(async () => {
    await refreshBilling({ silent: false });
    startBillingMonitors();
  }, wait);
}

function isManualStop() {
  return localStorage.getItem('bacopy_manual_stop') === '1';
}
function setManualStop(on) {
  if (on) localStorage.setItem('bacopy_manual_stop', '1');
  else localStorage.removeItem('bacopy_manual_stop');
}

async function initAuth() {
  if (!window.valhalla.authGetSession) {
    showSetup('Auth API unavailable (preload mismatch).');
    return;
  }
  try {
    const sess = await window.valhalla.authGetSession();
    if (!sess || !sess.ok) {
      showSetup();
      return;
    }
    const b = await refreshBilling({ silent: true });
    if (!b.ok) {
      showSetup(b.reason || 'Credit is not active');
      return;
    }
    showMain();
    startBillingMonitors();
    // Auto-arm unless user explicitly stopped
    if (!isManualStop()) {
      startBotFlow({ auto: true });
    }
  } catch (e) {
    showSetup(`Sign-in check failed: ${e.message || e}`);
  }
}

async function handleSignIn() {
  const email = ($('#setupEmail')?.value || '').trim();
  const password = ($('#setupPassword')?.value || '').trim();
  if (!email || !password) {
    showSetup('Email and password are required.');
    return;
  }
  const loading = $('#setupLoading');
  const btn = $('#btnActivate');
  if (loading) loading.style.display = 'block';
  if (btn) btn.disabled = true;
  try {
    const res = await window.valhalla.authSignIn(email, password);
    if (!res || !res.ok) {
      showSetup(res && res.reason ? res.reason : 'Sign-in failed');
      return;
    }
    const b = await refreshBilling({ silent: true });
    if (!b.ok) {
      showSetup(b.reason || 'Credit is not active');
      return;
    }
    showMain();
    startBillingMonitors();
    if (!isManualStop()) {
      startBotFlow({ auto: true });
    }
  } catch (e) {
    showSetup(`Sign-in failed: ${e.message || e}`);
  } finally {
    if (loading) loading.style.display = 'none';
    if (btn) btn.disabled = false;
  }
}

$('#btnActivate')?.addEventListener('click', handleSignIn);
$('#setupPassword')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') handleSignIn();
});
$('#linkBafather')?.addEventListener('click', () => window.valhalla.openExternal('https://bafather.uk'));

// --- Start / Stop ---
// ========== 残高スナップショット方式のPNL ==========
// 真実の源: Python から届く session_open_balance / daily_open_balance + current balance。
// Session PNL = currentBalance - sessionOpenBalance
// Daily PNL   = currentBalance - dailyOpenBalance (日付変化時に open を更新)
// GUI は計算・表示のみ担当。累積や加算はしない。
let _startedAt = 0;  // START押下時刻 (stopped誤検知防止用)
let _currentBalance = null;        // 最新残高 (Pythonから)
let _sessionOpenBalance = null;    // セッション起点残高
let _dailyOpenBalance = null;      // デイリー起点残高
let _dailyOpenDate = null;         // デイリー起点日付 (JST YYYY-MM-DD)
// 互換: sessionTotal は計算結果を保持する (他コードが参照するため)
let sessionTotal = 0;
const results = [];  // 'W' | 'L' | 'T'

function _jstDateStrNow() {
  const now = new Date();
  const utcMs = now.getTime() + (now.getTimezoneOffset() * 60000);
  const jst = new Date(utcMs + 9 * 3600000);
  return `${jst.getFullYear()}-${String(jst.getMonth()+1).padStart(2,'0')}-${String(jst.getDate()).padStart(2,'0')}`;
}

// 残高として妥当 (正数) かチェック。0 や負値は「不明」扱い。
// これを通らなければ _currentBalance 等に代入されない。
function _isValidBalance(v) {
  return typeof v === 'number' && Number.isFinite(v) && v > 0;
}

function _computePnl() {
  // 残高が未確定 (null / 0 / 負値) の場合は PNL=0 扱い
  // これで GUI 開いた直後に古いスナップショットの dailyOpen と 0 残高で
  // -$XXXX が出る事故を防ぐ。最初の status 到達で正しい値に回復する。
  if (!_isValidBalance(_currentBalance)) return { session: 0, daily: 0 };
  // Daily: 日付ロールオーバーチェック (GUI側でもフォールバック処理)
  const today = _jstDateStrNow();
  if (_dailyOpenDate !== today) {
    // Python から新しい date がまだ来ていない場合の暫定処理
    _dailyOpenDate = today;
    _dailyOpenBalance = _currentBalance;
    _persistBalanceSnapshot();
  }
  const session = _isValidBalance(_sessionOpenBalance) ? (_currentBalance - _sessionOpenBalance) : 0;
  const daily = _isValidBalance(_dailyOpenBalance) ? (_currentBalance - _dailyOpenBalance) : 0;
  sessionTotal = session;  // 互換用
  return { session, daily };
}

function _persistBalanceSnapshot() {
  try {
    // 無効値 (0 / null) は保存しない。古い有効データを壊さないため。
    const payload = {};
    if (_isValidBalance(_currentBalance)) payload.current = _currentBalance;
    if (_isValidBalance(_sessionOpenBalance)) payload.session_open = _sessionOpenBalance;
    if (_isValidBalance(_dailyOpenBalance)) payload.daily_open = _dailyOpenBalance;
    if (typeof _dailyOpenDate === 'string' && _dailyOpenDate) payload.daily_date = _dailyOpenDate;
    // 何も有効値が無ければそもそも書かない (既存データ保持)
    if (Object.keys(payload).length === 0) return;
    // 既存データとマージ (他フィールドを消さない)
    try {
      const existing = JSON.parse(localStorage.getItem('valhalla_balance_snapshot') || '{}');
      Object.assign(existing, payload);
      localStorage.setItem('valhalla_balance_snapshot', JSON.stringify(existing));
    } catch {
      localStorage.setItem('valhalla_balance_snapshot', JSON.stringify(payload));
    }
  } catch {}
}

function _restoreBalanceSnapshot() {
  try {
    const raw = localStorage.getItem('valhalla_balance_snapshot');
    if (!raw) return;
    const s = JSON.parse(raw);
    if (_isValidBalance(s.current)) _currentBalance = s.current;
    if (_isValidBalance(s.session_open)) _sessionOpenBalance = s.session_open;
    if (_isValidBalance(s.daily_open)) _dailyOpenBalance = s.daily_open;
    if (typeof s.daily_date === 'string' && s.daily_date) _dailyOpenDate = s.daily_date;
  } catch {}
}

function buildStartConfig() {
  const s = loadSettings();
  return {
    ...s,
    bet_mode: 'counter_seq7',
    resume_results: Array.isArray(results) ? results.slice() : [],
  };
}

async function startBotFlow({ auto = false } = {}) {
  if (!_billingOk) {
    showSetup('Credit is not active. Please charge at bafather.uk.');
    return;
  }
  setManualStop(false);

  const config = buildStartConfig();
  const hasPrev = localStorage.getItem('valhalla_session_state');
  if (hasPrev && !auto) {
    const choice = await showContinueDialog();
    if (choice === 'cancel') return;
    config.resume = (choice === 'continue');
  } else {
    config.resume = !!hasPrev;
  }

  if (!config.resume) {
    _sessionOpenBalance = null;
    sessionTotal = 0;
    _persistBalanceSnapshot();
    updateSessionDisplay();
    resetFeed();
  }

  config.resume_results = Array.isArray(results) ? results.slice() : [];
  _startedAt = Date.now();
  setRunning(true);
  setPhase('scanning', auto ? 'armed' : 'starting...');
  addLog(auto ? 'Armed. Waiting for master signal...' : 'Bot starting...', 'info');
  try {
    await window.valhalla.startBot(config);
    addLog('Bot started.', 'info');
  } catch (e) {
    addLog(`Start failed: ${e.message || e}`, 'lose');
    setRunning(false);
    setManualStop(true);
  }
}

async function stopBotFlow({ forced = false, reason = '' } = {}) {
  if (!forced) setManualStop(true);
  try {
    await window.valhalla.stopBot();
  } catch {}
  setRunning(false);
  if (forced && reason) {
    addLog(`Bot stopped: ${reason}`, 'warn');
    setAction(reason);
  } else {
    addLog('Bot stopped.', 'info');
    setAction('Stopped');
  }
}

$('#btnStart')?.addEventListener('click', () => startBotFlow({ auto: false }));

function updateSessionDisplay() {
  const { session, daily } = _computePnl();
  const el = $('#sessionPnl');
  el.textContent = `$${session >= 0 ? '+' : ''}${session.toFixed(2)}`;
  el.className = 'stat-value ' + (session >= 0 ? 'positive' : 'negative');
  // Daily P&L もここで更新
  const todayEl = $('#todayPnl');
  if (todayEl) {
    todayEl.textContent = `${daily >= 0 ? '+$' : '-$'}${Math.abs(daily).toFixed(0)}`;
    todayEl.className = 'today-pnl ' + (daily >= 0 ? 'positive' : 'negative');
  }
  persistSessionState();
}

function persistSessionState() {
  try {
    const state = {
      sessionTotal,
      results: results.slice(-200),
      ts: Date.now(),
    };
    localStorage.setItem('valhalla_session_state', JSON.stringify(state));
  } catch {}
}

function restoreSessionState() {
  try {
    const raw = localStorage.getItem('valhalla_session_state');
    if (!raw) return false;
    const state = JSON.parse(raw);
    // 互換: 旧 sessionTotal フィールドは残高スナップショット方式では不要。
    // _restoreBalanceSnapshot() で session_open_balance を復元する。
    results.length = 0;
    if (Array.isArray(state.results)) {
      for (const r of state.results) results.push(r);
    }
    _restoreBalanceSnapshot();
    updateSessionDisplay();
    renderFeed();
    renderRecent();
    return true;
  } catch { return false; }
}

$('#btnStop').addEventListener('click', async () => {
  await stopBotFlow({ forced: false });
});

function setRunning(running) {
  isRunning = running;
  $('#btnStart').disabled = running || !_billingOk;
  $('#btnStop').disabled = !running;
}

// Continue/Reset dialog
function showContinueDialog() {
  return new Promise((resolve) => {
    const modal = $('#continueModal');
    modal.classList.remove('hidden');
    const cleanup = () => {
      modal.classList.add('hidden');
      $('#btnContinue').onclick = null;
      $('#btnResetAll').onclick = null;
      $('#continueClose').onclick = null;
    };
    $('#btnContinue').onclick = () => { restoreSessionState(); cleanup(); resolve('continue'); };
    $('#btnResetAll').onclick = () => {
      // セッション関連のみリセット。デイリー (dailyOpenBalance/Date) と 14日履歴は保持。
      // 理由: Daily PNL は 20% 課金計算の基準で、管理パネルと同期必要。
      // 0時ロールオーバー以外で消してはいけない。
      localStorage.removeItem('valhalla_session_state');
      sessionTotal = 0;
      _sessionOpenBalance = null;  // セッション起点のみクリア
      // _currentBalance / _dailyOpenBalance / _dailyOpenDate は温存
      _persistBalanceSnapshot();
      updateSessionDisplay();
      resetFeed();
      cleanup();
      resolve('reset');
    };
    $('#continueClose').onclick = () => { cleanup(); resolve('cancel'); };
  });
}

// --- Settings ---
const DEFAULT_SETTINGS = {
  chip_base: 1,
  profit_target: 50,
  profit_session_limit: 0,
  loss_cut: 200,
  executor_id: 'gui-1',
  executor_label: 'MAIN-PC',
  stake_username: '',
  table_name_substr: 'Speed Baccarat',
  auto_click_wait_sec: 90,
  allow_switch_table: true,
  headless: false,
  dry_run: false,
  bet_mode: 'counter_seq7',
};
const ALLOWED_BET_MODES = new Set(['counter_seq7']);

function normalizeBetMode(mode) {
  return ALLOWED_BET_MODES.has(mode) ? mode : 'counter_seq7';
}

function normalizeProfitSessionLimit(value) {
  const n = Number.isFinite(Number(value)) ? Math.floor(Number(value)) : 0;
  return n >= 0 ? n : 0;
}

const SITE_URL = 'https://bafather.uk';
const LAPLACE_API_KEY = '';

let _paramCandidates = [];

function _formatParamCandidate(c, idx) {
  // シンプル化: "Auto2" / "Auto3" 等の表記のみ (内部パラメータは非表示)
  return `Auto${idx + 2}`;
}

function _renderParamCandidates(selected) {
  const select = $('#inputParamCandidate');
  if (!select) return;
  const hint = $('#paramCandidateHint');
  select.innerHTML = '<option value="auto">Auto</option>';
  if (_paramCandidates.length === 0) {
    if (hint) hint.textContent = 'No candidates from server.';
    return;
  }
  _paramCandidates.forEach((c, idx) => {
    const opt = document.createElement('option');
    opt.value = String(idx);
    opt.textContent = _formatParamCandidate(c, idx);
    select.appendChild(opt);
  });
  if (typeof selected === 'string' || typeof selected === 'number') {
    select.value = String(selected);
  }
  if (hint) hint.textContent = 'Choose a candidate to apply for this session only.';
}

async function loadParamCandidates(selected) {
  try {
    const res = await fetch(`${SITE_URL}/api/optimal-params/candidates?api_key=${encodeURIComponent(LAPLACE_API_KEY)}`);
    const data = await res.json();
    _paramCandidates = Array.isArray(data.candidates) ? data.candidates : [];
    _renderParamCandidates(selected);
  } catch (e) {
    _paramCandidates = [];
    _renderParamCandidates(selected);
  }
}

// --- Recommended Tables (Supabase) ---
async function fetchRecommendedTables() {
  const email = loadSettings().user_email;
  const qs = email
    ? `?email=${encodeURIComponent(email)}&api_key=${encodeURIComponent(LAPLACE_API_KEY)}`
    : `?api_key=${encodeURIComponent(LAPLACE_API_KEY)}`;
  try {
    const res = await fetch(`${SITE_URL}/api/recommended-tables${qs}`);
    const data = await res.json();
    if (data.tables && Array.isArray(data.tables)) {
      localStorage.setItem('recommended_tables', JSON.stringify(data.tables));
      localStorage.setItem('recommended_tables_source', data.source || 'unknown');
      return data.tables;
    }
  } catch (e) {
    console.warn('[sync] recommended-tables fetch failed:', e);
  }
  // fallback
  const cached = localStorage.getItem('recommended_tables');
  return cached ? JSON.parse(cached) : [
    { name: 'Japanese Speed Baccarat A', enabled: true, priority: 1 },
    { name: 'Korean Speed Baccarat B', enabled: true, priority: 2 },
  ];
}

async function saveRecommendedTablesToServer(tables) {
  const email = loadSettings().user_email;
  if (!email) return;
  try {
    await fetch(`${SITE_URL}/api/recommended-tables`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, api_key: LAPLACE_API_KEY, tables }),
    });
  } catch (e) {
    console.warn('[sync] recommended-tables save failed:', e);
  }
}

function getEnabledRecommendedTables() {
  const cached = localStorage.getItem('recommended_tables');
  if (!cached) return ['Japanese Speed Baccarat A', 'Korean Speed Baccarat B'];
  try {
    const tables = JSON.parse(cached);
    return tables
      .filter(t => t.enabled !== false)
      .sort((a, b) => (a.priority || 999) - (b.priority || 999))
      .map(t => t.name);
  } catch {
    return ['Japanese Speed Baccarat A', 'Korean Speed Baccarat B'];
  }
}

// --- GUI State Persistence (Supabase) ---
let _guiSyncTimer = null;
let _guiSyncPending = false;

function _getGuiState() {
  // 無効値 (null/0/負値) は送らない。サーバーの有効データを 0 で上書きして
  // 再起動時に -$XXXX を発生させないため。
  const payload = {
    // 互換: 旧フィールド
    session_total: sessionTotal,
    daily_pnl: loadDailyPnl(),
    results: results.slice(-200),
    bet_mode: (loadSettings().bet_mode || 'counter'),
    updated_at: new Date().toISOString(),
  };
  if (_isValidBalance(_currentBalance)) payload.current_balance = _currentBalance;
  if (_isValidBalance(_sessionOpenBalance)) payload.session_open_balance = _sessionOpenBalance;
  if (_isValidBalance(_dailyOpenBalance)) payload.daily_open_balance = _dailyOpenBalance;
  if (typeof _dailyOpenDate === 'string' && _dailyOpenDate) payload.daily_open_date = _dailyOpenDate;
  return payload;
}

async function syncGuiStateToServer() {
  const email = loadSettings().user_email;
  if (!email) return;
  try {
    await fetch(`${SITE_URL}/api/gui-state`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, api_key: LAPLACE_API_KEY, gui_state: _getGuiState() }),
    });
  } catch (e) {
    console.warn('[sync] gui-state sync failed:', e);
  }
}

function scheduleGuiStateSync() {
  // Disabled in BACOPYRECEIVER.
}

async function loadGuiStateFromServer() {
  const email = loadSettings().user_email;
  if (!email) return null;
  try {
    const res = await fetch(`${SITE_URL}/api/gui-state?email=${encodeURIComponent(email)}&api_key=${encodeURIComponent(LAPLACE_API_KEY)}`);
    const data = await res.json();
    return data.gui_state || null;
  } catch (e) {
    console.warn('[sync] gui-state load failed:', e);
    return null;
  }
}

async function restoreGuiStateFromServer() {
  const state = await loadGuiStateFromServer();
  if (!state) return false;
  // サーバに保存されている残高スナップショットを復元 (Python から最新値が届くまでの暫定表示)
  // _isValidBalance で 0/負値を除外。古い有効値を 0 で上書きしない (デイリーPNL -$XXXX バグ防止)。
  if (_isValidBalance(state.current_balance)) _currentBalance = state.current_balance;
  if (_isValidBalance(state.session_open_balance)) _sessionOpenBalance = state.session_open_balance;
  if (_isValidBalance(state.daily_open_balance)) _dailyOpenBalance = state.daily_open_balance;
  if (typeof state.daily_open_date === 'string' && state.daily_open_date) _dailyOpenDate = state.daily_open_date;
  if (state.daily_pnl && typeof state.daily_pnl === 'object') {
    saveDailyPnl(state.daily_pnl);
  }
  if (Array.isArray(state.results) && state.results.length > 0) {
    results.length = 0;
    for (const r of state.results) results.push(r);
    renderFeed();
    renderRecent();
  }
  _persistBalanceSnapshot();
  updateSessionDisplay();
  renderDailyPnl();
  addLog('GUI state restored from server.', 'info');
  return true;
}

// Tab switching
function initModalTabs() {
  // Tabs removed in BACOPYRECEIVER.
}

$('#btnSettings')?.addEventListener('click', () => {
  $('#settingsModal')?.classList.remove('hidden');
  const s = loadSettings();
  $('#inputChipBase').value = s.chip_base;
  $('#inputProfitTarget').value = s.profit_target;
  if ($('#inputProfitSessionLimit')) $('#inputProfitSessionLimit').value = s.profit_session_limit ?? 0;
  $('#inputLossCut').value = s.loss_cut;
  $('#inputDryRun').checked = !!s.dry_run;

  $('#inputExecutorId').value = s.executor_id || '';
  $('#inputExecutorLabel').value = s.executor_label || '';
  $('#inputStakeUsername').value = s.stake_username || '';

  $('#inputTableNameSubstr').value = s.table_name_substr || '';
  $('#inputAutoClickWaitSec').value = s.auto_click_wait_sec || 90;

  $('#inputAllowSwitchTable').checked = !!s.allow_switch_table;
  $('#inputHeadless').checked = !!s.headless;
});
$('#settingsClose')?.addEventListener('click', () => $('#settingsModal')?.classList.add('hidden'));
$('#btnSaveSettings')?.addEventListener('click', async () => {
  // IME/composition の入力が確定していないと value が古いまま読まれる事があるため、
  // クリック時にフォーカスを外して 1tick 待ってから値を読む。
  try { document.activeElement?.blur?.(); } catch {}
  await new Promise((r) => setTimeout(r, 0));

  const settings = {
    chip_base: parseFloat($('#inputChipBase').value) || 1,
    profit_target: parseFloat($('#inputProfitTarget').value) || 50,
    profit_session_limit: normalizeProfitSessionLimit($('#inputProfitSessionLimit')?.value),
    loss_cut: parseFloat($('#inputLossCut').value) || 200,
    dry_run: $('#inputDryRun').checked,
    bet_mode: 'counter_seq7',
    executor_id: $('#inputExecutorId').value.trim(),
    executor_label: $('#inputExecutorLabel').value.trim(),
    stake_username: $('#inputStakeUsername').value.trim(),
    table_name_substr: $('#inputTableNameSubstr').value.trim(),
    auto_click_wait_sec: parseInt($('#inputAutoClickWaitSec').value, 10) || 90,
    allow_switch_table: $('#inputAllowSwitchTable').checked,
    headless: $('#inputHeadless').checked,
  };

  localStorage.setItem('bacopy_settings', JSON.stringify(settings));
  $('#settingsModal')?.classList.add('hidden');
  addLog(`Settings saved. Base:$${settings.chip_base} Target:$${settings.profit_target} LossCut:$${settings.loss_cut}`, 'info');
});

function loadSettings() {
  try {
    const stored = JSON.parse(localStorage.getItem('bacopy_settings') || '{}');
    const merged = { ...DEFAULT_SETTINGS, ...stored };
    merged.bet_mode = normalizeBetMode(merged.bet_mode);
    merged.profit_session_limit = normalizeProfitSessionLimit(merged.profit_session_limit);
    merged.executor_id = String(merged.executor_id || DEFAULT_SETTINGS.executor_id);
    merged.executor_label = String(merged.executor_label || DEFAULT_SETTINGS.executor_label);
    merged.stake_username = String(merged.stake_username || '');
    merged.table_name_substr = String(merged.table_name_substr || DEFAULT_SETTINGS.table_name_substr);
    merged.auto_click_wait_sec = Number.isFinite(Number(merged.auto_click_wait_sec)) ? Math.max(10, Math.floor(Number(merged.auto_click_wait_sec))) : DEFAULT_SETTINGS.auto_click_wait_sec;
    merged.allow_switch_table = !!merged.allow_switch_table;
    merged.headless = !!merged.headless;
    return merged;
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

// --- Developer Mode ---
const DEV_PASSWORD = 'laplace1749';

function isDevMode() {
  // Always on for BACOPYRECEIVER (SEQ7 / OS stream is a required display).
  return true;
}

function setDevMode(on) {
  // no-op (developer mode UI removed)
  applyDevMode();
}

function applyDevMode() {
  const panel = $('#devPanel');
  if (panel) panel.classList.remove('hidden');
}

const devModeLink = $('#devModeLink');
if (devModeLink) devModeLink.addEventListener('click', () => {
  if (isDevMode()) {
    setDevMode(false);
    addLog('Developer Mode disabled.', 'info');
  } else {
    $('#settingsModal').classList.add('hidden');
    $('#devModeModal').classList.remove('hidden');
    $('#inputDevPassword').value = '';
    $('#inputDevPassword').focus();
  }
});

const devModeClose = $('#devModeClose');
if (devModeClose) devModeClose.addEventListener('click', () => {
  $('#devModeModal').classList.add('hidden');
});

const btnDevAuth = $('#btnDevAuth');
if (btnDevAuth) btnDevAuth.addEventListener('click', () => {
  const pw = $('#inputDevPassword').value;
  if (pw === DEV_PASSWORD) {
    setDevMode(true);
    $('#devModeModal').classList.add('hidden');
    addLog('Developer Mode UNLOCKED.', 'win');
  } else {
    addLog('Invalid password.', 'lose');
    $('#inputDevPassword').value = '';
    $('#inputDevPassword').focus();
  }
});

const inputDevPassword = $('#inputDevPassword');
if (inputDevPassword) inputDevPassword.addEventListener('keypress', (e) => {
  if (e.key === 'Enter') $('#btnDevAuth').click();
});

// Encrypted signal helpers (same logic as Telegram)
const _SIG_PREFIXES = 'CDEFG';
const _SIG_WL_PREFIXES = 'QRSTM';
const _SIG_OS_PREFIXES = 'UVWXY';
function _rndChar(s) { return s[Math.floor(Math.random() * s.length)]; }
function _turnToCode(turn) { return _rndChar(_SIG_PREFIXES) + String.fromCharCode(65 + Math.max(0, turn - 1)); }
function _ratioToCode(w, l) { return _rndChar(_SIG_WL_PREFIXES) + w + _rndChar(_SIG_WL_PREFIXES) + l; }
function _driftToCode(os) { return _rndChar(_SIG_OS_PREFIXES) + os; }

// =============================================================
// Signal Stream — シンプル版 (タイ無視 / ROUNDと色同期)
//
// 唯一の真実: ローカルの _streamSetIdx / _streamTurnsInSet。
//  - Stream は round_result (O/X) のみで追加。Tieは完全無視。
//  - 色は _STREAM_SET_COLORS[_streamSetIdx % 6]。セット完了で +1。
//  - ROUND 表示色も同じローカル state を参照 → 必ず同期。
//  - shoe_history で履歴を再構築する時、同じルールで色付け。
// =============================================================
const _STREAM_SET_COLORS = ['#ff3366', '#ffcc00', '#00b8d4', '#ffffff', '#00ff88', '#c084fc'];
function _setSizeForMode(mode) { return mode === 'counter_seq7' ? 7 : 5; }
let _streamSetSize = _setSizeForMode(loadSettings().bet_mode || 'counter');
let _streamSetIdx = 0;
let _streamTurnsInSet = 0;
let _lastRoundWon = null;

function _currentSetColor() {
  return _STREAM_SET_COLORS[_streamSetIdx % _STREAM_SET_COLORS.length];
}

function _appendStreamMark(el, mark, color) {
  const span = document.createElement('span');
  span.textContent = mark;
  if (color) span.style.color = color;
  el.appendChild(span);
  const panel = el.parentElement;
  if (panel) panel.scrollTop = panel.scrollHeight;
  else el.scrollTop = el.scrollHeight;
}

// O/X を 1つ Stream に追加してローカル状態を進める (Tie は呼ばれない)
function _pushStreamMark(mark) {
  if (mark !== 'O' && mark !== 'X') return;
  if (!isDevMode()) {
    _streamTurnsInSet += 1;
    if (_streamTurnsInSet >= _streamSetSize) {
      _streamTurnsInSet = 0;
      _streamSetIdx += 1;
    }
    return;
  }
  const el = $('#sigStream');
  if (el && el.querySelector('[style*="rgba"]')) el.innerHTML = '';  // Clear "AWAITING SIGNAL"
  const color = _currentSetColor();
  if (el) _appendStreamMark(el, mark, color);
  _streamTurnsInSet += 1;
  if (_streamTurnsInSet >= _streamSetSize) {
    _streamTurnsInSet = 0;
    _streamSetIdx += 1;
  }
}

function updateDevPanel(msg) {
  if (!isDevMode()) return;
  const sc = $('#sigCycle');
  const sr = $('#sigRatio');
  const sd = $('#sigDrift');
  const srd = $('#sigRound');
  if (sc && typeof msg.current_turn === 'number') sc.textContent = _turnToCode(msg.current_turn);
  if (sr) {
    const pw = typeof msg.pre_wins === 'number' ? msg.pre_wins : 0;
    const pl = typeof msg.pre_losses === 'number' ? msg.pre_losses : 0;
    if (pw > 0 || pl > 0) {
      sr.textContent = _ratioToCode(pw, pl);
    } else {
      const td = msg.turns_display || '';
      const sw = (td.match(/O/g) || []).length;
      const sl = (td.match(/X/g) || []).length;
      sr.textContent = _ratioToCode(sw, sl);
    }
  }
  if (sd && typeof msg.overshoot === 'number') sd.textContent = _driftToCode(msg.overshoot);
  // ROUND = 累計O/X数, 色は Stream と同じローカル state
  if (srd) {
    const roundNum = _streamSetIdx * _streamSetSize + _streamTurnsInSet;
    srd.textContent = `#${roundNum}`;
    srd.style.color = _currentSetColor();
  }
}

function renderDevSets(sets) {
  // shoe_history: Python からの完了セット履歴で Stream を再同期。
  // ローカル state を履歴の最後に合わせる（信頼できる真実の源）。
  if (!isDevMode()) return;
  const el = $('#sigStream');
  if (!el) return;
  const list = Array.isArray(sets) ? sets : [];
  // ライブ更新で既に反映済みなら何もしない (全再構築は不要)
  if (el.children.length > 0 && _streamSetIdx === list.length && _streamTurnsInSet === 0) return;
  // 完全再構築
  el.innerHTML = '';
  for (let i = 0; i < list.length; i += 1) {
    const results = (list[i] && list[i].results) || '';
    const color = _STREAM_SET_COLORS[i % _STREAM_SET_COLORS.length];
    for (const c of results) {
      if (c === 'O' || c === 'X') _appendStreamMark(el, c, color);
    }
  }
  _streamSetIdx = list.length;
  _streamTurnsInSet = 0;
}

// --- Log ---
$('#logToggle').addEventListener('click', () => {
  logVisible = !logVisible;
  $('#logPanel').classList.toggle('hidden', !logVisible);
  $('#logToggle').innerHTML = logVisible ? 'CONSOLE &#x25B2;' : 'CONSOLE &#x25BC;';
});

function addLog(text, type = '') {
  const el = $('#logContent');
  const t = new Date().toLocaleTimeString();
  const span = document.createElement('span');
  if (type) span.className = `log-${type}`;
  span.textContent = `[${t}] ${text}`;
  el.appendChild(span);
  // Cap history to avoid DOM bloat
  while (el.childElementCount > 500) el.removeChild(el.firstChild);
  // Auto-scroll to latest (terminal-like)
  requestAnimationFrame(() => {
    const panel = el.parentElement;
    if (panel) panel.scrollTop = panel.scrollHeight;
  });
}

function esc(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

// --- Flash Effect ---
function flashScreen(type) {
  const el = $('#flashOverlay');
  el.className = 'flash-overlay ' + type;
  const duration = (type === 'profit' || type === 'losscut') ? 2000 : 900;
  setTimeout(() => { el.className = 'flash-overlay'; }, duration);
}

// --- Reset Toast (big banner for profit/loss lock) ---
function showResetToast(title, amount, isProfit) {
  let toast = $('#resetToast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'resetToast';
    toast.className = 'reset-toast';
    document.body.appendChild(toast);
  }
  toast.className = 'reset-toast ' + (isProfit ? 'profit' : 'losscut') + ' show';
  toast.innerHTML = `
    <div class="toast-title">${title}</div>
    <div class="toast-amount">${amount}</div>
    <div class="toast-sub">${isProfit ? 'Locked in. New session.' : 'Stopped loss. New session.'}</div>
  `;
  setTimeout(() => { toast.className = 'reset-toast ' + (isProfit ? 'profit' : 'losscut'); }, 3500);
}

// --- Action Text ---
function setAction(text) {
  $('#actionText').textContent = text;
}

// --- Phase Badge (状態バッジ) ---
const _PHASE_LABELS = {
  idle: 'IDLE',
  scanning: 'SCANNING',
  entering: 'ENTERING',
  betting: 'BETTING',
  betting_player: 'BET PLAYER',
  betting_banker: 'BET BANKER',
  ws_stall: 'WS STALL',
  error: 'ERROR',
  stopped: 'STOPPED',
};
const _PHASE_VALID = new Set(Object.keys(_PHASE_LABELS));
let _currentPhase = 'idle';
let _currentPhaseDetail = '';
let _lastPhaseUpdate = Date.now();
let _phaseStaleCheckTimer = null;

function setPhase(name, detail) {
  if (!_PHASE_VALID.has(name)) name = 'idle';
  _currentPhase = name;
  _currentPhaseDetail = detail || '';
  _lastPhaseUpdate = Date.now();
  _renderPhaseBadge(false);
}

function _renderPhaseBadge(isStale) {
  const badge = $('#phaseBadge');
  const textEl = $('#phaseBadgeText');
  if (!badge || !textEl) return;
  // Reset all phase-* classes
  badge.className = 'phase-badge';
  const phaseCls = isStale ? 'stale' : _currentPhase;
  badge.classList.add('phase-' + phaseCls);
  const label = _PHASE_LABELS[_currentPhase] || _currentPhase.toUpperCase();
  if (isStale) {
    const ageSec = Math.round((Date.now() - _lastPhaseUpdate) / 1000);
    textEl.textContent = `${label} · STALE ${ageSec}s`;
  } else {
    textEl.textContent = _currentPhaseDetail ? `${label} · ${_currentPhaseDetail}` : label;
  }
}

// 60秒以上 phase 更新がなければ STALE 表示。Bot実行中のみ監視。
function _startPhaseStaleMonitor() {
  if (_phaseStaleCheckTimer) return;
  _phaseStaleCheckTimer = setInterval(() => {
    const age = Date.now() - _lastPhaseUpdate;
    const isRunning = !['idle', 'stopped', 'error'].includes(_currentPhase);
    if (isRunning && age > 60000) {
      _renderPhaseBadge(true);
    }
  }, 5000);
}
_startPhaseStaleMonitor();
setPhase('idle', '');

// --- Result Buffer (W/L/T list) ---
// Append-only list of individual hand results.
// - feedRow: shows last 5
// - recentGrid: shows last 20
const MAX_FEED = 10;
const MAX_RECENT = 100;

function addResult(mark) {
  results.push(mark);
  renderFeed();
  renderRecent();
}

function renderFeed() {
  const row = $('#feedRow');
  const last = results.slice(-MAX_FEED);
  let html = '';
  for (const m of last) {
    const cls = m === 'W' ? 'win' : m === 'L' ? 'lose' : 'tie';
    html += `<span class="feed-dot ${cls}">${m}</span>`;
  }
  html += '<span class="feed-cursor"></span>';
  row.innerHTML = html;
}

function renderRecent() {
  const grid = $('#recentGrid');
  if (results.length === 0) {
    grid.innerHTML = '<div class="shoe-empty">Waiting for results...</div>';
    return;
  }
  const last = results.slice(-MAX_RECENT);
  let html = '';
  for (const m of last) {
    if (m === 'W') html += '<span class="mark-o">O</span>';
    else if (m === 'L') html += '<span class="mark-x">X</span>';
    else html += '<span class="mark-t">T</span>';
  }
  grid.innerHTML = html;
}

function resetFeed() {
  results.length = 0;
  renderFeed();
  renderRecent();
}

// --- Daily P&L tracking (JST timezone, per-round delta aggregation) ---
// Stored as { "YYYY-MM-DD": pnl_amount, ... }
function loadDailyPnl() {
  try { return JSON.parse(localStorage.getItem('valhalla_daily_pnl') || '{}'); }
  catch { return {}; }
}

function saveDailyPnl(data) {
  localStorage.setItem('valhalla_daily_pnl', JSON.stringify(data));
}

function todayKeyJST() {
  // JST = UTC+9
  const now = new Date();
  const utcMs = now.getTime() + (now.getTimezoneOffset() * 60000);
  const jst = new Date(utcMs + 9 * 3600000);
  return `${jst.getFullYear()}-${String(jst.getMonth()+1).padStart(2,'0')}-${String(jst.getDate()).padStart(2,'0')}`;
}

// 日付ロールオーバー時: 前日のPNL(=直前のdaily値)をlocalStorageに凍結
function _freezeDailyIfRollover(newDate) {
  if (!newDate || !_dailyOpenDate) return;
  if (_dailyOpenDate === newDate) return;
  // 前日の最終値を凍結 (currentBalance は直近更新前の値)
  if (_currentBalance !== null && _dailyOpenBalance !== null) {
    const prevPnl = _currentBalance - _dailyOpenBalance;
    const data = loadDailyPnl();
    data[_dailyOpenDate] = prevPnl;
    saveDailyPnl(data);
  }
}

function renderDailyPnl() {
  const row = $('#dailyRow');
  const data = loadDailyPnl();

  // Today value = 残高スナップショット計算 (updateSessionDisplay で更新済みだがここでも念のため)
  const today = _dailyOpenDate || todayKeyJST();
  const { daily } = _computePnl();
  const todayEl = $('#todayPnl');
  if (todayEl) {
    todayEl.textContent = `${daily >= 0 ? '+$' : '-$'}${Math.abs(daily).toFixed(0)}`;
    todayEl.className = 'today-pnl ' + (daily >= 0 ? 'positive' : 'negative');
  }
  // 今日の値もlocalStorage に反映 (リアルタイム凍結)
  data[today] = daily;
  saveDailyPnl(data);

  const keys = Object.keys(data).sort().slice(-14);
  if (keys.length === 0) {
    row.innerHTML = '<div class="daily-empty">No history yet</div>';
    return;
  }
  let html = '';
  for (const k of keys) {
    const v = data[k];
    const isPos = v >= 0;
    const mmdd = k.slice(5);
    html += `
      <div class="daily-item ${isPos ? 'positive' : 'negative'}">
        <div class="daily-date">${mmdd}</div>
        <div class="daily-pnl ${isPos ? 'positive' : 'negative'}">${isPos ? '+' : ''}$${v.toFixed(0)}</div>
      </div>
    `;
  }
  row.innerHTML = html;
}

// --- Agent Messages ---
window.valhalla.onAgentMessage((msg) => {
  switch (msg.type) {
    case 'action':
      setAction(msg.message || '');
      break;

    case 'phase':
      setPhase(msg.name || 'idle', msg.detail || '');
      break;

    case 'master_status': {
      _masterStatus = { ..._masterStatus, ...msg };
      _renderMasterStatus();
      const modal = $('#masterModal');
      if (modal && !modal.classList.contains('hidden')) _renderMasterModal();
      break;
    }

    case 'round_result': {
      const r = msg.result;
      const won = msg.won;
      _lastRoundWon = won;
      // Tieは完全無視。O/Xのみストリームへ即append (ローカル state で色決定)。
      if (r !== 'tie') {
        const streamMark = won === true ? 'O' : won === false ? 'X' : '';
        if (streamMark) _pushStreamMark(streamMark);
      }
      if (r === 'tie') {
        setAction('Tie -- BET returned');
        addResult('T');
      } else if (won === true) {
        flashScreen('win');
        addResult('W');
      } else if (won === false) {
        flashScreen('lose');
        addResult('L');
      }

      // Update balance (スナップショット方式: Python から届く balance と open で PNL 再計算)
      if (typeof msg.balance === 'number' && msg.balance > 0) {
        _currentBalance = msg.balance;
        $('#balance').textContent = `$${msg.balance.toFixed(2)}`;
      }
      if (typeof msg.session_open_balance === 'number' && msg.session_open_balance > 0) {
        _sessionOpenBalance = msg.session_open_balance;
      }
      // 日付ロールオーバー検出 → 前日PNLを凍結してから開く
      if (typeof msg.daily_open_date === 'string' && msg.daily_open_date) {
        _freezeDailyIfRollover(msg.daily_open_date);
        _dailyOpenDate = msg.daily_open_date;
      }
      if (typeof msg.daily_open_balance === 'number' && msg.daily_open_balance > 0) {
        _dailyOpenBalance = msg.daily_open_balance;
      }
      _persistBalanceSnapshot();
      updateSessionDisplay();
      renderDailyPnl();
      scheduleGuiStateSync();
      break;
    }

    case 'set_complete':
      // In dev mode, show a log line
      if (isDevMode()) {
        const s = msg;
        const sign = s.set_profit >= 0 ? '+' : '';
        addLog(`[DEV] Set #${s.set_index} done: ${s.wins}W/${s.losses}L ${sign}${s.set_profit}ch OS:${s.overshoot}`, 'info');
      }
      break;

    case 'shoe_history':
      // In dev mode, render all sets
      if (isDevMode() && Array.isArray(msg.sets)) {
        renderDevSets(msg.sets);
      }
      break;

    case 'test_status': {
      // Pattern Test mode: 別カウンタ表示 (sessionPNL等は更新しない)
      const tw = msg.wins || 0;
      const tl = msg.losses || 0;
      const tt = msg.ties || 0;
      const total = tw + tl;
      const wr = total > 0 ? ((tw / total) * 100).toFixed(1) : '0.0';
      const el = $('#testCounter');
      if (el) {
        el.style.display = 'block';
        el.textContent = `🧪 TEST: ${tw}W ${tl}L ${tt}T (${wr}%)`;
      }
      // 視覚フィードバック (フラッシュのみ、PNL は更新しない)
      if (msg.last_won === true) flashScreen('win');
      else if (msg.last_won === false) flashScreen('lose');
      break;
    }

    case 'status': {
      $('#betCount').textContent = `${msg.wins || 0}W / ${msg.losses || 0}L`;
      const totalBets = (msg.wins || 0) + (msg.losses || 0);
      if (totalBets > 0) {
        const wr = ((msg.wins || 0) / totalBets * 100).toFixed(1);
        $('#winRate').textContent = `${wr}%`;
      }
      if (typeof msg.balance === 'number' && msg.balance > 0) {
        _currentBalance = msg.balance;
        $('#balance').textContent = `$${msg.balance.toFixed(2)}`;
      }
      // 残高スナップショット (Python側の真実の源を受信)
      if (typeof msg.session_open_balance === 'number' && msg.session_open_balance > 0) {
        _sessionOpenBalance = msg.session_open_balance;
      }
      if (typeof msg.daily_open_date === 'string' && msg.daily_open_date) {
        _freezeDailyIfRollover(msg.daily_open_date);
        _dailyOpenDate = msg.daily_open_date;
      }
      if (typeof msg.daily_open_balance === 'number' && msg.daily_open_balance > 0) {
        _dailyOpenBalance = msg.daily_open_balance;
      }
      _persistBalanceSnapshot();
      updateSessionDisplay();
      renderDailyPnl();
      // OS (overshoot) tag in BETS card (removed from HTML, guard with null check)
      if (typeof msg.overshoot === 'number') {
        const osEl = $('#osValue');
        if (osEl) {
          const os = msg.overshoot;
          osEl.textContent = `OS ${os}`;
          osEl.className = 'os-tag ' + (os === 0 ? '' : os <= 2 ? 'safe' : os <= 4 ? 'warn' : 'danger');
        }
      }
      // Developer panel
      updateDevPanel(msg);
      break;
    }

    case 'session_reset': {
      const rawAmt = (msg.amount_actual ?? msg.amount);
      let amt = (typeof rawAmt === 'number') ? rawAmt : parseFloat(rawAmt);
      if (!Number.isFinite(amt)) {
        amt = 0;
      }
      if (amt === 0 && sessionTotal !== 0) {
        amt = sessionTotal;
      }
      const isProfit = msg.is_profit;
      const title = isProfit ? 'PROFIT TARGET HIT' : 'LOSS CUT';
      const sign = amt >= 0 ? '+' : '-';
      showResetToast(title, `${sign}$${Math.abs(amt).toFixed(0)}`, isProfit);
      flashScreen(isProfit ? 'profit' : 'losscut');
      addLog(`=== ${title} ===  ${sign}$${Math.abs(amt).toFixed(0)}`, isProfit ? 'win' : 'lose');
      // 残高スナップショット方式: session_open_balance は Python 側で現残高に更新済み。
      // 次の status/round_result で新しい session_open_balance が届いてPNL=0に戻る。
      // 暫定的にローカルでも 0 にしておく (即時反映のため)。
      if (_currentBalance !== null) {
        _sessionOpenBalance = _currentBalance;
      }
      _persistBalanceSnapshot();
      updateSessionDisplay();
      break;
    }

    case 'error':
      addLog(`Error: ${msg.message}`, 'lose');
      setPhase('error', (msg.message || '').slice(0, 40));
      break;

    case 'stopped':
      // START直後(3秒以内)の stopped は旧プロセスの遅延シグナル→無視
      if (_startedAt && Date.now() - _startedAt < 3000) {
        console.log('[UI] Ignoring stale stopped signal from old process');
        break;
      }
      setRunning(false);
      setAction('Stopped');
      setPhase('stopped', '');
      addLog('Bot stopped.');
      break;

    case 'started':
      // 自動再起動 (watchdog / Electron auto-restart) で新しい Python が起動した時
      // ボタン状態 (START disabled / STOP enabled) を再同期
      _startedAt = Date.now();
      setRunning(true);
      setAction('Running (auto-restarted)');
      addLog('🔄 Bot auto-restarted', 'info');
      break;

    case 'log':
      addLog(msg.message || '');
      break;

    case 'mode_changed': {
      const nextMode = normalizeBetMode(msg.mode);
      // Settings モーダル操作中に backend 側の mode_changed で select が上書きされると、
      // ユーザーが選んだ値が SAVE 時に失われることがあるため、開いている間は触らない。
      const modal = $('#settingsModal');
      const modalOpen = modal && !modal.classList.contains('hidden');
      if (!modalOpen && $('#inputBetMode')) $('#inputBetMode').value = nextMode;
      _streamSetSize = _setSizeForMode(nextMode);
      addLog(`BET mode → ${nextMode}`, 'info');
      break;
    }

    default:
      if (msg.message) addLog(msg.message);
      break;
  }
});

window.valhalla.onAgentLog((text) => {
  text.trim().split('\n').forEach(line => { if (line.trim()) addLog(line); });
});

// --- Init ---
sessionTotal = 0;
results.length = 0;
setRunning(false);
// Restore previous state (for resume / auto-arm)
restoreSessionState();
_restoreBalanceSnapshot();
updateSessionDisplay();
setAction('Ready. Sign in to begin.');
addLog('BACOPYRECEIVER ready.', 'info');
renderDailyPnl();
renderFeed();
renderRecent();
applyDevMode();
initModalTabs();
initAuth();
