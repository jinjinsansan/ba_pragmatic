

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
// エンジン(課金)が計算する日次PnL(ベット純益累積=daily_bet_pnl)。参考保持。
let _engineDailyPnl = null;
// bafather.uk リアルタイム監視(admin/users)と同じ「残高差分」
// = current_balance - daily_open.balance。DAILY TOTAL ヘッダーはこれを最優先表示し、
// bafather.uk の各自デイリートータルと一致させる(engine の daily_total msg から算出)。
let _engineDailyTotal = null;
// 通貨ラベル(参考)。
let _engineCurrency = '';
// 残高が取れないプラットフォーム(hh88)。true の時は残高差分でなく daily_pnl($, 換算済)を
// DAILY TOTAL に表示する。Stake は false(従来=残高差分)。
let _enginePnlOnly = false;

let sessionTotal = 0;
let _balanceConfirmed = false; // エンジンから実残高を受信したら true
const results = [];
const manualAssistItems = [];
let manualAssistEnabled = false;
let manualAssistEnabledAt = 0;
// デュアルラインオート(全自動WS BET)中か。engine の manual_assist_mode メッセージの
// auto_bet_enabled / manual_assist_auto_click から判定。オート時は WIN/LOSE を隠す
// (自動決済が走るため手動タップは二重決済になる)。
let manualAutoBetMode = false;
let activeManualItemId = '';
// 機能①(HOLD): この卓を固定中か。固定中は他卓のシグナルでスクロール/枠移動しない。
let manualHoldOn = false;
let manualHoldTableId = '';

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
    // 自動フラットBET ON時は money mode を flat に強制(自動×SEQ=破産)。
    money_mode: settings.dga_auto_bet ? 'flat' : (settings.dual_money_mode || 'flat'),
    money_unit: settings.dual_unit || 100,
    seq_turns: settings.seq_turns === 5 ? 5 : 7,
    dual_mode: settings.dual_mode || 'v3',
    safety_mode: !!settings.safety_mode,
    on_limit: settings.dual_on_limit || 'stop',
    manual_assist_auto_click: !!settings.manual_assist_auto_click,
    // オート追従: 勝った時だけ同卓追従(大路 telecho=逆張り / dragon=順張り)。
    dual_follow: isDualLineFollowBetMode(selectedBetMode),
    dga_auto_bet: !!settings.dga_auto_bet,
    dga_regular_only: settings.dga_regular_only !== false,
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

// 拾ったNOWの勝率を2欄表示する。BET成立勝率(#winRate)とは別物で、BETの有無に関係なく
// 台の結果で判定(取りこぼし込み)。#caughtWinRate=追従込み(初回+追従)、#caughtNowWinRate=追従なし(初回のみ)。
function _setCaughtCard(elId, wins, losses, rate) {
  const el = $('#' + elId);
  if (!el) return;
  const w = wins || 0, l = losses || 0, n = w + l;
  if (n <= 0) { el.textContent = '-'; return; }
  const r = (typeof rate === 'number') ? rate : (w / n * 100);
  el.textContent = `${r.toFixed(1)}% (${w}W/${l}L)`;
}
function updateCaughtWinRate(msg) {
  if (!msg) return;
  if (typeof msg.caught_wins !== 'number' && typeof msg.caught_win_rate !== 'number'
      && typeof msg.caught_now_wins !== 'number') return;
  _setCaughtCard('caughtWinRate', msg.caught_wins, msg.caught_losses, msg.caught_win_rate);       // 追従込み
  _setCaughtCard('caughtNowWinRate', msg.caught_now_wins, msg.caught_now_losses, msg.caught_now_win_rate); // 追従なし
}

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
    // hh88(pnl_only): 残高差分が取れないので daily_pnl($, HKD→USD換算済)を $ で表示。
    if (_enginePnlOnly && typeof _engineDailyPnl === 'number' && isFinite(_engineDailyPnl)) {
      const d = _engineDailyPnl;
      todayEl.textContent = `${d >= 0 ? '+$' : '-$'}${Math.abs(d).toFixed(2)}`;
      todayEl.className = 'today-pnl ' + (d >= 0 ? 'positive' : 'negative');
    }
    // Stake: bafather.uk のリアルタイム監視(admin/users)と同じ残高差分
    // (current_balance - daily_open.balance)を最優先表示。残高が取れない時は
    // bafather.uk と同様に "--"(daily_bet_pnl にはフォールバックしない=両者一致)。
    else if (typeof _engineDailyTotal === 'number' && isFinite(_engineDailyTotal)) {
      const d = _engineDailyTotal;
      todayEl.textContent = `${d >= 0 ? '+$' : '-$'}${Math.abs(d).toFixed(2)}`;
      todayEl.className = 'today-pnl ' + (d >= 0 ? 'positive' : 'negative');
    } else {
      todayEl.textContent = '--';
      todayEl.className = 'today-pnl';
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
  profit_target: 0,
  profit_session_limit: 0,
  loss_cut: 0,
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
  bet_mode: 'dual_line',
  mode: 'dual_line_assist',
  dual_money_mode: 'small1',
  dual_unit: 100,
  dual_mode: 'v3',
  safety_mode: false,
  platform: 'stake',
  seq_shape: 'attack',
  kelly_bankroll: 1000,
  dual_live: false,
  dual_on_limit: 'stop',
  manual_assist_auto_click: false,
  // 自動フラットBET (両側・dga local-signal)。ON時は engine を dga-live 経路で
  // 全自動着弾させ、money mode を flat に強制する(自動×SEQ は破産するため)。
  dga_auto_bet: false,
  dga_regular_only: true,
};
const ALLOWED_BET_MODES = new Set(['flat_1usd', 'seq_user10', 'newseq', 'newseq30', 'small3', 'small02', 'small06', 'small1', 'small2', 'small6', 'small10', 'small30', 'kelly', 'dual_line', 'dual_line_assist', 'dual_line_auto', 'dual_line_auto_follow']);

function normalizeBetMode(mode) {
  return ALLOWED_BET_MODES.has(mode) ? mode : 'flat_1usd';
}

function isDualLineBetMode(mode) {
  return mode === 'dual_line' || mode === 'dual_line_assist' || mode === 'dual_line_auto' || mode === 'dual_line_auto_follow';
}
// 追従(オート追従)モードか。auto の一種(WS自動BET)＋勝った時だけ同卓追従。
function isDualLineFollowBetMode(mode) {
  return mode === 'dual_line_auto_follow';
}

function isDualLineAssistBetMode(mode) {
  return mode === 'dual_line' || mode === 'dual_line_assist';
}

function _ensureDualLineOption() {
  // BET MODE はデュアルラインアシストのみ。旧 auto / legacy オプションは追加しない。
  const sel = $('#inputBetMode');
  if (!sel) return;
  let hasDual = false;
  for (const opt of sel.options) {
    if (opt.value === 'dual_line') {
      hasDual = true;
      opt.textContent = 'デュアルラインアシスト';
    }
  }
  if (!hasDual) {
    const opt = document.createElement('option');
    opt.value = 'dual_line';
    opt.textContent = 'デュアルラインアシスト';
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
    // BET MODE 選択を即 localStorage に保存(保存ボタン押し忘れでも Start に反映される)。
    // Start は loadSettings() の bet_mode を読むため、ここで永続化しないと選択が無視される。
    try {
      const s = loadSettings();
      s.bet_mode = normalizeBetMode(this.value);
      localStorage.setItem('bacopy_settings', JSON.stringify(s));
    } catch (e) {}
  });
  $('#inputDualLive')?.addEventListener('change', function() {
    if ($('#inputDryRun')) $('#inputDryRun').checked = !this.checked;
  });
  // MONEY MODE: SEQ / 非SEQ の二段ゲート
  $('#inputMoneyType')?.addEventListener('change', () => { _applyMoneyTypeVisibility(); _commitMoneyMode(); });
  $('#inputSeqVariant')?.addEventListener('change', _commitMoneyMode);
  $('#inputFlatVariant')?.addEventListener('change', _commitMoneyMode);
  // 安全モードは廃止(2026-06-21)。UIをコメントアウト済み(#inputSafetyMode 不在)。
  // 下のリスナーは要素が無ければ ?. で no-op だが、明示的に無効化しておく。
  /* 安全モード(廃止):
  $('#inputSafetyMode')?.addEventListener('change', function() {
    const enabled = this.value === 'on';
    try { window.valhalla?.setSafetyMode?.(enabled); } catch (_) {}
    try {
      const st = JSON.parse(localStorage.getItem('bacopy_settings') || '{}');
      st.safety_mode = enabled;
      localStorage.setItem('bacopy_settings', JSON.stringify(st));
    } catch (_) {}
  });
  */
}, 100);

// MONEY MODE 二段ゲート: 「SEQ」選択時はスモールSEQ(0.2/1/3/6)、非SEQ時は
// フラット/マーチン/ダランベール＋ユニット額入力。確定値は隠し select
// #inputDualMoneyMode に集約し、保存/読込ロジックを壊さない。
function _applyMoneyTypeVisibility() {
  const t = $('#inputMoneyType')?.value || 'seq';
  const seq = t === 'seq', kelly = t === 'kelly', other = t === 'other';
  const b123set = t === 'b123set', dalset = t === 'dalset';
  const setmode = b123set || dalset;  // ×セット系(ターン制+ユニットを共有)
  if ($('#seqVariantGroup')) $('#seqVariantGroup').style.display = seq ? '' : 'none';
  // ターン制(セット長 5/7)は SEQ と ×セット系 で使う
  if ($('#seqTurnsGroup')) $('#seqTurnsGroup').style.display = (seq || setmode) ? '' : 'none';
  // 型(攻撃/バランス/守備)は SEQ と Kelly の両方で使う
  if ($('#seqShapeGroup')) $('#seqShapeGroup').style.display = (seq || kelly) ? '' : 'none';
  if ($('#kellyBankrollGroup')) $('#kellyBankrollGroup').style.display = kelly ? '' : 'none';
  if ($('#flatVariantGroup')) $('#flatVariantGroup').style.display = other ? '' : 'none';
  // ユニット額入力は フラット系 と ×セット系 で使う
  if ($('#dualUnitGroup')) $('#dualUnitGroup').style.display = (other || setmode) ? '' : 'none';
  if ($('#b123setNote')) $('#b123setNote').style.display = b123set ? '' : 'none';
  if ($('#dalsetNote')) $('#dalsetNote').style.display = dalset ? '' : 'none';
}
function _commitMoneyMode() {
  const t = $('#inputMoneyType')?.value || 'seq';
  let val;
  if (t === 'seq') val = ($('#inputSeqVariant')?.value || 'small1');
  else if (t === 'kelly') val = 'kelly';
  else if (t === 'b123set') val = 'bet123set';
  else if (t === 'dalset') val = 'dalembertset';
  else val = ($('#inputFlatVariant')?.value || 'flat');
  if ($('#inputDualMoneyMode')) $('#inputDualMoneyMode').value = val;
}
function _loadMoneyModeUI(mode) {
  const m = String(mode || 'small1');
  const isSeq = m.indexOf('small') === 0;  // small02/small1/small3/small6
  const isKelly = m === 'kelly';
  const isB123set = m === 'bet123set';
  const isDalset = m === 'dalembertset';
  if ($('#inputMoneyType')) $('#inputMoneyType').value = isKelly ? 'kelly'
    : (isSeq ? 'seq' : (isB123set ? 'b123set' : (isDalset ? 'dalset' : 'other')));
  if (isSeq) { if ($('#inputSeqVariant')) $('#inputSeqVariant').value = m; }
  else if (!isKelly && !isB123set && !isDalset) { if ($('#inputFlatVariant')) $('#inputFlatVariant').value = m; }
  if ($('#inputDualMoneyMode')) $('#inputDualMoneyMode').value = m;
  _applyMoneyTypeVisibility();
}

// NOW 通知音: NOW(赤/青枠)が新規に出た時に2音チャイムを鳴らす。
// Web Audio で生成(アセット不要・オフライン可)。同一 NOW の再配信では鳴らさない。
let _nowAudioCtx = null;
let _lastNowSoundId = '';
function playNowSound() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    _nowAudioCtx = _nowAudioCtx || new AC();
    const ctx = _nowAudioCtx;
    if (ctx.state === 'suspended') { try { ctx.resume(); } catch (_) {} }
    const t0 = ctx.currentTime;
    [[880, 0.0], [1320, 0.14]].forEach(([freq, dt]) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t0 + dt);
      gain.gain.exponentialRampToValueAtTime(0.4, t0 + dt + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + 0.45);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t0 + dt);
      osc.stop(t0 + dt + 0.47);
    });
  } catch (_) { /* sound is best-effort */ }
}

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
  _loadMoneyModeUI(s.dual_money_mode || 'small1');
  if ($('#inputDualUnit')) $('#inputDualUnit').value = s.dual_unit || 100;
  if ($('#inputSeqTurns')) $('#inputSeqTurns').value = String(s.seq_turns === 5 ? 5 : 7);
  if ($('#inputSeqShape')) $('#inputSeqShape').value = (['balance','defense'].includes(s.seq_shape) ? s.seq_shape : 'attack');
  if ($('#inputKellyBankroll') && s.kelly_bankroll) $('#inputKellyBankroll').value = s.kelly_bankroll;
  if ($('#inputPlatform')) $('#inputPlatform').value = (s.platform === 'hh88') ? 'hh88' : 'stake';
  if ($('#inputSafetyMode')) $('#inputSafetyMode').value = s.safety_mode ? 'on' : 'off';
  // hh88 選択時のみ URL コピー行を表示し、コピーボタン/切替を配線(冪等)。
  (function wireHh88Url() {
    const sel = $('#inputPlatform');
    const row = $('#hh88UrlRow');
    if (!sel || !row) return;
    const sync = () => { row.style.display = (sel.value === 'hh88') ? '' : 'none'; };
    sync();
    sel.onchange = sync;
    const btn = $('#btnCopyHh88Url');
    const inp = $('#inputHh88Url');
    if (btn && inp) {
      btn.onclick = () => {
        const text = inp.value || '';
        const done = () => { const o = btn.textContent; btn.textContent = 'コピーしました'; setTimeout(() => { btn.textContent = o; }, 1500); };
        const fallback = () => { try { inp.focus(); inp.select(); document.execCommand('copy'); done(); } catch (_) {} };
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(fallback);
          } else { fallback(); }
        } catch (_) { fallback(); }
      };
    }
    // 「betting Chromeで開く」: 9222 Chrome に hh88 タブを開かせる(ワンクリック)。
    const openBtn = $('#btnOpenHh88Url');
    if (openBtn && inp) {
      openBtn.onclick = async () => {
        const url = inp.value || '';
        const o = openBtn.textContent;
        openBtn.disabled = true;
        openBtn.textContent = '開いています…';
        let res = null;
        try {
          if (window.valhalla && window.valhalla.openBettingUrl) {
            res = await window.valhalla.openBettingUrl(url);
          }
        } catch (_) {}
        const ok = res && res.ok;
        openBtn.textContent = ok ? '開きました' : '失敗(Chrome未起動?)';
        setTimeout(() => { openBtn.textContent = o; openBtn.disabled = false; }, 2000);
      };
    }
  })();
  if ($('#inputDualLive')) $('#inputDualLive').checked = !!s.dual_live;
  if ($('#inputManualAssistAutoClick')) $('#inputManualAssistAutoClick').checked = !!s.manual_assist_auto_click;
  if ($('#inputDgaAutoBet')) $('#inputDgaAutoBet').checked = !!s.dga_auto_bet;
  if ($('#inputDgaRegularOnly')) $('#inputDgaRegularOnly').checked = s.dga_regular_only !== false;
  if ($('#dgaRegularOnlyGroup')) $('#dgaRegularOnlyGroup').style.display = s.dga_auto_bet ? '' : 'none';
  if ($('#inputDgaAutoBet')) $('#inputDgaAutoBet').onchange = (e) => {
    if ($('#dgaRegularOnlyGroup')) $('#dgaRegularOnlyGroup').style.display = e.target.checked ? '' : 'none';
    // 自動BET ON 時は SEQ を選べないよう money type を flat に寄せる(視覚的な安全策)。
    if (e.target.checked && $('#inputMoneyType')) { $('#inputMoneyType').value = 'other'; if ($('#inputFlatVariant')) $('#inputFlatVariant').value = 'flat'; if (typeof _applyMoneyTypeVisibility === 'function') _applyMoneyTypeVisibility(); if (typeof _commitMoneyMode === 'function') _commitMoneyMode(); }
  };
  if ($('#inputOnLimitRestart')) $('#inputOnLimitRestart').checked = s.dual_on_limit === 'restart';
  // dual-line 時 chip_base グループを非表示
  if ($('#chipBaseGroup')) $('#chipBaseGroup').style.display = isDL ? 'none' : '';
  // dual-line 時 BET MODE に dual_line を追加
  _ensureDualLineOption();
  $('#inputProfitTarget').value = s.profit_target;
  if ($('#inputDualProfitTarget')) $('#inputDualProfitTarget').value = Number(s.profit_target) || 0;
  if ($('#inputDualLossCut')) $('#inputDualLossCut').value = Number(s.loss_cut) || 0;
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

  // 二段ゲートの選択を確定 money mode へ反映してから読む
  _commitMoneyMode();
  const selectedBetMode = normalizeBetMode($('#inputBetMode')?.value);
  const isDualLine = isDualLineBetMode(selectedBetMode);
  const isDualLineAssist = isDualLineAssistBetMode(selectedBetMode);
  const settings = {
    // bet_mode が金額を決めるため chip_base は固定 (UIも非表示)
    chip_base: isDualLine ? parseFloat($('#inputDualUnit')?.value || 100) : 1,
    // 利確(セッションPnL目標): dual-line は専用入力 #inputDualProfitTarget を使う。
    // 到達で自動停止(SEQ保持・表示維持)。0=無効。
    profit_target: isDualLine ? (parseFloat($('#inputDualProfitTarget')?.value) || 0) : 0,
    profit_session_limit: 0,
    // 損切り(全モード共通): dual-line は専用入力 #inputDualLossCut。到達で自動停止(飛ばない保険)。
    // エンジンの BetManager は全 money mode で loss_cut を適用(_check_limits)。0=無効。
    loss_cut: isDualLine ? (parseFloat($('#inputDualLossCut')?.value) || 0) : 0,
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
    seq_turns: parseInt($('#inputSeqTurns')?.value || '7', 10),
    seq_shape: $('#inputSeqShape')?.value || 'attack',
    kelly_bankroll: parseFloat($('#inputKellyBankroll')?.value || 1000),
    dual_mode: $('#inputDualMode')?.value || 'v3',
    safety_mode: $('#inputSafetyMode')?.value === 'on',
    platform: $('#inputPlatform')?.value || 'stake',
    dual_live: $('#inputDualLive')?.checked || false,
    dual_on_limit: 'stop',  // 利確はSEQ保持で停止(restart=リセットは使わない)
    manual_assist_auto_click: $('#inputManualAssistAutoClick')?.checked || false,
    dga_auto_bet: $('#inputDgaAutoBet')?.checked || false,
    dga_regular_only: $('#inputDgaRegularOnly')?.checked !== false,
  };

  localStorage.setItem('bacopy_settings', JSON.stringify(settings));



  try {
    if (window.bacopy?.saveSettings) {
      const envPayload = {
        telegram_bot_token: $('#inputTelegramToken')?.value.trim() || '',
        telegram_chat_id:   $('#inputTelegramChat')?.value.trim()  || '',
        // プラットフォーム選択を .env に保存 → 次回アイコン起動で正しいカジノが開く。
        platform: $('#inputPlatform')?.value || 'stake',
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

// Build the bet ladder for martingale (unit*2^k) / dalembert (unit*(k+1)) /
// bet123 (unit*1,2,3 fixed cycle), marking the current level.
function _betLadder(mode, unit, level) {
  const u = Number(unit) || 1;
  const cur = Math.max(0, Number(level) || 0);
  const span = mode === 'bet123' ? 2 : Math.max(cur + 3, 7);
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
  const level = mode === 'bet123' ? (Number(ms.seq_level) || 0) : (Number(ms.loss_count) || 0);
  const nextBet = Number(ms.next_bet) || 0;
  const pnl = Number(ms.session_pnl) || 0;
  if (mode === 'martingale') {
    _setSigPanel('MARTINGALE', ['LOSSES', 'NEXT $', 'MAX $', 'PNL $'], 'BET LADDER (x2)');
    const sd = $('#sigDrift');
    if (sd) sd.textContent = (Number(ms.martingale_max_bet) > 0) ? _fmtAmt(Number(ms.martingale_max_bet)) : '--';
  } else if (mode === 'bet123') {
    _setSigPanel('123BET', ['STEP', 'NEXT $', 'UNIT $', 'PNL $'], 'BET CYCLE (1-2-3)');
    const sd = $('#sigDrift');
    if (sd) sd.textContent = _fmtAmt(unit);
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

function updateNextBetCard(ms) {
  if (!ms || typeof ms !== 'object') return;
  const amtEl = $('#manualNextBetAmt');
  if (!amtEl) return;
  const n = Number(ms.next_bet ?? ms.gui_next_bet ?? 0) || 0;
  amtEl.textContent = '$ ' + n.toFixed(2);
}

function renderKellyPanel(ms) {
  // 比例(ケリー): 残高 × f% = NEXT BET を可視化。型=攻撃(フル)/バランス(½)/守備(¼)。
  const shapeMap = { attack: '攻撃(フル)', balance: 'バランス(½)', defense: '守備(¼)' };
  const shape = String(ms.kelly_shape || 'defense').toLowerCase();
  const bank = Number(ms.kelly_bankroll) || 0;
  const bet = Number(ms.next_bet) || 0;
  const pnl = Number(ms.session_pnl) || 0;
  const fpct = bank > 0 ? (bet / bank * 100) : 0;
  _setSigPanel('KELLY 比例', ['型', '残高 $', 'f %', 'NEXT $'], '残高 × f = BET（複利・破滅せず）');
  const sc = $('#sigCycle'); if (sc) sc.textContent = shapeMap[shape] || shape;
  const sr = $('#sigRatio'); if (sr) sr.textContent = '$' + bank.toFixed(0);
  const sd = $('#sigDrift'); if (sd) sd.textContent = fpct.toFixed(3) + '%';
  const srd = $('#sigRound'); if (srd) { srd.textContent = '$' + bet.toFixed(2); srd.style.color = '#00e5ff'; }
  const el = $('#sigStream');
  if (el) {
    el.innerHTML = '';
    const span = document.createElement('span');
    span.textContent = `残高 $${bank.toFixed(0)} × ${fpct.toFixed(3)}% = $${bet.toFixed(2)}　|　PnL ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}　(攻撃=フル/バランス=½/守備=¼)`;
    span.style.color = '#9fb4c8';
    span.style.fontSize = '12px';
    el.appendChild(span);
  }
}

// 1-2-3×セット打法: Nハンド(=セット長)=1セットで賭け額固定(step単位)。セット
// 負け越し→step+1(1→2→3, step3で更に負け越し→1へ一巡)・勝ち越し→1へ戻す。最大3単位。
// 現セットの〇✕進行 + 1→2→3ラダー(現stepを強調)を表示する。
function renderBet123SetPanel(ms) {
  if (!isDevMode()) return;
  const unit = Number(ms.unit) || 1;
  const step = Math.max(1, Math.min(3, Number(ms.b123set_step) || 1));
  const marks = Array.isArray(ms.b123set_marks) ? ms.b123set_marks : [];
  const setSize = Number(ms.b123set_set_size) || 7;
  const nextBet = Number(ms.next_bet) || 0;
  const pnl = Number(ms.session_pnl) || 0;
  const wins = marks.filter((m) => m === 'O').length;
  const losses = marks.length - wins;
  _setSigPanel('123×SET', ['STEP', 'NEXT $', 'SET', 'PNL $'], `${setSize}手=1セットで1-2-3（負→上げ / 勝→1）`);
  const sc = $('#sigCycle'); if (sc) sc.textContent = String(step) + '/3';
  const sr = $('#sigRatio'); if (sr) sr.textContent = _fmtAmt(nextBet);
  const sd = $('#sigDrift'); if (sd) sd.textContent = `${marks.length}/${setSize} (${wins}勝${losses}敗)`;
  const srd = $('#sigRound');
  if (srd) { srd.textContent = _fmtAmt(pnl); srd.style.color = pnl >= 0 ? '#00ff88' : '#ff3366'; }
  const el = $('#sigStream');
  if (el) {
    el.innerHTML = '';
    // 現セットの〇✕進行(残りは _)
    const mw = document.createElement('span');
    mw.style.marginRight = '14px';
    for (let i = 0; i < setSize; i += 1) {
      const mk = marks[i];
      const s = document.createElement('span');
      s.textContent = mk === 'O' ? '〇' : (mk === 'X' ? '✕' : '_');
      s.style.margin = '0 1px';
      s.style.color = mk === 'O' ? '#00ff88' : (mk === 'X' ? '#ff3366' : '#445566');
      mw.appendChild(s);
    }
    el.appendChild(mw);
    // 1→2→3 ラダー(現stepを強調)
    for (let k = 1; k <= 3; k += 1) {
      const s = document.createElement('span');
      s.textContent = _fmtAmt(unit * k);
      s.style.display = 'inline-block';
      s.style.margin = '0 3px';
      if (k === step) { s.style.color = '#ffcc00'; s.style.fontWeight = '700'; s.style.textDecoration = 'underline'; }
      else { s.style.color = '#7a8aa0'; }
      el.appendChild(s);
    }
  }
}

// ダランベール×セット打法: Nハンド=1セットで賭け額固定(level単位)。セット負け越し→
// level+1 / 勝ち越し→level-1(下限1, 上限なし)。現セット〇✕進行 + level近傍ラダーを表示。
function renderDalembertSetPanel(ms) {
  if (!isDevMode()) return;
  const unit = Number(ms.unit) || 1;
  const level = Math.max(1, Number(ms.dalembertset_level) || 1);
  const marks = Array.isArray(ms.dalembertset_marks) ? ms.dalembertset_marks : [];
  const setSize = Number(ms.dalembertset_set_size) || 7;
  const nextBet = Number(ms.next_bet) || 0;
  const pnl = Number(ms.session_pnl) || 0;
  const wins = marks.filter((m) => m === 'O').length;
  const losses = marks.length - wins;
  _setSigPanel("DALEMBERT×SET", ['LEVEL', 'NEXT $', 'SET', 'PNL $'], `${setSize}手=1セットで ±1ユニット（負→+1 / 勝→-1）`);
  const sc = $('#sigCycle'); if (sc) sc.textContent = String(level);
  const sr = $('#sigRatio'); if (sr) sr.textContent = _fmtAmt(nextBet);
  const sd = $('#sigDrift'); if (sd) sd.textContent = `${marks.length}/${setSize} (${wins}勝${losses}敗)`;
  const srd = $('#sigRound');
  if (srd) { srd.textContent = _fmtAmt(pnl); srd.style.color = pnl >= 0 ? '#00ff88' : '#ff3366'; }
  const el = $('#sigStream');
  if (el) {
    el.innerHTML = '';
    // 現セットの〇✕進行
    const mw = document.createElement('span');
    mw.style.marginRight = '14px';
    for (let i = 0; i < setSize; i += 1) {
      const mk = marks[i];
      const s = document.createElement('span');
      s.textContent = mk === 'O' ? '〇' : (mk === 'X' ? '✕' : '_');
      s.style.margin = '0 1px';
      s.style.color = mk === 'O' ? '#00ff88' : (mk === 'X' ? '#ff3366' : '#445566');
      mw.appendChild(s);
    }
    el.appendChild(mw);
    // level 近傍ラダー(現levelを強調・上限なしなので現在地中心に表示)
    const lo = Math.max(1, level - 2);
    for (let k = lo; k <= lo + 5; k += 1) {
      const s = document.createElement('span');
      s.textContent = _fmtAmt(unit * k);
      s.style.display = 'inline-block';
      s.style.margin = '0 3px';
      if (k === level) { s.style.color = '#ffcc00'; s.style.fontWeight = '700'; s.style.textDecoration = 'underline'; }
      else { s.style.color = '#7a8aa0'; }
      el.appendChild(s);
    }
  }
}

function applyMoneyStatusToSignalPanel(ms) {
  if (!ms || typeof ms !== 'object') return;
  updateNextBetCard(ms);
  const mode = String(ms.mode || '').toLowerCase();
  if (mode === 'kelly') {
    renderKellyPanel(ms);
    return;
  }
  if (mode === 'bet123set') {
    renderBet123SetPanel(ms);
    return;
  }
  if (mode === 'dalembertset') {
    renderDalembertSetPanel(ms);
    return;
  }
  if (mode === 'martingale' || mode === 'dalembert' || mode === 'bet123') {
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
    // 機能①(HOLD): 固定中は固定卓以外のNOWでアクティブ(結果ボタン対象)を奪わせない。
    const itemTbl = String(next.qpid || next.table_id || '');
    const stealBlocked = manualHoldOn && manualHoldTableId && itemTbl !== manualHoldTableId;
    if (!stealBlocked) {
      activeManualItemId = id;
    }
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
          <div class="manual-item-actions"></div>
        </div>
      `;
    }).join('');
  }

  // アシスト時: WIN/LOSE/TIE は常に押せる(人間が手動BETして結果をタップ→SEQ進行)。
  // オート時(デュアルラインオート): 自動決済が走るため手動タップは二重決済になる。
  // → WIN/LOSE/TIE を非表示にして事故を防ぐ(HOLDは表示維持=スクロール凍結用)。
  const autoMode = manualAutoBetMode;
  const resultEnabled = !autoMode;
  ['#manualResultWin', '#manualResultLose', '#manualResultTie'].forEach((sel) => {
    const b = $(sel);
    if (b) {
      b.style.display = autoMode ? 'none' : '';
      b.toggleAttribute('disabled', !resultEnabled);
    }
  });
  const modeBadge = $('#manualAssistMode');
  if (modeBadge) {
    modeBadge.textContent = autoMode ? 'AUTO' : 'ASSIST';
    modeBadge.classList.toggle('auto', autoMode);
  }
  const holdBtn = $('#manualHoldToggle');
  if (holdBtn) {
    // オート時はスクロール制御も自動なので HOLD も隠す(結果行は全ボタン非表示)。
    holdBtn.style.display = autoMode ? 'none' : '';
    holdBtn.classList.toggle('active', manualHoldOn);
    holdBtn.textContent = manualHoldOn ? '● HOLD' : 'HOLD';
  }
  // NEXT BETの色を現在のNOW(赤=Banker/青=Player)に追従。NOW無し=水色(default)。
  const mnb = $('#manualNextBet');
  if (mnb) {
    const act = getManualResultTarget();
    const aside = act ? String(act.side || '').toUpperCase() : '';
    mnb.classList.toggle('now-b', aside === 'B');
    mnb.classList.toggle('now-p', aside === 'P');
  }

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
    // No TAKE required: apply the result to the most relevant NOW item (its side
    // + amount), or standalone if none (engine falls back to the current next_bet).
    const item = getManualResultTarget();
    addLog(`[DL Assist] ${result} selected${item ? ` for ${item.table_name || item.table_id || ''}` : ''}`, 'info');
    sendManualAssistCommand({
      action: 'result',
      result,
      id: item ? item.id : '',
      side: item ? (item.side || '') : '',
      decision_id: item ? (item.decision_id || '') : '',
    });
    // Optimistically settle locally so a second tap does not re-resolve the same
    // NOW; the engine's resolution message confirms and updates W/L/T + next bet.
    // TIE(常にプッシュ=再BET) と HOLD固定中(継続BET) は枠を維持するため確定しない。
    if (item && result !== 'TIE' && !manualHoldOn) { item.status = 'SETTLED'; renderManualAssistPanel(); }
  });
}

// 機能①(HOLD): この卓を固定/解除するトグル。固定中はその卓に留まり、
// 他卓の新シグナル(予告)が来てもスクロール・枠移動しない。解除はもう一度押す。
document.addEventListener('click', (ev) => {
  const target = ev.target;
  if (!target || target.id !== 'manualHoldToggle') return;
  manualHoldOn = !manualHoldOn;
  const item = getManualResultTarget();
  if (manualHoldOn) {
    manualHoldTableId = item ? String(item.qpid || item.table_id || '') : '';
  } else {
    manualHoldTableId = '';
  }
  addLog(`[DL Assist] HOLD ${manualHoldOn ? 'ON' : 'OFF'}${item ? ` (${item.table_name || item.table_id || ''})` : ''}`, 'info');
  sendManualAssistCommand({
    action: 'hold',
    hold: manualHoldOn,
    id: item ? item.id : '',
    side: item ? (item.side || '') : '',
    decision_id: item ? (item.decision_id || '') : '',
  });
  renderManualAssistPanel();
});

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
  // #todayPnl は updateSessionDisplay が唯一の書き手(bafather.uk 監視と同じ残高差分)。
  // 旧コードはここでローカル差分(toFixed(0))に上書きして engine 値を潰していた=二重
  // 書き込み不具合 → 削除。

  // 履歴(過去14日ストリップ)の「今日」セルはヘッダーと一致させるため、engine の
  // 残高差分(_engineDailyTotal)を優先。無ければ従来のローカル差分にフォールバック。
  const haveEngineTotal = (typeof _engineDailyTotal === 'number' && isFinite(_engineDailyTotal));
  const todayVal = haveEngineTotal ? _engineDailyTotal : daily;
  // 実残高確認済み or engine値ありの時のみ保存（未確認かつengine無しの時に 0 で潰さない）
  if (_balanceConfirmed || haveEngineTotal) {
    data[today] = todayVal;
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
    // 今日かつ(残高未確認 & engine残高差分なし)の場合は -- 表示
    if (isToday && !_balanceConfirmed && !haveEngineTotal) {
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

// ── 安全モードの状態表示 ───────────────────────────────────────────────
// engine の safety_status を受けて「安全モードで待機中(=不具合ではない)」を
// 一目で分かるよう全体を黄色で縁取り+下部バナー表示する。稼働中(賭けられる
// 系統あり)は緑バナー。OFF/未稼働は表示を消す。
function applySafetyStatus(msg) {
  const enabled = !!(msg && msg.enabled);
  const holding = !!(msg && msg.holding);
  document.body.classList.toggle('safety-holding', enabled && holding);

  let banner = document.getElementById('safetyBanner');
  if (!enabled) { if (banner) banner.remove(); return; }
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'safetyBanner';
    document.body.appendChild(banner);
  }
  banner.className = holding ? 'holding' : 'active';
  const wr = (d) => (d && d.n ? Number(d.wr || 0).toFixed(1) : '—');
  const v3 = wr(msg && msg.v3), v4 = wr(msg && msg.v4);
  let state, message;
  if (holding) {
    state = '待機';
    message = (msg && msg.fresh === false) ? '勝率データ取得待ち' : '当日勝率 50%未満 — BET停止中';
  } else {
    state = '稼働';
    const sys = ((msg && msg.allowed) || []).map((s) => (s === 'v3' ? '6P' : '10P')).join('・') || '—';
    message = `${sys} を狙っています`;
  }
  banner.innerHTML =
    '<span class="sb-dot"></span>' +
    '<span class="sb-label">SAFE MODE</span>' +
    `<span class="sb-state">${state}</span>` +
    `<span class="sb-msg">${message}</span>` +
    '<span class="sb-stats">' +
      `<span class="sb-stat tag6">6P ${v3}%</span>` +
      `<span class="sb-stat tag10">10P ${v4}%</span>` +
    '</span>';
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
      updateCaughtWinRate(msg);
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

    case 'caught_stats': {
      // 拾ったNOW(初回シグナル・取りこぼし込み)の勝率。BET勝率(#winRate)とは別物。
      updateCaughtWinRate(msg);
      break;
    }

    case 'money_status': {
      logSeqProbe('money_status', msg.money_status || msg);
      applyMoneyStatusToSignalPanel(msg.money_status || msg);
      break;
    }

    case 'manual_assist_pin': {
      // 機能①(HOLD): エンジンからのpin状態ack。ボタン表示を同期。
      manualHoldOn = !!msg.pinned;
      manualHoldTableId = manualHoldOn ? String(msg.table_id || '') : '';
      renderManualAssistPanel();
      addLog(`[DL Assist] HOLD ${manualHoldOn ? 'ON' : 'OFF'} (engine)`, 'info');
      break;
    }

    case 'profit_target_reached': {
      // 利確達成: 緑バナーを出したまま bot を停止(SEQはエンジン側で保持)。
      const pnl = Number(msg.session_pnl) || 0;
      const tgt = Number(msg.profit_stop) || 0;
      const txt = `🎯 利確達成  +$${pnl.toFixed(2)}  (目標 $${tgt.toFixed(0)}) — 停止`;
      addLog(txt + ' / SEQ保持・再開で続行', 'info');
      try {
        let b = document.getElementById('profitBanner');
        if (!b) {
          b = document.createElement('div');
          b.id = 'profitBanner';
          b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;padding:14px 16px;text-align:center;font-weight:900;font-size:17px;color:#022;background:linear-gradient(90deg,rgba(0,229,160,0.96),rgba(0,200,120,0.96));box-shadow:0 6px 22px rgba(0,0,0,0.55);cursor:pointer;';
          b.title = 'クリックで閉じる';
          b.addEventListener('click', () => b.remove());
          document.body.appendChild(b);
        }
        b.textContent = txt + '  （クリックで閉じる）';
      } catch (e) {}
      try { stopBotFlow({ forced: true, reason: '利確達成 +$' + pnl.toFixed(2) }); } catch (e) {}
      break;
    }

    case 'daily_total': {
      // エンジン(課金)が計算した日次値。
      const d = Number(msg.daily_pnl);
      if (isFinite(d)) _engineDailyPnl = d;
      if (typeof msg.currency === 'string' && msg.currency) _engineCurrency = msg.currency;
      if (typeof msg.pnl_only === 'boolean') _enginePnlOnly = msg.pnl_only;
      // bafather.uk 監視(admin/users)と同一式: current_balance - daily_open.balance。
      // ★balance が実数(null/欠落でない)の時のみ更新。Number(null)=0 で残高差分を
      //   0 にしてしまう事故(hh88 は balance=null)を防ぐため typeof で厳密判定。
      if (typeof msg.balance === 'number' && typeof msg.daily_open_balance === 'number') {
        _engineDailyTotal = msg.balance - msg.daily_open_balance;
      }
      updateSessionDisplay();
      break;
    }

    case 'safety_status': {
      applySafetyStatus(msg);
      break;
    }

    case 'manual_assist_mode': {
      const enabled = !!msg.enabled;
      manualAssistEnabled = enabled;
      // オート(全自動)判定: auto_bet_enabled か manual_assist_auto_click。
      manualAutoBetMode = !!(msg.auto_bet_enabled || msg.manual_assist_auto_click);
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
      // NOW(赤/青枠)が新規に出たら通知音。同一NOWの再配信では鳴らさない。
      if (status === 'NOW') {
        const _nowId = String(msg.id || msg.decision_id || '');
        if (_nowId && _nowId !== _lastNowSoundId) {
          _lastNowSoundId = _nowId;
          playNowSound();
        }
      }
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
      applySafetyStatus({ enabled: false });  // 停止時は安全モード表示を消す
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
      // BET MODE はデュアルラインアシスト固定。select に存在する値のみ反映する
      // (旧モード値が来ても単一オプションを壊さない)。
      const _sel = $('#inputBetMode');
      if (!modalOpen && _sel && [..._sel.options].some(o => o.value === nextMode)) {
        _sel.value = nextMode;
      }
      _streamSetSize = _setSizeForMode(nextMode);
      addLog(`BET mode → ${nextMode}`, 'info');
      break;
    }

    case 'resolution': {
      const r = msg;
      {
        let item = r.decision_id
          ? manualAssistItems.find((it) => String(it.decision_id || it.id || '') === String(r.decision_id))
          : null;
        // フォールバック: decision_id 未一致(遅延結果は予告/NOWと別ID・黄色枠が先に消えた等)
        // の時は、同卓(table_id/qpid)の未確定の最新項目に結果を乗せる(取りこぼし防止)。
        if (!item && r.table_id) {
          const cand = manualAssistItems.filter((it) =>
            (String(it.table_id || '') === String(r.table_id) || String(it.qpid || '') === String(r.table_id))
            && it.status !== 'SETTLED');
          item = cand.length ? cand[cand.length - 1] : null;
        }
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

// ── 毎時勝率パネル(時間帯ヒートストリップ) ──────────────────────────
// master /api/hourly-stats (main.jsが60秒毎にIPC転送) を描画。
// 緑発光=60%+ / 赤=40%以下 / 減光シアン=中間。「緑が連なっているか」で
// 罫線どおりに走っている時間帯かを一目で判断するための計器。
(() => {
  const panel = document.getElementById('hourlyPanel');
  if (!panel || !window.valhalla || !window.valhalla.onHourlyStats) return;
  const head = document.getElementById('hourlyHead');
  const body = document.getElementById('hourlyBody');
  head.addEventListener('click', () => {
    body.classList.toggle('hidden');
    panel.classList.toggle('open');
  });
  const cls = (wr) => (wr >= 0.60 ? 'good' : (wr <= 0.40 ? 'bad' : 'mid'));
  const pad2 = (h) => String(h).padStart(2, '0');

  function renderStrip(el, hours) {
    el.innerHTML = '';
    const byH = {};
    for (const h of hours) byH[h.h] = h;
    const cur = new Date().getHours();
    for (let i = 7; i >= 0; i--) {
      const hh = cur - i;
      if (hh < 0) continue;
      const d = byH[hh];
      const seg = document.createElement('span');
      const wr = d && d.n ? d.w / d.n : null;
      seg.className = 'hourly-seg ' + (wr === null ? 'empty' : cls(wr));
      seg.title = `${pad2(hh)}時 ` + (d && d.n ? `${d.n}件 ${Math.round(wr * 100)}%` : 'データなし');
      el.appendChild(seg);
    }
  }

  function renderCur(el, d) {
    const hs = (d.hours || []).filter((h) => h.n);
    const last = hs.length ? hs[hs.length - 1] : null;
    if (!last) { el.textContent = '--%'; el.className = 'hourly-cur'; return; }
    const wr = last.w / last.n;
    el.textContent = Math.round(wr * 100) + '%';
    el.className = 'hourly-cur ' + cls(wr);
  }

  function renderRows(el, cumEl, d) {
    el.innerHTML = '';
    for (const h of (d.hours || [])) {
      if (!h.n) continue;
      const wr = h.w / h.n;
      const row = document.createElement('div');
      row.className = 'hourly-row ' + cls(wr);
      const bar = Math.round(wr * 100);
      row.innerHTML =
        `<span class="hr-h">${pad2(h.h)}時</span>` +
        `<span class="hr-n">${h.n}</span>` +
        `<span class="hr-bar"><span class="hr-bar-fill" style="width:${bar}%"></span></span>` +
        `<span class="hr-wr">${bar}%</span>`;
      el.appendChild(row);
    }
    cumEl.textContent = d.cum_n
      ? `累計 ${d.cum_w}/${d.cum_n}  ${(d.cum_w / d.cum_n * 100).toFixed(1)}%`
      : '本日まだデータなし';
  }

  window.valhalla.onHourlyStats((data) => {
    if (!data || !data.ok || !data.v3 || !data.v4) return;
    renderStrip(document.getElementById('hourlyStrip6'), data.v3.hours || []);
    renderStrip(document.getElementById('hourlyStrip10'), data.v4.hours || []);
    renderCur(document.getElementById('hourlyCur6'), data.v3);
    renderCur(document.getElementById('hourlyCur10'), data.v4);
    renderRows(document.getElementById('hourlyRows6'), document.getElementById('hourlyCum6'), data.v3);
    renderRows(document.getElementById('hourlyRows10'), document.getElementById('hourlyCum10'), data.v4);
    const foot = document.getElementById('hourlyFoot');
    const parts = [];
    if ((data.v3.lose_streak || 0) >= 3) parts.push(`6P ▼ ${data.v3.lose_streak}連敗中`);
    if ((data.v4.lose_streak || 0) >= 3) parts.push(`10P ▼ ${data.v4.lose_streak}連敗中`);
    foot.textContent = parts.join('   ');
  });
})();

// ── 勝率レンジチャート(TIME RATE上部) ─────────────────────────────────
// master /api/winrate-trend の「チャンネルと完全一致する累計勝率」の時系列を描画する。
//   パターンtab : v3=self.wins / v4=self._v4  (= 6P/10P チャンネルの累計% と byte一致)
//   追従込みtab : (NOW+follow) の累計  (= 結果チャンネルの 追従込み% / follow_sim 由来)
// 各系列の floor(下限)/ceil(上限) を帯+破線で可視化し、その帯の中を累計%ラインが動く。
// 小サンプルの初期振れ(n<500)は除外済みなので、帯はチャンネルで体感する狭帯域に一致する。
// 純粋な表示機能(エンジン/賭け経路に非依存)。
(() => {
  const chart = document.getElementById('wrChart');
  if (!chart || !window.valhalla || !window.valhalla.onWinrateTrend) return;
  const cv6 = document.getElementById('wrCanvas6');
  const cv10 = document.getElementById('wrCanvas10');
  const statusEl = document.getElementById('wrChartStatus');
  const tabWrap = document.getElementById('wrChartTf');
  const condEl6 = document.getElementById('wrCond6');
  const condEl10 = document.getElementById('wrCond10');

  let lastData = null;
  let tab = 'pattern'; // 'pattern' | 'follow'

  // ── 4段階コンディション ───────────────────────────────────────────
  // 直近 ~300手の勝率が、自分の累計ペース p0 のどこに居るかを σ で判定。
  //   好調: z>=0(累計と同等以上)  軟調: -1<=z<0  低調: -2<=z<-1  悪調: z<-2
  // ★予測ではない(自己相関≈0)。短期=見るだけ / 長期累計<50%は warn-edge でエッジ劣化を示唆。
  const COND_WINDOW = 300;       // 直近何手で「調子」を見るか
  const COND_MIN = 80;           // これ未満の直近サンプルは判定しない(蓄積中)
  const COND_LABEL = { good: '好調', soft: '軟調', low: '低調', bad: '悪調', wait: '—' };

  function computeGauge(series) {
    const pts = series && series.points;
    if (!pts || pts.length < 2) return { stage: 'wait' };
    const last = pts[pts.length - 1];
    if (!last || !last.n || last.w == null || last.l == null) return { stage: 'wait' };
    const p0 = last.w / last.n;                       // 累計ペース(基準・小数)
    let prev = pts[0];
    for (let i = pts.length - 1; i >= 0; i--) {       // 直近 COND_WINDOW 手だけ遡る
      prev = pts[i];
      if (last.n - pts[i].n >= COND_WINDOW) break;
    }
    const rw = last.w - prev.w, rl = last.l - prev.l, rn = rw + rl;
    if (rn < COND_MIN) return { stage: 'wait', recentN: rn };
    const recentWr = rw / rn;
    const sigma = Math.sqrt(p0 * (1 - p0) / rn) || 1e-9;
    const z = (recentWr - p0) / sigma;
    let stage = 'bad';
    if (z >= 0) stage = 'good';
    else if (z >= -1) stage = 'soft';
    else if (z >= -2) stage = 'low';
    return {
      stage, z, recentN: rn,
      recentWr: recentWr * 100, p0: p0 * 100,
      cumWr: last.wr, cumN: last.n,
      warnEdge: (last.wr != null && last.wr < 50.0),  // 累計がBE割れ=エッジ劣化サイン
    };
  }

  function updateCond(el, series, sysName) {
    if (!el) return;
    const g = computeGauge(series);
    el.setAttribute('data-stage', g.stage || 'wait');
    el.classList.toggle('warn-edge', !!g.warnEdge);
    const txt = el.querySelector('.wrcond-txt');
    if (txt) txt.textContent = COND_LABEL[g.stage] || '—';
    if (g.stage === 'wait') {
      el.title = `${sysName} 調子：直近データ蓄積中（${g.recentN || 0}手）`;
    } else {
      const tl = COND_LABEL[g.stage];
      el.title =
        `${sysName} 調子：${tl}（z=${g.z.toFixed(2)}）\n` +
        `直近${g.recentN}手 ${g.recentWr.toFixed(1)}%  ←基準(累計${g.cumN}手) ${g.p0.toFixed(1)}%\n` +
        (g.warnEdge ? '⚠累計が50%割れ＝エッジ劣化の疑い（長期で続くならパターン要確認）\n' : '') +
        '※短期は見るだけ・増減判断には使わない（自己相関≈0）';
    }
  }

  const CHART_H = 100;
  function fitCanvas(cv) {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(120, Math.floor(cv.clientWidth || cv.parentElement.clientWidth - 30));
    const h = CHART_H;
    cv.style.height = h + 'px';
    cv.width = Math.floor(w * dpr);
    cv.height = Math.floor(h * dpr);
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  // series = {points:[{ts,wr,n}], floor, ceil, cur}  ※ wr は % (例 50.49)
  function drawRange(cv, series, accent) {
    const { ctx, w, h } = fitCanvas(cv);
    ctx.clearRect(0, 0, w, h);
    const padL = 36, padR = 52, padT = 10, padB = 14;
    const x0 = padL, x1 = w - padR, y0 = padT, y1 = h - padB;
    const pts = (series && series.points) || [];

    if (pts.length < 2 || !series || series.floor == null) {
      ctx.fillStyle = 'rgba(111,138,166,0.6)';
      ctx.font = '10px ui-monospace, Consolas, monospace';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(tab === 'follow' ? '追従データ蓄積中…' : '蓄積中…', (x0 + x1) / 2, (y0 + y1) / 2);
      return;
    }

    const floor = series.floor, ceil = series.ceil;
    // y スケール: 下限/上限の外側に余白。帯が狭くても最低縦幅を確保して動きを見せる。
    let lo = floor, hi = ceil;
    const pad = Math.max(0.25, (hi - lo) * 0.28);
    lo -= pad; hi += pad;
    const MINSPAN = 1.2;
    if (hi - lo < MINSPAN) { const m = (hi + lo) / 2; lo = m - MINSPAN / 2; hi = m + MINSPAN / 2; }
    const yOf = (v) => y1 - (v - lo) / (hi - lo) * (y1 - y0);
    const xOf = (i) => pts.length <= 1 ? (x0 + x1) / 2 : x0 + i / (pts.length - 1) * (x1 - x0);

    // レンジ帯(下限→上限)
    const yF = yOf(floor), yC = yOf(ceil);
    ctx.fillStyle = accent.band;
    ctx.fillRect(x0, yC, x1 - x0, yF - yC);

    // 上限/下限 破線
    ctx.setLineDash([3, 3]); ctx.lineWidth = 1; ctx.strokeStyle = accent.bound;
    [yC, yF].forEach((y) => { ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke(); });
    ctx.setLineDash([]);

    // 50% 基準線(帯内に入る時のみ)
    const has50 = (50 >= lo && 50 <= hi);
    if (has50) {
      const y = yOf(50);
      ctx.strokeStyle = 'rgba(214,232,244,0.30)'; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke(); ctx.setLineDash([]);
    }

    // 左軸ラベル(上限/下限/50 の数値)
    ctx.font = '9px ui-monospace, Consolas, monospace'; ctx.textBaseline = 'middle'; ctx.textAlign = 'right';
    ctx.fillStyle = 'rgba(214,232,244,0.85)'; ctx.fillText(ceil.toFixed(2), x0 - 3, yC);
    ctx.fillStyle = 'rgba(214,232,244,0.85)'; ctx.fillText(floor.toFixed(2), x0 - 3, yF);
    if (has50) { ctx.fillStyle = 'rgba(214,232,244,0.5)'; ctx.fillText('50', x0 - 3, yOf(50)); }

    // 累計%ライン
    ctx.beginPath();
    pts.forEach((p, i) => { const x = xOf(i), y = yOf(p.wr); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.strokeStyle = accent.line; ctx.lineWidth = 1.6; ctx.stroke();

    // 最新点
    const lastp = pts[pts.length - 1];
    const lx = xOf(pts.length - 1), ly = yOf(lastp.wr);
    ctx.beginPath(); ctx.arc(lx, ly, 3, 0, Math.PI * 2); ctx.fillStyle = accent.line; ctx.fill();

    // 右側: 上限/下限ラベル + 現在値(チャンネルと一致する数値)
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.font = '8px ui-monospace, Consolas, monospace'; ctx.fillStyle = accent.bound;
    ctx.fillText('上限', x1 + 4, yC); ctx.fillText('下限', x1 + 4, yF);
    ctx.font = 'bold 11px ui-monospace, Consolas, monospace'; ctx.fillStyle = accent.line;
    ctx.fillText(lastp.wr.toFixed(2) + '%', x1 + 4, ly);

    // x軸: 最初/中間/最新の時刻
    ctx.fillStyle = 'rgba(111,138,166,0.7)'; ctx.font = '8px ui-monospace, Consolas, monospace'; ctx.textBaseline = 'alphabetic';
    const fmt = (ts) => { const m = /(\d{2})-(\d{2})T(\d{2}):/.exec(String(ts || '')); return m ? `${m[1]}/${m[2]} ${m[3]}h` : ''; };
    const idxs = [0, Math.floor((pts.length - 1) / 2), pts.length - 1];
    idxs.forEach((i, k) => {
      ctx.textAlign = k === 0 ? 'left' : (k === 2 ? 'right' : 'center');
      ctx.fillText(fmt(pts[i].ts), Math.max(x0, Math.min(x1, xOf(i))), h - 3);
    });
  }

  // 6P=電気シアン / 10P=ネオン緑
  const ACC6 = { line: '#00e5ff', band: 'rgba(0,229,255,0.10)', bound: 'rgba(0,229,255,0.55)' };
  const ACC10 = { line: '#00ff9c', band: 'rgba(0,255,156,0.10)', bound: 'rgba(0,255,156,0.55)' };

  function redraw() {
    const grp = lastData && lastData[tab];
    drawRange(cv6, grp && grp.v3, ACC6);
    drawRange(cv10, grp && grp.v4, ACC10);
    updateCond(condEl6, grp && grp.v3, tab === 'follow' ? '6P(追従込)' : '6P');
    updateCond(condEl10, grp && grp.v4, tab === 'follow' ? '10P(追従込)' : '10P');
    if (statusEl) {
      statusEl.textContent = (lastData && lastData.updated_at)
        ? `更新 ${(lastData.updated_at || '').slice(5, 16)}`
        : '蓄積中…';
    }
  }

  // タブ切替(パターン / 追従込み)
  if (tabWrap) {
    tabWrap.querySelectorAll('.wr-tf-btn').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        tab = btn.getAttribute('data-tab') || 'pattern';
        tabWrap.querySelectorAll('.wr-tf-btn').forEach((b) => b.classList.toggle('active', b === btn));
        redraw();
      });
    });
  }

  let rt = null;
  window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(redraw, 150); });

  window.valhalla.onWinrateTrend((data) => {
    if (!data || !data.ok) return;
    lastData = data;
    redraw();
  });

  setTimeout(redraw, 200);
})();
