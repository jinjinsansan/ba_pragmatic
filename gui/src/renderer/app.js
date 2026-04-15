// === Valhalla II -- Renderer (Futuristic BET GUI) ===

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let isRunning = false;
let logVisible = true;

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

// --- License / Setup Screen ---
function showSetup(errorMsg) {
  $('#setupScreen').style.display = 'flex';
  $('#mainContent').style.display = 'none';
  if (errorMsg) {
    $('#setupError').style.display = 'block';
    $('#setupError').textContent = errorMsg;
  }
}

function showMain() {
  $('#setupScreen').style.display = 'none';
  $('#mainContent').style.display = 'block';
  // サポートID 情報を設定モーダルに反映
  _populateSupportInfo();
  // 案C: 初回起動ハイブリッド — OpenSSH未セットアップならモーダル誘導
  _checkInitialSetup();
}

// SYSTEM タブのサポートID ボックスを .env の値で埋める
async function _populateSupportInfo() {
  try {
    const env = await window.valhalla.getEnv();
    const emailEl = $('#supportIdEmail');
    const portEl = $('#supportIdPort');
    const statusEl = $('#supportIdStatus');
    if (!emailEl || !portEl || !statusEl) return;
    // email: EXE 固有の SUPPORT_USER_EMAIL → なければ account_email → 未設定
    const email = (env && env.support_email) || (env && env.account_email) || '';
    const port = (env && env.support_port) || '';
    emailEl.textContent = email || '(not configured)';
    portEl.textContent = port || '(not configured)';
    // トンネル状態はサポート有効かつ設定ありの時のみ「Active」表示
    const supportOn = env && (env.support_enabled === '1' || env.support_enabled === 1);
    if (supportOn && email && port) {
      statusEl.textContent = '[ACTIVE]';
      statusEl.style.color = '#4ade80';
    } else {
      statusEl.textContent = '[DISABLED]';
      statusEl.style.color = 'var(--text-muted)';
    }
  } catch (e) {
    console.warn('[support-info] failed:', e);
  }
}

// 初回セットアップ誘導モーダル (OpenSSH / winget 依存が揃っているかチェック)
async function _checkInitialSetup() {
  try {
    if (!window.valhalla.checkSshdInstalled) return;  // 旧preloadでも壊れない
    const status = await window.valhalla.checkSshdInstalled();
    if (status && status.installed) {
      console.log('[setup] sshd installed — skipping prompt');
      return;
    }
    // 未インストール → モーダル表示
    const modal = $('#initSetupModal');
    if (!modal) return;
    modal.classList.remove('hidden');
    const close = () => modal.classList.add('hidden');
    $('#btnSetupNow').onclick = async () => {
      close();
      addLog('Launching initial setup (UAC prompt will appear)...', 'info');
      try {
        await window.valhalla.installDeps();
      } catch (e) {
        addLog(`Setup launch failed: ${e.message || e}`, 'lose');
      }
    };
    $('#btnSetupLater').onclick = () => {
      close();
      addLog('Setup deferred. Re-prompted on next launch, or via Settings > SYSTEM.', 'warn');
    };
  } catch (e) {
    console.warn('[setup] check failed:', e);
  }
}

async function initLicense() {
  const env = await window.valhalla.getEnv();
  const email = env.account_email;

  if (!email) {
    showSetup();
    return;
  }

  // 既存メールでライセンス確認
  const result = await window.valhalla.checkLicense(email);
  if (result.ok) {
    showMain();
  } else {
    showSetup(result.reason);
  }
}

$('#btnActivate').addEventListener('click', async () => {
  const email = $('#setupEmail').value.trim();
  const stakeUser = $('#setupStakeUser').value.trim();
  const stakePass = $('#setupStakePass').value.trim();

  if (!email || !stakeUser || !stakePass) {
    $('#setupError').style.display = 'block';
    $('#setupError').textContent = 'All fields are required.';
    return;
  }

  $('#setupLoading').style.display = 'block';
  $('#btnActivate').disabled = true;
  $('#setupError').style.display = 'none';

  const result = await window.valhalla.checkLicense(email);
  if (!result.ok) {
    $('#setupLoading').style.display = 'none';
    $('#btnActivate').disabled = false;
    $('#setupError').style.display = 'block';
    $('#setupError').textContent = result.reason;
    return;
  }

  await window.valhalla.saveCredentials({ email, stake_username: stakeUser, stake_password: stakePass });
  $('#setupLoading').style.display = 'none';
  showMain();
});

$('#linkBafather').addEventListener('click', () => window.valhalla.openExternal('https://bafather.uk'));

// 起動時にライセンス確認
initLicense();

// --- Auto Updater ---
window.valhalla.onUpdateStatus((data) => {
  const banner = $('#updateBanner');
  const text = $('#updateText');
  const btn = $('#btnInstallUpdate');
  const runUpdateBtn = $('#btnRunUpdate');
  if (data.status === 'available') {
    banner.style.display = 'flex';
    text.textContent = `New version ${data.version} downloading...`;
    btn.style.display = 'none';
    if (runUpdateBtn) runUpdateBtn.classList.add('update-needed');
  } else if (data.status === 'downloading') {
    banner.style.display = 'flex';
    text.textContent = `Downloading... ${data.percent}%`;
    btn.style.display = 'none';
    if (runUpdateBtn) runUpdateBtn.classList.add('update-needed');
  } else if (data.status === 'up-to-date' || data.status === 'not-available' || data.status === 'installed') {
    if (runUpdateBtn) runUpdateBtn.classList.remove('update-needed');
  }
});
$('#btnInstallUpdate').addEventListener('click', () => window.valhalla.openUpdatePage());

// --- SYSTEM tab button handlers ---
const _runUpdateBtn = $('#btnRunUpdate');
if (_runUpdateBtn) {
  _runUpdateBtn.addEventListener('click', async () => {
    try {
      setAction('Opening update…');
      addLog('Launching update...', 'info');
      const prevText = _runUpdateBtn.textContent;
      _runUpdateBtn.disabled = true;
      _runUpdateBtn.textContent = 'OPENING...';
      const result = await window.valhalla.runUpdate();
      if (!result || !result.ok) {
        addLog(`Update failed: ${result?.error || 'Unknown error'}`, 'lose');
        setAction('Update failed');
        _runUpdateBtn.disabled = false;
        _runUpdateBtn.textContent = prevText;
      } else {
        if (result.mode === 'open-page') {
          addLog('Opened the update download page in your browser.', 'info');
          setAction('Update page opened');
          _runUpdateBtn.disabled = false;
          _runUpdateBtn.textContent = prevText;
        } else {
          addLog('Update launched. GUI will close...', 'info');
        }
      }
    } catch (e) {
      addLog(`Update error: ${e.message || e}`, 'lose');
      setAction('Update error');
      _runUpdateBtn.disabled = false;
      _runUpdateBtn.textContent = 'DOWNLOAD & INSTALL UPDATE';
    }
  });
}
const _installDepsBtn = $('#btnInstallDeps');
if (_installDepsBtn) {
  _installDepsBtn.addEventListener('click', async () => {
    try {
      setAction('Launching installer…');
      addLog('Launching dependency installer (UAC prompt will appear)...', 'info');
      const prevText = _installDepsBtn.textContent;
      _installDepsBtn.disabled = true;
      _installDepsBtn.textContent = 'LAUNCHING...';
      const result = await window.valhalla.installDeps();
      if (!result || !result.ok) {
        addLog(`Install failed: ${result?.error || 'Unknown error'}`, 'lose');
        setAction('Install failed');
        _installDepsBtn.disabled = false;
        _installDepsBtn.textContent = prevText;
      } else {
        addLog(`Setup launched. Check log: ${result.logPath || 'C:\\ProgramData\\LAPLACE\\setup-all.log'}`, 'info');
        setAction('Installer launched');
        // Make progress visible for non-technical users: open the log automatically.
        if (window.valhalla.openSetupLog) {
          setTimeout(async () => {
            try { await window.valhalla.openSetupLog(); } catch {}
          }, 1200);
        }
        // Re-enable after a short delay so the user can click again if they cancelled UAC.
        setTimeout(() => {
          _installDepsBtn.disabled = false;
          _installDepsBtn.textContent = prevText;
        }, 4000);
      }
    } catch (e) {
      addLog(`Install error: ${e.message || e}`, 'lose');
      setAction('Install error');
      _installDepsBtn.disabled = false;
      _installDepsBtn.textContent = 'INSTALL ON THIS PC';
    }
  });
}

const _openSetupLogBtn = $('#btnOpenSetupLog');
if (_openSetupLogBtn) {
  _openSetupLogBtn.addEventListener('click', async () => {
    try {
      const result = await window.valhalla.openSetupLog();
      if (!result || !result.ok) {
        addLog(`Setup log not available yet: ${result?.error || 'Unknown error'}`, 'warn');
      } else {
        addLog(`Opened setup log: ${result.logPath}`, 'info');
      }
    } catch (e) {
      addLog(`Open setup log failed: ${e.message || e}`, 'lose');
    }
  });
}

// Listen for install-deps-result event from main process
if (window.valhalla.onInstallDepsResult) {
  window.valhalla.onInstallDepsResult((data) => {
    if (data.success) {
      addLog(data.message || 'Setup completed.', 'win');
    } else {
      addLog(`Setup failed: ${data.error || 'Unknown error'}`, 'lose');
    }
  });
}

// --- BOT tab: Watchdog / Remote Support toggles (persist; backend on change only) ---
const _watchdogToggle = $('#inputWatchdogToggle');
const _supportToggle = $('#inputSupportToggle');
const _WATCHDOG_KEY = 'valhalla_watchdog_enabled';
const _SUPPORT_KEY  = 'valhalla_support_enabled';
if (_watchdogToggle) {
  const saved = localStorage.getItem(_WATCHDOG_KEY);
  _watchdogToggle.checked = (saved === null) ? true : (saved === '1');
  _watchdogToggle.addEventListener('change', async () => {
    const on = _watchdogToggle.checked;
    localStorage.setItem(_WATCHDOG_KEY, on ? '1' : '0');
    if (on) {
      try { await window.valhalla.runWatchdog(); } catch (e) { console.warn(e); }
    }
  });
}
if (_supportToggle) {
  (async () => {
    try {
      const env = await window.valhalla.getEnv();
      const v = (env && env.support_enabled) ? String(env.support_enabled).toLowerCase() : '1';
      _supportToggle.checked = ['1', 'true', 'yes'].includes(v);
    } catch { _supportToggle.checked = true; }
  })();
  _supportToggle.addEventListener('change', async () => {
    const on = _supportToggle.checked;
    try { await window.valhalla.toggleSupport(on); } catch (e) { console.warn(e); }
  });
}

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

$('#btnStart').addEventListener('click', async () => {
  // Syncモード用: 起動時にサーバーから最新の推奨テーブルを取得
  await fetchRecommendedTables();
  const config = {
    ...loadSettings(),
    site_api_key: LAPLACE_API_KEY,
    resume_results: (typeof results !== 'undefined' && Array.isArray(results)) ? results.slice() : [],
    recommended_tables: getEnabledRecommendedTables(),
  };
  const hasPrev = localStorage.getItem('valhalla_session_state');
  if (hasPrev) {
    const choice = await showContinueDialog();
    if (choice === 'cancel') return;
    config.resume = (choice === 'continue');
  } else {
    config.resume = false;
  }
  if (!config.resume) {
    // 新規開始: スナップショットをクリア (Python から最初の balance で再初期化される)
    _sessionOpenBalance = null;
    // daily_open はその日のうちは保持 (日次は再開でリセットしない)
    sessionTotal = 0;
    _persistBalanceSnapshot();
    updateSessionDisplay();
    resetFeed();
  } else {
    // Resume: Supabaseからgui_stateを復元 → Python から届く status で上書きされる
    await restoreGuiStateFromServer();
  }
  config.resume_results = Array.isArray(results) ? results.slice() : [];
  _startedAt = Date.now();
  setRunning(true);
  setPhase('scanning', 'starting...');
  addLog('Bot starting...', 'info');
  try {
    await window.valhalla.startBot(config);
    addLog('Bot started.', 'info');
  } catch (e) {
    addLog(`Start failed: ${e.message || e}`, 'lose');
    setRunning(false);
  }
});

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
  await window.valhalla.stopBot();
  setRunning(false);
  addLog('Bot stopped.', 'info');
});

function setRunning(running) {
  isRunning = running;
  $('#btnStart').disabled = running;
  $('#btnStop').disabled = !running;
  $('#btnSkip').disabled = !running;
}

// SKIP TABLE: request agent to exit current table and find new one
$('#btnSkip').addEventListener('click', async () => {
  if (!isRunning) return;
  try {
    await window.valhalla.sendCommand({ type: 'skip_table' });
    addLog('Skip table requested. Searching for new table...', 'info');
  } catch (e) {
    addLog(`Skip failed: ${e.message || e}`, 'lose');
  }
});

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
  telegram_bot_token: '',
  telegram_chat_id: '',
  user_email: '',
  dry_run: false,
  bet_mode: 'counter',
  counter_params: null,
  param_candidate: 'auto',
};
const ALLOWED_BET_MODES = new Set(['counter', 'counter_seq7']);

function normalizeBetMode(mode) {
  return ALLOWED_BET_MODES.has(mode) ? mode : 'counter';
}

function normalizeProfitSessionLimit(value) {
  const n = Number.isFinite(Number(value)) ? Math.floor(Number(value)) : 0;
  return n >= 0 ? n : 0;
}

const SITE_URL = 'https://bafather.uk';
const LAPLACE_API_KEY = 'c6gDoe0xIyBOTQ7bvzRaAHNYn4ZE1W9Mriumqkw8Shf5Jlsd';

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
  _guiSyncPending = true;
  if (_guiSyncTimer) return; // already scheduled
  _guiSyncTimer = setTimeout(() => {
    _guiSyncTimer = null;
    if (_guiSyncPending) {
      _guiSyncPending = false;
      syncGuiStateToServer();
    }
  }, 5000); // debounce 5s
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
  function switchTab(active) {
    $$('.modal-tab').forEach(t => t.classList.remove('active'));
    $$('.tab-content').forEach(c => c.classList.add('hidden'));
    if (active === 'bot') {
      $('#tabBotBtn').classList.add('active');
      $('#tabBotContent').classList.remove('hidden');
    } else {
      $('#tabSystemBtn').classList.add('active');
      $('#tabSystemContent').classList.remove('hidden');
    }
  }
  $('#tabBotBtn').addEventListener('click', () => switchTab('bot'));
  $('#tabSystemBtn').addEventListener('click', () => switchTab('system'));
}

$('#btnSettings').addEventListener('click', () => {
  $('#settingsModal').classList.remove('hidden');
  const s = loadSettings();
  $('#inputChipBase').value = s.chip_base;
  $('#inputProfitTarget').value = s.profit_target;
  if ($('#inputProfitSessionLimit')) $('#inputProfitSessionLimit').value = s.profit_session_limit ?? 0;
  $('#inputLossCut').value = s.loss_cut;
  if ($('#inputTelegramToken')) $('#inputTelegramToken').value = s.telegram_bot_token || '';
  $('#inputTelegramChat').value = s.telegram_chat_id || '';
  $('#inputUserEmail').value = s.user_email || '';
  $('#inputDryRun').checked = !!s.dry_run;
  $('#inputBetMode').value = s.bet_mode || 'counter';
  loadParamCandidates(s.param_candidate || 'auto');
  // Reset to BOT tab
  $$('.modal-tab').forEach(t => t.classList.remove('active'));
  $$('.tab-content').forEach(c => c.classList.add('hidden'));
  $('#tabBotBtn').classList.add('active');
  $('#tabBotContent').classList.remove('hidden');
});
$('#settingsClose').addEventListener('click', () => $('#settingsModal').classList.add('hidden'));
$('#btnSaveSettings').addEventListener('click', async () => {
  // IME/composition の入力が確定していないと value が古いまま読まれる事があるため、
  // クリック時にフォーカスを外して 1tick 待ってから値を読む。
  try { document.activeElement?.blur?.(); } catch {}
  await new Promise((r) => setTimeout(r, 0));

  const settings = {
    chip_base: parseFloat($('#inputChipBase').value) || 1,
    profit_target: parseFloat($('#inputProfitTarget').value) || 50,
    profit_session_limit: normalizeProfitSessionLimit($('#inputProfitSessionLimit')?.value),
    loss_cut: parseFloat($('#inputLossCut').value) || 200,
    telegram_bot_token: ($('#inputTelegramToken') ? $('#inputTelegramToken').value.trim() : ''),
    telegram_chat_id: $('#inputTelegramChat').value.trim(),
    user_email: $('#inputUserEmail').value.trim(),
    dry_run: $('#inputDryRun').checked,
    bet_mode: $('#inputBetMode').value || '1drop',
    counter_params: null,
    param_candidate: 'auto',
  };
  const paramSelect = $('#inputParamCandidate');
  let useCloudParams = false;
  if (paramSelect && paramSelect.value && paramSelect.value !== 'auto') {
    const idx = Number(paramSelect.value);
    const candidate = _paramCandidates[idx];
    if (candidate) {
      settings.counter_params = {
        entry_window: candidate.entry_window,
        entry_threshold: candidate.entry_threshold,
        exit_drop3_limit: candidate.exit_drop3_limit,
        exit_drop5_immediate: candidate.exit_drop5_immediate,
      };
      settings.param_candidate = paramSelect.value;
    }
  } else if (paramSelect && paramSelect.value === 'auto') {
    useCloudParams = true;
  }
  localStorage.setItem('valhalla_settings', JSON.stringify(settings));
  $('#settingsModal').classList.add('hidden');
  addLog(`Settings saved. Base:$${settings.chip_base} Target:$${settings.profit_target} ProfitSessions:${settings.profit_session_limit} LossCut:$${settings.loss_cut}`, 'info');

  // Live-update profit_target & loss_cut if session is running
  if (isRunning) {
    try {
      const updateCfg = {
        profit_target: settings.profit_target,
        profit_session_limit: settings.profit_session_limit,
        loss_cut: settings.loss_cut,
      };
      if (settings.counter_params) {
        updateCfg.entry_window = settings.counter_params.entry_window;
        updateCfg.entry_threshold = settings.counter_params.entry_threshold;
        updateCfg.exit_drop3_limit = settings.counter_params.exit_drop3_limit;
        updateCfg.exit_drop5_immediate = settings.counter_params.exit_drop5_immediate;
      } else if (useCloudParams) {
        updateCfg.use_cloud_params = true;
      }
      await window.valhalla.sendCommand({
        type: 'update_config',
        config: updateCfg,
      });
      // Live BET mode switch
      await window.valhalla.sendCommand({
        type: 'change_mode',
        mode: settings.bet_mode,
      });
      addLog(`Live config update sent (profit/loss + mode: ${settings.bet_mode}).`, 'info');
    } catch (e) {
      addLog(`Live update failed: ${e.message || e}`, 'lose');
    }
  }
});

function loadSettings() {
  try {
    const stored = JSON.parse(localStorage.getItem('valhalla_settings') || '{}');
    const merged = { ...DEFAULT_SETTINGS, ...stored };
    merged.bet_mode = normalizeBetMode(merged.bet_mode);
    merged.profit_session_limit = normalizeProfitSessionLimit(merged.profit_session_limit);
    return merged;
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

// --- Developer Mode ---
const DEV_PASSWORD = 'laplace1749';

function isDevMode() {
  return localStorage.getItem('valhalla_dev_mode') === '1';
}

function setDevMode(on) {
  if (on) localStorage.setItem('valhalla_dev_mode', '1');
  else localStorage.removeItem('valhalla_dev_mode');
  applyDevMode();
}

function applyDevMode() {
  const on = isDevMode();
  const panel = $('#devPanel');
  const status = $('#devModeStatus');
  if (panel) panel.classList.toggle('hidden', !on);
  if (status) status.textContent = `Developer Mode: ${on ? 'ON (click to disable)' : 'OFF'}`;
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
      syncGuiStateToServer(); // 停止時に即時保存
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
// 重要: valhalla_daily_pnl (14日履歴) と valhalla_balance_snapshot (今日の起点残高)
// は GUI 起動時に保持。20% 課金の計算基準になるので消してはいけない。
// セッション途中状態 (valhalla_session_state) だけクリアする。
localStorage.removeItem('valhalla_session_state');
sessionTotal = 0;
results.length = 0;
setRunning(false);
// 前回のスナップショットを復元 (あれば)
_restoreBalanceSnapshot();
updateSessionDisplay();
setAction('Ready. Press START to begin.');
addLog('LAPLACE ready. All data cleared.', 'info');
renderDailyPnl();
renderFeed();
renderRecent();
applyDevMode();
initModalTabs();
