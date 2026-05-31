

// === BACOPY renderer build marker + debug proof object ====================
// Open Electron DevTools (Ctrl+Shift+I) and verify these in the console:
//   window.__bacopyDebug.build       -> proves which app.js the renderer loaded
//   window.__bacopyDebug.agentCounts -> per-msg-type receive count
//   window.__bacopyDebug.lastMsg     -> last full agent-message payload
//   window.__bacopyDebug.panelProof  -> latest #manualAssistPanel rect/visibility
const __BACOPY_RENDERER_BUILD = 'manual-assist-proof-2026-05-28';
window.__bacopyDebug = window.__bacopyDebug || {
  build: __BACOPY_RENDERER_BUILD,
  agentCounts: {},
  lastMsg: null,
  lastManualMode: null,
  lastManualItem: null,
  panelProof: null,
  errors: [],
};
console.log('[BACOPY-RENDERER] loaded build=' + __BACOPY_RENDERER_BUILD);

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let isRunning = false;
let logVisible = true;
let _billingOk = false;

let _billingNetworkFailCount = 0;

const _BILLING_FAIL_TOLERANCE = 3;

let _balanceEmptyFirstAt = null;

const _BALANCE_GRACE_MS = 30 * 60 * 1000;

function _setAnimationsActive(active) {
  document.body.classList.toggle('animations-on', active);
}
window.addEventListener('focus', () => _setAnimationsActive(true));
window.addEventListener('blur', () => _setAnimationsActive(false));

if (document.hasFocus()) _setAnimationsActive(true);

$('#btnMinimize').addEventListener('click', () => window.valhalla.windowMinimize());
$('#btnMaximize').addEventListener('click', () => window.valhalla.windowMaximize());
$('#btnClose').addEventListener('click', () => window.valhalla.windowClose());

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
  const card = $('#masterCard');
  const valueEl = $('#masterCardValue');
  if (!card || !valueEl) return;

  card.classList.remove('master-online', 'master-active', 'master-offline', 'master-unknown');
  let label = '--';
  if (_masterStatus.connected === true) {
    if (_masterStatus.active) {
      card.classList.add('master-active');
      label = 'ACTIVE';
    } else {
      card.classList.add('master-online');
      label = 'ONLINE';
    }
  } else if (_masterStatus.connected === false) {
    card.classList.add('master-offline');
    label = 'OFFLINE';
  } else {
    card.classList.add('master-unknown');
  }
  valueEl.textContent = label;
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

$('#masterCard')?.addEventListener('click', () => {
  $('#masterModal')?.classList.remove('hidden');
  _renderMasterModal();
});
$('#masterClose')?.addEventListener('click', () => $('#masterModal')?.classList.add('hidden'));

function _msUntilNextJstMidnight() {
  const now = Date.now();
  const jst = new Date(now + 9 * 60 * 60 * 1000);
  const next = new Date(jst);
  next.setUTCHours(0, 0, 5, 0);

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



    if (b && b.network_error) {
      _billingNetworkFailCount++;
      if (!silent) addLog(`CREDIT: ネットワーク障害 (${_billingNetworkFailCount}回目)。稼働継続。`, 'warn');


      return b;
    }
    _billingNetworkFailCount = 0;



    if (b && b.balance_empty && isRunning) {
      if (!_balanceEmptyFirstAt) {
        _balanceEmptyFirstAt = Date.now();
        addLog(`CREDIT: 残高0を検出。${_BALANCE_GRACE_MS / 60000}分以内にチャージしてください。`, 'warn');
      }
      const elapsed = Date.now() - _balanceEmptyFirstAt;
      if (elapsed < _BALANCE_GRACE_MS) {


        const remaining = Math.ceil((_BALANCE_GRACE_MS - elapsed) / 60000);
        if (!silent) addLog(`CREDIT: 残高0 猶予中 残り${remaining}分`, 'warn');
        _billingOk = false;
        const el = $('#creditBalance');
        if (el) { el.textContent = `$0.00 (猶予${remaining}分)`; el.className = 'stat-value negative'; }
        return b;
      }


    } else if (b && b.ok) {
      _balanceEmptyFirstAt = null;

    }

    _billingOk = !!(b && b.ok);
    const el = $('#creditBalance');
    if (el) {
      if (b && b.is_free) el.textContent = 'FREE / UNLIMITED';
      else if (b && typeof b.balance === 'number') el.textContent = `$${b.balance.toFixed(2)}`;
      else el.textContent = '-';
      el.className = 'stat-value ' + (_billingOk ? 'positive' : 'negative');
    }


    if (!isRunning) {
      const startBtn = $('#btnStart');
      if (startBtn) startBtn.disabled = !_billingOk;
    }
    if (!_billingOk && !silent && b && b.reason) {
      addLog(`CREDIT: ${b.reason}`, 'warn');
    }


    if (!_billingOk && isRunning && b && !b.network_error && !b.balance_empty) {
      await stopBotFlow({ forced: true, reason: b.reason || 'Credit is not active' });
      showSetup(b.reason || 'Credit is not active');
    }


    if (!_billingOk && isRunning && b && b.balance_empty) {
      addLog('CREDIT: 残高0の猶予期間が終了しました。ボットを停止します。', 'err');
      await stopBotFlow({ forced: true, reason: b.reason || 'Balance is empty' });
      showSetup(b.reason || 'Balance is empty');
    }
    return b || { ok: false, reason: 'Unknown billing status' };
  } catch (e) {


    _billingNetworkFailCount++;
    if (!silent) addLog(`Billing check failed (${_billingNetworkFailCount}): ${e.message || e}`, 'warn');
    return { ok: false, network_error: true, reason: 'Billing check failed' };
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


    window.__authEmail = sess.email || '';
    window.__authUserId = sess.user_id || '';
    const b = await refreshBilling({ silent: true });
    if (!b.ok) {
      showSetup(b.reason || 'Credit is not active');
      return;
    }
    showMain();
    startBillingMonitors();








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
    window.__authEmail = res.email || email || '';
    window.__authUserId = res.user_id || '';
    const b = await refreshBilling({ silent: true });
    if (!b.ok) {
      showSetup(b.reason || 'Credit is not active');
      return;
    }
    showMain();
    startBillingMonitors();






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

let _startedAt = 0;

let _currentBalance = null;

let _sessionOpenBalance = null;

let _dailyOpenBalance = null;

let _dailyOpenDate = null;

let sessionTotal = 0;
let _balanceConfirmed = false; // エンジンから実残高を受信したら true
const results = [];
const manualAssistItems = [];
let manualAssistEnabled = false;
let manualAssistEnabledAt = 0;
let activeManualItemId = '';

function _jstDateStrNow() {
  const now = new Date();
  const utcMs = now.getTime() + (now.getTimezoneOffset() * 60000);
  const jst = new Date(utcMs + 9 * 3600000);
  return `${jst.getFullYear()}-${String(jst.getMonth()+1).padStart(2,'0')}-${String(jst.getDate()).padStart(2,'0')}`;
}

function _isValidBalance(v) {
  return typeof v === 'number' && Number.isFinite(v) && v > 0;
}

function _computePnl() {






  if (!_isValidBalance(_currentBalance)) return { session: 0, daily: 0 };


  const today = _jstDateStrNow();
  if (_dailyOpenDate !== today) {


    _dailyOpenDate = today;
    _dailyOpenBalance = _currentBalance;
    _persistBalanceSnapshot();
  }
  const session = _isValidBalance(_sessionOpenBalance) ? (_currentBalance - _sessionOpenBalance) : 0;
  const daily = _isValidBalance(_dailyOpenBalance) ? (_currentBalance - _dailyOpenBalance) : 0;
  sessionTotal = session;

  return { session, daily };
}

function _persistBalanceSnapshot() {
  try {


    const payload = {};
    if (_isValidBalance(_currentBalance)) payload.current = _currentBalance;
    if (_isValidBalance(_sessionOpenBalance)) payload.session_open = _sessionOpenBalance;
    if (_isValidBalance(_dailyOpenBalance)) payload.daily_open = _dailyOpenBalance;
    if (typeof _dailyOpenDate === 'string' && _dailyOpenDate) payload.daily_date = _dailyOpenDate;


    if (Object.keys(payload).length === 0) return;


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
    user_email: (window.__authEmail || ''),
    user_id: (window.__authUserId || ''),
    bet_mode: normalizeBetMode(s.bet_mode),
    resume_results: Array.isArray(results) ? results.slice() : [],
  };
}

async function startBotFlow({ auto = false } = {}) {
  if (!_billingOk) {
    showSetup('Credit is not active. Please charge at bafather.uk.');
    return;
  }
  setManualStop(false);

  const settings = loadSettings();
  const selectedBetMode = normalizeBetMode(settings.bet_mode);
  const isDL = isDualLineBetMode(selectedBetMode) || settings.mode === 'dual_line'
    || settings.mode === 'dual_line_assist'
    || settings.mode === 'dual_line_auto' || settings.mode === 'dual_line_manual';
  const isDLAssist = isDualLineAssistBetMode(selectedBetMode) || settings.mode === 'dual_line'
    || settings.mode === 'dual_line_assist'
    || settings.mode === 'dual_line_manual';
  manualAssistEnabled = !!isDLAssist;
  if (manualAssistEnabled) manualAssistEnabledAt = Date.now();
  renderManualAssistPanel();
  const config = {
    ...buildStartConfig(),
    mode: isDL ? (isDLAssist ? 'dual_line_assist' : 'dual_line_auto') : 'executor',
    headless: isDL ? false : !!settings.headless,
    live: isDLAssist ? true : (settings.dual_live || false),
    money_mode: settings.dual_money_mode || 'flat',
    money_unit: settings.dual_unit || 100,
    dual_mode: settings.dual_mode || 'v3',
    on_limit: settings.dual_on_limit || 'stop',
    manual_assist_auto_click: !!settings.manual_assist_auto_click,
  };
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
    manualAssistItems.length = 0;
    activeManualItemId = '';
    renderManualAssistPanel();


    try {
      const logEl = document.getElementById('logContent');
      if (logEl) logEl.innerHTML = '';
    } catch (_) {}


    try {
      const sig = document.getElementById('sigStream');
      if (sig) sig.innerHTML = '';
      _streamSetIdx = 0;
      _streamTurnsInSet = 0;
      _lastRoundWon = null;
    } catch (_) {}


    try {
      ['sigCycle','sigRatio','sigDrift','sigRound'].forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.textContent = '--'; el.style.color = ''; }
      });
    } catch (_) {}


    try {
      const lf = document.getElementById('feedList') || document.getElementById('liveFeed');
      if (lf) lf.innerHTML = '';
    } catch (_) {}


    try {
      localStorage.removeItem('valhalla_session_state');
      localStorage.removeItem('valhalla_recent_results');
      localStorage.removeItem('valhalla_set_history');
    } catch (_) {}


    try {
      setPhase('idle', 'new session — waiting for master signal');
      setAction('NEW SESSION started');
    } catch (_) {}


    try {
      const fl = document.getElementById('flashOverlay');
      if (fl) fl.className = 'flash-overlay';
    } catch (_) {}
    addLog('=== NEW SESSION — history cleared ===', 'info');
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
  if (!_balanceConfirmed) {
    el.textContent = '--';
    el.className = 'stat-value';
  } else {
    el.textContent = `$${session >= 0 ? '+' : ''}${session.toFixed(2)}`;
    el.className = 'stat-value ' + (session >= 0 ? 'positive' : 'negative');
  }

  const todayEl = $('#todayPnl');
  if (todayEl) {
    if (!_balanceConfirmed) {
      todayEl.textContent = '--';
      todayEl.className = 'today-pnl';
    } else {
      todayEl.textContent = `${daily >= 0 ? '+$' : '-$'}${Math.abs(daily).toFixed(0)}`;
      todayEl.className = 'today-pnl ' + (daily >= 0 ? 'positive' : 'negative');
    }
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






      localStorage.removeItem('valhalla_session_state');
      sessionTotal = 0;
      _sessionOpenBalance = null;



      _persistBalanceSnapshot();
      updateSessionDisplay();
      resetFeed();
      cleanup();
      resolve('reset');
    };
    $('#continueClose').onclick = () => { cleanup(); resolve('cancel'); };
  });
}

const DEFAULT_SETTINGS = {
  chip_base: 1,
  profit_target: 50,
  profit_session_limit: 0,
  loss_cut: 200,
  executor_id: 'gui-1',
  executor_label: 'MAIN-PC',
  stake_username: '',








  table_name_substr: '',
  auto_click_wait_sec: 90,
  allow_switch_table: true,
  allow_banker: true,

  allow_tie: false,

  assume_bc_012: true,

  headless: false,
  dry_run: false,
  bet_mode: 'flat_1usd',
  mode: 'executor',
  dual_money_mode: 'flat',
  dual_unit: 100,
  dual_mode: 'v3',
  dual_live: false,
  dual_on_limit: 'stop',
  manual_assist_auto_click: false,
};
const ALLOWED_BET_MODES = new Set(['flat_1usd', 'seq_user10', 'newseq', 'newseq30', 'small3', 'small02', 'small1', 'small6', 'dual_line', 'dual_line_assist', 'dual_line_auto']);

function normalizeBetMode(mode) {
  return ALLOWED_BET_MODES.has(mode) ? mode : 'flat_1usd';
}

function isDualLineBetMode(mode) {
  return mode === 'dual_line' || mode === 'dual_line_assist' || mode === 'dual_line_auto';
}

function isDualLineAssistBetMode(mode) {
  return mode === 'dual_line' || mode === 'dual_line_assist';
}

function _ensureDualLineOption() {
  const sel = $('#inputBetMode');
  if (!sel) return;
  let hasDual = false;
  let hasAuto = false;
  for (const opt of sel.options) {
    if (opt.value === 'dual_line') {
      hasDual = true;
      opt.textContent = 'DUAL-LINE Assist — manual final BET';
    }
    if (opt.value === 'dual_line_auto') hasAuto = true;
  }
  if (!hasDual) {
    const opt = document.createElement('option');
    opt.value = 'dual_line';
    opt.textContent = 'DUAL-LINE Assist — manual final BET';
    sel.appendChild(opt);
  }
  if (!hasAuto) {
    const opt = document.createElement('option');
    opt.value = 'dual_line_auto';
    opt.textContent = 'DUAL-LINE Auto — experimental';
    sel.appendChild(opt);
  }
}
// SETTINGS モーダル開く前に dual_line option を追加
document.addEventListener('DOMContentLoaded', _ensureDualLineOption);
// betMode 変更時に dual-line 設定の表示切替
setTimeout(() => {
  $('#inputBetMode')?.addEventListener('change', function() {
    const isDL = isDualLineBetMode(this.value);
    if ($('#dualLineMoneyGroup')) $('#dualLineMoneyGroup').style.display = isDL ? '' : 'none';
    if ($('#chipBaseGroup')) $('#chipBaseGroup').style.display = isDL ? 'none' : '';
    if ($('#inputDryRun')) {
      // dual-line では dry_run → dual_live の逆
      $('#inputDryRun').checked = !isDL || !($('#inputDualLive')?.checked);
    }
  });
  $('#inputDualLive')?.addEventListener('change', function() {
    if ($('#inputDryRun')) $('#inputDryRun').checked = !this.checked;
  });
}, 100);

function normalizeProfitSessionLimit(value) {
  const n = Number.isFinite(Number(value)) ? Math.floor(Number(value)) : 0;
  return n >= 0 ? n : 0;
}

const SITE_URL = 'https://bafather.uk';
const LAPLACE_API_KEY = '';

let _paramCandidates = [];

function _formatParamCandidate(c, idx) {


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

let _guiSyncTimer = null;
let _guiSyncPending = false;

function _getGuiState() {




  const payload = {


    session_total: sessionTotal,
    daily_pnl: loadDailyPnl(),
    results: results.slice(-200),
    bet_mode: normalizeBetMode(loadSettings().bet_mode),
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

function _settingsSwitchTab(which) {
  $$('.modal-tab').forEach(t => t.classList.remove('active'));
  $$('.tab-content').forEach(c => c.classList.add('hidden'));
  if (which === 'system') {
    $('#tabSystemBtn')?.classList.add('active');
    $('#tabSystemContent')?.classList.remove('hidden');
    _refreshSupportInfo();

  } else {
    $('#tabBotBtn')?.classList.add('active');
    $('#tabBotContent')?.classList.remove('hidden');
  }
}
$('#tabBotBtn')?.addEventListener('click', () => _settingsSwitchTab('bot'));
$('#tabSystemBtn')?.addEventListener('click', () => _settingsSwitchTab('system'));

function initModalTabs() {


  _settingsSwitchTab('bot');
}

function _settingsToast(msg, type = 'info') {
  let el = $('#settingsToast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'settingsToast';
    el.className = 'settings-toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.remove('toast-info', 'toast-win', 'toast-lose');
  el.classList.add(type === 'win' ? 'toast-win' : type === 'lose' ? 'toast-lose' : 'toast-info');
  el.classList.add('show');
  clearTimeout(_settingsToast._tm);
  _settingsToast._tm = setTimeout(() => el.classList.remove('show'), 3600);
}

async function _refreshSupportInfo() {
  try {
    if (!window.bacopy?.getSupportInfo) return;
    const info = await window.bacopy.getSupportInfo();
    const $e = $('#supportIdEmail'), $p = $('#supportIdPort'), $s = $('#supportIdStatus');
    if ($e) $e.textContent = info.email || '—';
    if ($p) $p.textContent = info.port ? `${info.port} (接続待ち)` : '—';
    if ($s) {
      const isRunning = info.tunnel_status === 'running';
      if (isRunning) {
        $s.textContent = '接続中 ✓';
        $s.style.color = 'var(--win)';
      } else if (info.last_error) {
        $s.textContent = `停止 (${info.fail_count}回失敗: ${info.last_error.slice(0, 60)})`;
        $s.style.color = 'var(--lose)';
      } else {
        $s.textContent = '停止';
        $s.style.color = 'var(--text-muted)';
      }
    }
  } catch (e) {
    console.warn('[Settings] getSupportInfo failed:', e);
  }
}

$('#btnSettings')?.addEventListener('click', async () => {
  $('#settingsModal')?.classList.remove('hidden');
  const s = loadSettings();
  if ($('#inputBetMode')) $('#inputBetMode').value = normalizeBetMode(s.bet_mode);
  $('#inputChipBase').value = s.chip_base;

  // dual-line 設定の表示切替（assist/auto も含めて group を表示する）
  const isDL = isDualLineBetMode(normalizeBetMode($('#inputBetMode')?.value));
  if ($('#dualLineMoneyGroup')) $('#dualLineMoneyGroup').style.display = isDL ? '' : 'none';
  if ($('#inputDualMoneyMode')) $('#inputDualMoneyMode').value = s.dual_money_mode || 'flat';
  if ($('#inputDualUnit')) $('#inputDualUnit').value = s.dual_unit || 100;
  if ($('#inputDualLive')) $('#inputDualLive').checked = !!s.dual_live;
  if ($('#inputManualAssistAutoClick')) $('#inputManualAssistAutoClick').checked = !!s.manual_assist_auto_click;
  if ($('#inputOnLimitRestart')) $('#inputOnLimitRestart').checked = s.dual_on_limit === 'restart';
  // dual-line 時 chip_base グループを非表示
  if ($('#chipBaseGroup')) $('#chipBaseGroup').style.display = isDL ? 'none' : '';
  // dual-line 時 BET MODE に dual_line を追加
  _ensureDualLineOption();
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
  if ($('#inputAllowBanker')) $('#inputAllowBanker').checked = !!s.allow_banker;
  if ($('#inputAllowTie')) $('#inputAllowTie').checked = !!s.allow_tie;
  if ($('#inputAssumeBc012')) $('#inputAssumeBc012').checked = !!s.assume_bc_012;
  $('#inputHeadless').checked = !!s.headless;



  try {
    if (window.bacopy?.getSettings) {
      const remote = await window.bacopy.getSettings();
      if ($('#inputTelegramToken')) $('#inputTelegramToken').value = remote.telegram_bot_token || '';
      if ($('#inputTelegramChat'))  $('#inputTelegramChat').value  = remote.telegram_chat_id || '';
      if ($('#inputSupportToggle')) {
        const v = String(remote.support_enabled || '0').toLowerCase();
        $('#inputSupportToggle').checked = ['1', 'true', 'yes'].includes(v);
      }
    }
  } catch (e) {
    console.warn('[Settings] getSettings failed:', e);
  }



  _settingsSwitchTab('bot');
});
$('#settingsClose')?.addEventListener('click', () => $('#settingsModal')?.classList.add('hidden'));

$('#btnTestTelegram')?.addEventListener('click', async () => {
  try {


    const token = $('#inputTelegramToken')?.value.trim() || '';
    const chat  = $('#inputTelegramChat')?.value.trim()  || '';
    if (!token || !chat) {
      _settingsToast('Token と Chat ID を入力してください', 'lose');
      return;
    }
    if (window.bacopy?.saveSettings) {
      await window.bacopy.saveSettings({ telegram_bot_token: token, telegram_chat_id: chat });
    }
    _settingsToast('Telegram にテスト送信中...', 'info');
    const res = await window.bacopy.testTelegram();
    if (res?.ok) _settingsToast('Telegram OK — メッセージ届きました', 'win');
    else _settingsToast(`Telegram NG: ${res?.error || 'unknown error'}`, 'lose');
  } catch (e) {
    _settingsToast(`Test 失敗: ${e.message || e}`, 'lose');
  }
});

$('#inputSupportToggle')?.addEventListener('change', async (ev) => {
  const on = !!ev.target.checked;
  try {
    if (window.bacopy?.toggleSupport) await window.bacopy.toggleSupport(on);
    _settingsToast(on ? 'リモート支援: 有効' : 'リモート支援: 無効', 'info');
    _refreshSupportInfo();
  } catch (e) {
    _settingsToast(`Support toggle failed: ${e.message || e}`, 'lose');
  }
});

$('#btnInstallDeps')?.addEventListener('click', async () => {
  try {
    _settingsToast('セットアップを起動中... (UAC 承認)', 'info');
    const res = await window.bacopy.installDeps();
    if (res?.ok) _settingsToast('Setup launched. OPEN SETUP LOG で進捗確認.', 'win');
    else _settingsToast(`Install 失敗: ${res?.error || 'unknown'}`, 'lose');
  } catch (e) {
    _settingsToast(`Install 失敗: ${e.message || e}`, 'lose');
  }
});

$('#btnOpenSetupLog')?.addEventListener('click', async () => {
  try {
    const res = await window.bacopy.openSetupLog();
    if (!res?.ok) _settingsToast(`Log open 失敗: ${res?.error || 'not found'}`, 'lose');
  } catch (e) {
    _settingsToast(`Log open 失敗: ${e.message || e}`, 'lose');
  }
});

if (window.bacopy?.onInstallDepsResult) {
  window.bacopy.onInstallDepsResult((data) => {
    const msg = data?.message || (data?.success ? 'Setup launched' : 'Setup failed');
    _settingsToast(msg, data?.success ? 'win' : 'lose');
  });
}
$('#btnSaveSettings')?.addEventListener('click', async () => {




  try { document.activeElement?.blur?.(); } catch {}
  await new Promise((r) => setTimeout(r, 0));

  const selectedBetMode = normalizeBetMode($('#inputBetMode')?.value);
  const isDualLine = isDualLineBetMode(selectedBetMode);
  const isDualLineAssist = isDualLineAssistBetMode(selectedBetMode);
  const settings = {
    // bet_mode が金額を決めるため chip_base は固定 (UIも非表示)
    chip_base: isDualLine ? parseFloat($('#inputDualUnit')?.value || 100) : 1,
    profit_target: (v => Number.isFinite(v) ? v : 50)(parseFloat($('#inputProfitTarget').value)),
    profit_session_limit: normalizeProfitSessionLimit($('#inputProfitSessionLimit')?.value),
    loss_cut: (v => Number.isFinite(v) ? v : 200)(parseFloat($('#inputLossCut').value)),
    dry_run: $('#inputDryRun').checked,
    bet_mode: selectedBetMode,
    executor_id: $('#inputExecutorId').value.trim(),
    executor_label: $('#inputExecutorLabel').value.trim(),
    stake_username: $('#inputStakeUsername').value.trim(),
    table_name_substr: $('#inputTableNameSubstr').value.trim(),
    auto_click_wait_sec: parseInt($('#inputAutoClickWaitSec').value, 10) || 90,
    allow_switch_table: $('#inputAllowSwitchTable').checked,
    allow_banker: $('#inputAllowBanker')?.checked,
    allow_tie: $('#inputAllowTie')?.checked,
    assume_bc_012: $('#inputAssumeBc012')?.checked,
    headless: isDualLine ? false : $('#inputHeadless').checked,
    // dual-line settings
    mode: isDualLine ? (isDualLineAssist ? 'dual_line_assist' : 'dual_line_auto') : 'executor',
    dual_money_mode: $('#inputDualMoneyMode')?.value || 'flat',
    dual_unit: parseFloat($('#inputDualUnit')?.value || 100),
    dual_mode: $('#inputDualMode')?.value || 'v3',
    dual_live: $('#inputDualLive')?.checked || false,
    dual_on_limit: $('#inputOnLimitRestart')?.checked ? 'restart' : 'stop',
    manual_assist_auto_click: $('#inputManualAssistAutoClick')?.checked || false,
  };

  localStorage.setItem('bacopy_settings', JSON.stringify(settings));



  try {
    if (window.bacopy?.saveSettings) {
      const envPayload = {
        telegram_bot_token: $('#inputTelegramToken')?.value.trim() || '',
        telegram_chat_id:   $('#inputTelegramChat')?.value.trim()  || '',
      };


      await window.bacopy.saveSettings(envPayload);
    }
  } catch (e) {
    console.warn('[Settings] env save failed:', e);
  }

  $('#settingsModal')?.classList.add('hidden');
  addLog(`Settings saved. Mode:${settings.bet_mode} Target:$${settings.profit_target} LossCut:$${settings.loss_cut}`, 'info');
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




    if (String(merged.table_name_substr || '').trim() === 'Speed Baccarat') {
      merged.table_name_substr = '';
    } else {
      merged.table_name_substr = String(merged.table_name_substr || '');
    }
    merged.auto_click_wait_sec = Number.isFinite(Number(merged.auto_click_wait_sec)) ? Math.max(10, Math.floor(Number(merged.auto_click_wait_sec))) : DEFAULT_SETTINGS.auto_click_wait_sec;




    merged.allow_switch_table = true;
    merged.allow_banker = true;
    merged.allow_tie = false;
    merged.assume_bc_012 = true;
    merged.headless = !!merged.headless;
    return merged;
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

const DEV_PASSWORD = 'laplace1749';

function isDevMode() {


  return true;
}

function setDevMode(on) {


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

const _SIG_PREFIXES = 'CDEFG';
const _SIG_WL_PREFIXES = 'QRSTM';
const _SIG_OS_PREFIXES = 'UVWXY';
function _rndChar(s) { return s[Math.floor(Math.random() * s.length)]; }

const _sigCodeCache = { turn: { key: null, code: '' }, ratio: { key: null, code: '' }, drift: { key: null, code: '' } };
function _turnToCode(turn) {
  const key = String(turn);
  if (_sigCodeCache.turn.key !== key) {
    _sigCodeCache.turn.key = key;
    _sigCodeCache.turn.code = _rndChar(_SIG_PREFIXES) + String.fromCharCode(65 + Math.max(0, turn - 1));
  }
  return _sigCodeCache.turn.code;
}
function _ratioToCode(w, l) {
  const key = w + '/' + l;
  if (_sigCodeCache.ratio.key !== key) {
    _sigCodeCache.ratio.key = key;
    _sigCodeCache.ratio.code = _rndChar(_SIG_WL_PREFIXES) + w + _rndChar(_SIG_WL_PREFIXES) + l;
  }
  return _sigCodeCache.ratio.code;
}
function _driftToCode(os) {
  const key = String(os);
  if (_sigCodeCache.drift.key !== key) {
    _sigCodeCache.drift.key = key;
    _sigCodeCache.drift.code = _rndChar(_SIG_OS_PREFIXES) + os;
  }
  return _sigCodeCache.drift.code;
}

const _STREAM_SET_COLORS = ['#ff3366', '#ffcc00', '#00b8d4', '#ffffff', '#00ff88', '#c084fc'];
function _setSizeForMode(mode) { return 7; } // 全モード7ターン制
let _streamSetSize = _setSizeForMode(normalizeBetMode(loadSettings().bet_mode));
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
  requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
}

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
  if (el && el.querySelector('[style*="rgba"]')) el.innerHTML = '';

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
  msg = msg || {};
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


  if (srd) {
    const roundNum = _streamSetIdx * _streamSetSize + _streamTurnsInSet;
    srd.textContent = `#${roundNum}`;
    srd.style.color = _currentSetColor();
  }
}

function logSeqProbe(source, ms, extra = {}) {
  try {
    const s = ms || {};
    const turns = Array.isArray(s.seq7_current_turns) ? s.seq7_current_turns.join('') : '';
    console.log(
      `[AUTO-PROBE-GUI] ${source} ` +
      `next=${Number(s.next_bet || 0).toFixed(2)} ` +
      `mode=${s.mode || '-'} seq_turn=${s.seq_turn ?? '-'} ` +
      `overshoot=${s.seq_overshoot ?? '-'} turns=${turns} ` +
      `pnl=${Number(s.session_pnl || 0).toFixed(2)} ` +
      `extra=${JSON.stringify(extra || {})}`
    );
  } catch (e) {
    console.log('[AUTO-PROBE-GUI] log failed', e && e.message);
  }
}

function renderDevSets(sets, current_turns) {




  if (!isDevMode()) return;
  const el = $('#sigStream');
  if (!el) return;
  const list = Array.isArray(sets) ? sets : [];
  const ct = Array.isArray(current_turns) ? current_turns.filter(c => c === 'O' || c === 'X') : [];

  if (el.children.length > 0 && _streamSetIdx === list.length && _streamTurnsInSet === ct.length) return;

  el.innerHTML = '';
  for (let i = 0; i < list.length; i += 1) {
    const results = (list[i] && list[i].results) || '';
    const color = _STREAM_SET_COLORS[i % _STREAM_SET_COLORS.length];
    for (const c of results) {
      if (c === 'O' || c === 'X') _appendStreamMark(el, c, color);
    }
  }
  // 進行中セットの途中結果も描画 (recover_session 後に○×が消える問題の修正)
  if (ct.length > 0) {
    const color = _STREAM_SET_COLORS[list.length % _STREAM_SET_COLORS.length];
    for (const c of ct) _appendStreamMark(el, c, color);
  }
  _streamSetIdx = list.length;
  _streamTurnsInSet = ct.length;
}

// Set the SIGNAL PANEL header, the 4 cell labels and the stream label so the
// panel reflects whatever money-management mode is active.
function _setSigPanel(title, labels, streamLabel) {
  const t = $('#devPanelTitle'); if (t) t.textContent = title;
  const ids = ['#sigLabel1', '#sigLabel2', '#sigLabel3', '#sigLabel4'];
  for (let i = 0; i < 4; i += 1) {
    const el = $(ids[i]);
    if (el) el.textContent = labels[i] || '';
  }
  const sl = $('#sigStreamLabel'); if (sl) sl.textContent = streamLabel;
}

function _fmtAmt(v) {
  return (v % 1 === 0) ? v.toFixed(0) : v.toFixed(1);
}

// Build the bet ladder for martingale (unit*2^k) / dalembert (unit*(k+1)),
// marking the current level. Window is sized around the current level.
function _betLadder(mode, unit, level) {
  const u = Number(unit) || 1;
  const cur = Math.max(0, Number(level) || 0);
  const span = Math.max(cur + 3, 7);
  const out = [];
  for (let k = 0; k <= span; k += 1) {
    const amount = mode === 'martingale' ? u * Math.pow(2, k) : u * (k + 1);
    out.push({ amount, current: k === cur });
    if (mode === 'martingale' && k >= 12) break; // cap runaway martingale ladder
  }
  return out;
}

// Render the live progression panel for martingale / dalembert in place of the
// SEQ ○× stream (which is meaningless for these modes).
function renderProgressionPanel(ms, mode) {
  if (!isDevMode()) return;
  const unit = Number(ms.unit) || 1;
  const level = Number(ms.loss_count) || 0;
  const nextBet = Number(ms.next_bet) || 0;
  const pnl = Number(ms.session_pnl) || 0;
  if (mode === 'martingale') {
    _setSigPanel('MARTINGALE', ['LOSSES', 'NEXT $', 'MAX $', 'PNL $'], 'BET LADDER (x2)');
    const sd = $('#sigDrift');
    if (sd) sd.textContent = (Number(ms.martingale_max_bet) > 0) ? _fmtAmt(Number(ms.martingale_max_bet)) : '--';
  } else {
    _setSigPanel("D'ALEMBERT", ['LEVEL', 'NEXT $', 'STEP $', 'PNL $'], 'BET LADDER (+1u)');
    const sd = $('#sigDrift');
    if (sd) sd.textContent = _fmtAmt(unit);
  }
  const sc = $('#sigCycle'); if (sc) sc.textContent = String(level);
  const sr = $('#sigRatio'); if (sr) sr.textContent = _fmtAmt(nextBet);
  const srd = $('#sigRound');
  if (srd) { srd.textContent = _fmtAmt(pnl); srd.style.color = pnl >= 0 ? '#00ff88' : '#ff3366'; }
  const el = $('#sigStream');
  if (el) {
    el.innerHTML = '';
    for (const step of _betLadder(mode, unit, level)) {
      const span = document.createElement('span');
      span.textContent = _fmtAmt(step.amount);
      span.style.display = 'inline-block';
      span.style.margin = '0 3px';
      if (step.current) {
        span.style.color = '#ffcc00';
        span.style.fontWeight = '700';
        span.style.textDecoration = 'underline';
      } else {
        span.style.color = '#7a8aa0';
      }
      el.appendChild(span);
    }
  }
}

function applyMoneyStatusToSignalPanel(ms) {
  if (!ms || typeof ms !== 'object') return;
  const mode = String(ms.mode || '').toLowerCase();
  if (mode === 'martingale' || mode === 'dalembert') {
    renderProgressionPanel(ms, mode);
    return;
  }
  // SEQ / flat: restore the default SEQ labels then render the ○× sets.
  _setSigPanel('SIGNAL PANEL', ['CYCLE', 'RATIO', 'DRIFT', 'ROUND'], 'STREAM');
  const sets = Array.isArray(ms.seq7_sets) ? ms.seq7_sets : [];
  const turns = Array.isArray(ms.seq7_current_turns) ? ms.seq7_current_turns : [];
  renderDevSets(sets, turns);
  updateDevPanel({
    current_turn: ms.seq_turn,
    overshoot: ms.seq_overshoot,
    turns_display: turns.join(''),
  });
}

function _manualItemExpiryMs(item) {
  const raw = item && item.expires_at;
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    return raw < 100000000000 ? raw * 1000 : raw;
  }
  return 0;
}

function _manualNormalizeStatus(status) {
  const s = String(status || '').toUpperCase();
  if (['READY', 'NOW', 'TAKEN', 'EXPIRED', 'MISSED', 'SETTLED'].includes(s)) return s;
  return 'READY';
}

function upsertManualAssistItem(msg) {
  const id = String(msg.id || msg.decision_id || `${msg.status}:${msg.qpid || msg.table_id}:${Date.now()}`);
  const existing = manualAssistItems.find((it) => it.id === id);
  const next = {
    ...(existing || {}),
    ...msg,
    id,
    status: _manualNormalizeStatus(msg.status),
    updated_at_ms: Date.now(),
  };
  if (existing) {
    Object.assign(existing, next);
  } else {
    manualAssistItems.unshift(next);
  }
  const status = _manualNormalizeStatus(next.status);
  const active = manualAssistItems.find((it) => it.id === activeManualItemId);
  const activeStatus = active ? _manualNormalizeStatus(active.status) : '';
  if (status === 'NOW' || !active || activeStatus === 'SETTLED') {
    activeManualItemId = id;
  }
  while (manualAssistItems.length > 20) manualAssistItems.pop();
  renderManualAssistPanel();
}

function getManualResultTarget() {
  const active = manualAssistItems.find((it) => it.id === activeManualItemId);
  if (active && ['NOW', 'TAKEN'].includes(_manualNormalizeStatus(active.status))) return active;
  return manualAssistItems
    .slice()
    .sort((a, b) => {
      const rank = { NOW: 0, TAKEN: 1, READY: 2, MISSED: 3, EXPIRED: 4, SETTLED: 5 };
      const ar = rank[_manualNormalizeStatus(a.status)] ?? 9;
      const br = rank[_manualNormalizeStatus(b.status)] ?? 9;
      if (ar !== br) return ar - br;
      return (b.updated_at_ms || 0) - (a.updated_at_ms || 0);
    })
    .find((it) => ['NOW', 'TAKEN'].includes(_manualNormalizeStatus(it.status))) || null;
}

function markManualAssistTaken(id) {
  const item = manualAssistItems.find((it) => it.id === id);
  if (!item) return;
  if (['EXPIRED', 'MISSED', 'SETTLED'].includes(_manualNormalizeStatus(item.status))) return;
  item.status = 'TAKEN';
  item.taken_at_ms = Date.now();
  activeManualItemId = id;
  addLog(`[DL Assist] taken ${item.table_name || item.table_id || ''}`, 'info');
  sendManualAssistCommand({ action: 'take', id, decision_id: item.decision_id || '' });
  renderManualAssistPanel();
}

async function sendManualAssistCommand(payload) {
  if (!window.valhalla?.manualAssistCommand) {
    addLog('[DL Assist] command channel unavailable', 'lose');
    return { ok: false };
  }
  try {
    const res = await window.valhalla.manualAssistCommand(payload);
    if (!res || res.ok === false) {
      addLog(`[DL Assist] command failed: ${(res && (res.error || res.reason)) || 'unknown'}`, 'lose');
    }
    return res || { ok: false };
  } catch (e) {
    addLog(`[DL Assist] command failed: ${e.message || e}`, 'lose');
    return { ok: false };
  }
}

function renderManualAssistPanel() {
  const panel = $('#manualAssistPanel');
  const queue = $('#manualAssistQueue');
  const summary = $('#manualAssistSummary');
  const mode = $('#manualAssistMode');
  if (!panel || !queue) return;
  panel.style.removeProperty('display');
  let selectedAssistMode = false;
  try {
    const settings = loadSettings();
    const selectedBetMode = normalizeBetMode(settings.bet_mode);
    selectedAssistMode = isDualLineAssistBetMode(selectedBetMode)
      || settings.mode === 'dual_line'
      || settings.mode === 'dual_line_assist'
      || settings.mode === 'dual_line_manual';
  } catch (_) {}
  // Hysteresis: keep panel visible for 90s after the last enabled signal so
  // an engine auto-restart or a transient IPC gap does not blank the panel.
  const recentEnable = manualAssistEnabledAt > 0 && (Date.now() - manualAssistEnabledAt < 90000);
  panel.classList.toggle(
    'hidden',
    !manualAssistEnabled && !selectedAssistMode && !recentEnable && manualAssistItems.length === 0
  );
  if (mode) mode.textContent = manualAssistEnabled ? 'ASSIST' : 'AUTO';

  const now = Date.now();
  for (const item of manualAssistItems) {
    const status = _manualNormalizeStatus(item.status);
    const expiry = _manualItemExpiryMs(item);
    if ((status === 'READY' || status === 'NOW') && expiry > 0 && now > expiry) {
      item.status = status === 'NOW' ? 'MISSED' : 'EXPIRED';
    }
  }

  const visible = manualAssistItems.slice().sort((a, b) => {
    const rank = { NOW: 0, TAKEN: 1, READY: 2, MISSED: 3, EXPIRED: 4, SETTLED: 5 };
    const ar = rank[_manualNormalizeStatus(a.status)] ?? 9;
    const br = rank[_manualNormalizeStatus(b.status)] ?? 9;
    if (ar !== br) return ar - br;
    return (b.updated_at_ms || 0) - (a.updated_at_ms || 0);
  });

  const nowCount = visible.filter((it) => _manualNormalizeStatus(it.status) === 'NOW').length;
  const readyCount = visible.filter((it) => _manualNormalizeStatus(it.status) === 'READY').length;
  const taken = visible.find((it) => it.id === activeManualItemId && _manualNormalizeStatus(it.status) === 'TAKEN');
  if (summary) {
    const takenText = taken ? ` | TAKEN ${taken.table_name || taken.table_id || ''}` : '';
    summary.textContent = `NOW ${nowCount} / READY ${readyCount}${takenText}`;
  }

  if (visible.length === 0) {
    queue.innerHTML = '<div class="manual-assist-empty">No manual targets</div>';
  } else {
    queue.innerHTML = visible.map((item) => {
      const status = _manualNormalizeStatus(item.status);
      const sideRaw = String(item.side || '?').toUpperCase();
      const sideCls = sideRaw.startsWith('B') ? 'b' : sideRaw.startsWith('P') ? 'p' : '';
      const sideLabel = sideRaw === 'B' ? 'BANKER' : sideRaw === 'P' ? 'PLAYER' : sideRaw;
      const amount = Number.isFinite(Number(item.amount)) ? Number(item.amount).toFixed(2) : '0.00';
      const table = item.table_name || item.table_id || item.qpid || '?';
      const pattern = item.pattern_key || item.pattern || '';
      const expiry = _manualItemExpiryMs(item);
      const remain = expiry > 0 && ['READY', 'NOW'].includes(status)
        ? Math.max(0, Math.ceil((expiry - now) / 1000))
        : 0;
      const canTake = status === 'NOW';
      return `
        <div class="manual-assist-item ${status.toLowerCase()}" data-id="${esc(item.id)}">
          <div class="manual-status">${esc(status)}</div>
          <div class="manual-main">
            <div class="manual-table" title="${esc(table)}">${esc(table)}</div>
            <div class="manual-meta">
              <span class="manual-side ${sideCls}">${esc(sideLabel)}</span>
              $${amount}${remain ? ` | ${remain}s` : ''}${pattern ? ` | ${esc(pattern)}` : ''}
            </div>
          </div>
          <div class="manual-item-actions">
            <button class="manual-mini-btn" data-manual-action="take" data-id="${esc(item.id)}" ${canTake ? '' : 'disabled'}>TAKE</button>
          </div>
        </div>
      `;
    }).join('');
  }

  const resultEnabled = !!taken;
  $('#manualResultWin')?.toggleAttribute('disabled', !resultEnabled);
  $('#manualResultLose')?.toggleAttribute('disabled', !resultEnabled);
  $('#manualResultTie')?.toggleAttribute('disabled', !resultEnabled);

  // DOM visibility proof: tag the panel with last-update timestamp + record
  // its bounding rect so we can prove from DevTools whether the panel is in
  // the layout but visually hidden vs missing entirely.
  try {
    const stamp = Date.now();
    panel.setAttribute('data-bacopy-last-update', String(stamp));
    panel.setAttribute('data-bacopy-mode', manualAssistEnabled ? 'ASSIST' : 'AUTO');
    panel.setAttribute('data-bacopy-items', String(manualAssistItems.length));
    const rect = panel.getBoundingClientRect();
    const cs = window.getComputedStyle(panel);
    window.__bacopyDebug.panelProof = {
      at: stamp,
      inDom: !!panel.parentNode,
      offsetWidth: panel.offsetWidth,
      offsetHeight: panel.offsetHeight,
      clientHeight: panel.clientHeight,
      rect: { top: rect.top, left: rect.left, width: rect.width, height: rect.height },
      hiddenClass: panel.classList.contains('hidden'),
      computedDisplay: cs.display,
      computedVisibility: cs.visibility,
      computedOpacity: cs.opacity,
      computedZIndex: cs.zIndex,
      mainContentHidden: !!($('#mainContent') && $('#mainContent').classList.contains('hidden')),
      itemsCount: manualAssistItems.length,
      manualAssistEnabled,
      manualAssistEnabledAt,
      mode: manualAssistEnabled ? 'ASSIST' : 'AUTO',
    };
  } catch (_) {}
}

setInterval(renderManualAssistPanel, 1000);

document.addEventListener('click', (ev) => {
  const btn = ev.target && ev.target.closest ? ev.target.closest('[data-manual-action]') : null;
  if (!btn) return;
  const action = btn.getAttribute('data-manual-action');
  const id = btn.getAttribute('data-id') || '';
  if (action === 'take') markManualAssistTaken(id);
});

for (const [id, result] of [
  ['manualResultWin', 'WIN'],
  ['manualResultLose', 'LOSE'],
  ['manualResultTie', 'TIE'],
]) {
  document.addEventListener('click', (ev) => {
    const target = ev.target;
    if (!target || target.id !== id) return;
    const item = manualAssistItems.find((it) => it.id === activeManualItemId);
    if (!item || _manualNormalizeStatus(item.status) !== 'TAKEN') return;
    addLog(`[DL Assist] ${result} selected${item ? ` for ${item.table_name || item.table_id || ''}` : ''}`, 'info');
    sendManualAssistCommand({
      action: 'result',
      result,
      id: item.id,
      decision_id: item.decision_id || '',
    });
  });
}

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


  while (el.childElementCount > 500) el.removeChild(el.firstChild);


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

function flashScreen(type) {
  const el = $('#flashOverlay');
  el.className = 'flash-overlay ' + type;
  const duration = (type === 'profit' || type === 'losscut') ? 2000 : 900;
  setTimeout(() => { el.className = 'flash-overlay'; }, duration);
}

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

function setAction(text) {
  $('#actionText').textContent = text;
}

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

function loadDailyPnl() {
  try { return JSON.parse(localStorage.getItem('valhalla_daily_pnl') || '{}'); }
  catch { return {}; }
}

function saveDailyPnl(data) {
  localStorage.setItem('valhalla_daily_pnl', JSON.stringify(data));
}

function todayKeyJST() {


  const now = new Date();
  const utcMs = now.getTime() + (now.getTimezoneOffset() * 60000);
  const jst = new Date(utcMs + 9 * 3600000);
  return `${jst.getFullYear()}-${String(jst.getMonth()+1).padStart(2,'0')}-${String(jst.getDate()).padStart(2,'0')}`;
}

function _freezeDailyIfRollover(newDate) {
  if (!newDate || !_dailyOpenDate) return;
  if (_dailyOpenDate === newDate) return;


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



  const today = _dailyOpenDate || todayKeyJST();
  const { daily } = _computePnl();
  const todayEl = $('#todayPnl');
  if (todayEl) {
    todayEl.textContent = `${daily >= 0 ? '+$' : '-$'}${Math.abs(daily).toFixed(0)}`;
    todayEl.className = 'today-pnl ' + (daily >= 0 ? 'positive' : 'negative');
  }


  // 実残高確認済みの場合のみ今日の値を保存（未確認時に 0 で上書きしない）
  if (_balanceConfirmed) {
    data[today] = daily;
    saveDailyPnl(data);
  }

  const keys = Object.keys(data).sort().slice(-14);
  if (keys.length === 0) {
    row.innerHTML = '<div class="daily-empty">No history yet</div>';
    return;
  }
  let html = '';
  for (const k of keys) {
    const v = data[k];
    const isToday = (k === today);
    // 今日かつ未確認の場合は -- 表示
    if (isToday && !_balanceConfirmed) {
      html += `
        <div class="daily-item">
          <div class="daily-date">${k.slice(5)}</div>
          <div class="daily-pnl">--</div>
        </div>
      `;
      continue;
    }
    const isPos = v >= 0;
    html += `
      <div class="daily-item ${isPos ? 'positive' : 'negative'}">
        <div class="daily-date">${k.slice(5)}</div>
        <div class="daily-pnl ${isPos ? 'positive' : 'negative'}">${isPos ? '+' : ''}$${v.toFixed(0)}</div>
      </div>
    `;
  }
  row.innerHTML = html;
}

window.valhalla.onAgentMessage((msg) => {
  try {
    const t = (msg && msg.type) || 'unknown';
    window.__bacopyDebug.agentCounts[t] = (window.__bacopyDebug.agentCounts[t] || 0) + 1;
    window.__bacopyDebug.lastMsg = msg;
    if (t === 'manual_assist_mode' || t === 'manual_assist_item') {
      // High-signal events: log to DevTools so we can copy/paste evidence
      console.log('[BACOPY-RENDERER] received', t, msg);
      if (t === 'manual_assist_mode') window.__bacopyDebug.lastManualMode = msg;
      if (t === 'manual_assist_item') window.__bacopyDebug.lastManualItem = msg;
    }
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



      if (typeof msg.balance === 'number' && msg.balance > 0) {
        _currentBalance = msg.balance;
        _balanceConfirmed = true;
        $('#balance').textContent = `$${msg.balance.toFixed(2)}`;
      }
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
      if (msg.money_status) {
        applyMoneyStatusToSignalPanel(msg.money_status);
      } else if (typeof msg.current_turn === 'number' || typeof msg.overshoot === 'number' || msg.turns_display) {
        updateDevPanel({
          current_turn: msg.current_turn,
          overshoot: msg.overshoot,
          turns_display: msg.turns_display || '',
        });
      }
      updateSessionDisplay();
      renderDailyPnl();
      scheduleGuiStateSync();
      break;
    }

    case 'set_complete':


      if (isDevMode()) {
        const s = msg;
        const sign = s.set_profit >= 0 ? '+' : '';
        addLog(`[DEV] Set #${s.set_index} done: ${s.wins}W/${s.losses}L ${sign}${s.set_profit}ch OS:${s.overshoot}`, 'info');
      }
      break;

    case 'shoe_history':


      if (isDevMode() && Array.isArray(msg.sets)) {
        renderDevSets(msg.sets, msg.current_turns);
      }
      break;

    case 'test_status': {


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
        _balanceConfirmed = true;
        $('#balance').textContent = `$${msg.balance.toFixed(2)}`;
      }


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


      if (typeof msg.overshoot === 'number') {
        const osEl = $('#osValue');
        if (osEl) {
          const os = msg.overshoot;
          osEl.textContent = `OS ${os}`;
          osEl.className = 'os-tag ' + (os === 0 ? '' : os <= 2 ? 'safe' : os <= 4 ? 'warn' : 'danger');
        }
      }


      if (msg.money_status) {
        logSeqProbe('status', msg.money_status, { wins: msg.wins || 0, losses: msg.losses || 0, ties: msg.ties || 0 });
        applyMoneyStatusToSignalPanel(msg.money_status);
      } else updateDevPanel(msg);
      break;
    }

    case 'money_status': {
      logSeqProbe('money_status', msg.money_status || msg);
      applyMoneyStatusToSignalPanel(msg.money_status || msg);
      break;
    }

    case 'manual_assist_mode': {
      const enabled = !!msg.enabled;
      manualAssistEnabled = enabled;
      if (enabled) manualAssistEnabledAt = Date.now();
      renderManualAssistPanel();
      addLog(
        enabled
          ? (msg.manual_assist_auto_click ? '[DL Assist] manual assist enabled; auto-click armed' : '[DL Assist] manual assist enabled; auto-bet disabled')
          : '[DL Assist] auto-bet enabled',
        'info'
      );
      if (msg.money_status) {
        logSeqProbe('manual_assist_mode', msg.money_status, { enabled });
        applyMoneyStatusToSignalPanel(msg.money_status);
      }
      break;
    }

    case 'manual_assist_item': {
      manualAssistEnabled = true;
      manualAssistEnabledAt = Date.now();
      const status = String(msg.status || '').toUpperCase();
      const side = String(msg.side || '?').toUpperCase();
      const amount = Number.isFinite(Number(msg.amount)) ? Number(msg.amount).toFixed(2) : '0.00';
      const table = msg.table_name || msg.table_id || msg.qpid || '?';
      const label = status === 'NOW' ? '[DL Assist NOW]' : `[DL Assist ${status || 'ITEM'}]`;
      upsertManualAssistItem(msg);
      addLog(`${label} ${table} ${side} $${amount}`, status === 'NOW' ? 'win' : 'info');
      setAction(`${label} ${side} $${amount} on ${table}`);
      if (msg.money_status) {
        logSeqProbe('manual_assist_item', msg.money_status, {
          status,
          side,
          amount,
          table,
          gui_next_bet: msg.gui_next_bet,
        });
        applyMoneyStatusToSignalPanel(msg.money_status);
      }
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




      const modal = $('#settingsModal');
      const modalOpen = modal && !modal.classList.contains('hidden');
      if (!modalOpen && $('#inputBetMode')) $('#inputBetMode').value = nextMode;
      _streamSetSize = _setSizeForMode(nextMode);
      addLog(`BET mode → ${nextMode}`, 'info');
      break;
    }

    case 'resolution': {
      const r = msg;
      if (r.decision_id) {
        const item = manualAssistItems.find((it) => String(it.decision_id || it.id || '') === String(r.decision_id));
        if (item) {
          item.status = 'SETTLED';
          item.result = r.result;
          item.pnl = r.pnl;
          item.money_status = r.money_status;
          if (r.money_status) {
            item.seq_turn = r.money_status.seq_turn;
            item.seq_overshoot = r.money_status.seq_overshoot;
            item.seq7_current_turns = Array.isArray(r.money_status.seq7_current_turns)
              ? r.money_status.seq7_current_turns
              : [];
            item.gui_next_bet = r.money_status.next_bet;
          }
          item.updated_at_ms = Date.now();
          activeManualItemId = item.id;
          renderManualAssistPanel();
        }
      }
      const resultIcon = r.result === 'WIN' ? 'W' : (r.result === 'TIE' ? 'T' : 'L');
      addResult(resultIcon);
      if (r.result === 'WIN') flashScreen('win');
      else if (r.result === 'LOSE') flashScreen('lose');
      // STREAM (\u25cb\u00d7)
      if (r.result !== 'TIE') _pushStreamMark(r.result === 'WIN' ? 'O' : 'X');
      // CYCLE / RATIO / DRIFT / ROUND
      const _ms = r.money_status || {};
      logSeqProbe('resolution', _ms, {
        result: r.result,
        prediction: r.prediction,
        outcome: r.outcome,
        bet_amount: r.bet_amount,
        pnl: r.pnl,
      });
      applyMoneyStatusToSignalPanel(_ms);
      setAction(
        '[DL] ' + r.result + ' ' + r.table_name + ': ' + r.prediction + '\u2192' + r.outcome + ' ' +
        'pnl=' + ((r.pnl||0) >= 0 ? '+' : '') + '$' + (r.pnl||0).toFixed(2) + ' ' +
        '(' + r.wins + 'W/' + r.losses + 'L/' + r.ties + 'T ' + r.win_rate + '%)'
      );
      if (typeof r.cumulative_pnl === 'number') {
        const cp = r.cumulative_pnl;
        $('#balance').textContent = (cp >= 0 ? '+' : '') + '$' + cp.toFixed(2);
      }
      $('#betCount').textContent = (r.wins || 0) + 'W / ' + (r.losses || 0) + 'L / ' + (r.ties || 0) + 'T';
      updateSessionDisplay();
      break;
    }

    default:
      if (msg.message) addLog(msg.message);
      break;
  }
  } catch (e) {
    console.error('[onAgentMessage] error in msg.type=' + (msg && msg.type), e);
    try {
      window.__bacopyDebug.errors.push({
        at: Date.now(),
        type: msg && msg.type,
        message: String(e && e.message || e),
        stack: String(e && e.stack || ''),
      });
      if (window.__bacopyDebug.errors.length > 50) window.__bacopyDebug.errors.shift();
    } catch (_) {}
  }
});

window.valhalla.onAgentLog((text) => {
  text.trim().split('\n').forEach(line => { if (line.trim()) addLog(line); });
});

sessionTotal = 0;
results.length = 0;
setRunning(false);

restoreSessionState();
_restoreBalanceSnapshot();
_balanceConfirmed = false; // エンジンから実残高が届くまで -- 表示
$('#balance').textContent = '--';
updateSessionDisplay();
setAction('Ready. Sign in to begin.');
addLog('BACOPYRECEIVER ready.', 'info');
renderDailyPnl();
renderFeed();
renderRecent();
applyDevMode();
initModalTabs();
initAuth();
