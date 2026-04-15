// Minimal smoke test for Settings SAVE behavior (no Electron / DevTools needed).
// Run on Windows: `node gui/scripts/smoke-settings-save.js`
//
// We simulate the relevant DOM nodes and the SAVE click handler logic to ensure:
// - chip_base / profit_target / bet_mode are read from inputs and stored
// - "mode_changed" does not overwrite bet_mode while settings modal is open

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function makeEl(init = {}) {
  return Object.assign(
    {
      value: '',
      checked: false,
      textContent: '',
      classList: {
        _set: new Set(),
        contains(c) { return this._set.has(c); },
        add(c) { this._set.add(c); },
        remove(c) { this._set.delete(c); },
      },
    },
    init
  );
}

// --- Stubs ---
const els = {
  settingsModal: makeEl(),
  inputChipBase: makeEl({ value: '10' }),
  inputProfitTarget: makeEl({ value: '300' }),
  inputProfitSessionLimit: makeEl({ value: '0' }),
  inputLossCut: makeEl({ value: '15000' }),
  inputTelegramToken: makeEl({ value: '' }),
  inputTelegramChat: makeEl({ value: '123' }),
  inputUserEmail: makeEl({ value: 'a@b.com' }),
  inputDryRun: makeEl({ checked: false }),
  inputBetMode: makeEl({ value: 'counter_seq7' }),
};

// Modal open (not hidden)
els.settingsModal.classList.remove('hidden');

function $(sel) {
  if (!sel.startsWith('#')) return null;
  return els[sel.slice(1)] || null;
}

// localStorage stub
const localStorage = {
  _m: new Map(),
  setItem(k, v) { this._m.set(k, String(v)); },
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
};

function normalizeProfitSessionLimit(v) {
  const n = Number.isFinite(Number(v)) ? Math.floor(Number(v)) : 0;
  return n >= 0 ? n : 0;
}

function normalizeBetMode(mode) {
  return (mode === 'counter' || mode === 'counter_seq7') ? mode : 'counter';
}

// --- Logic under test (mirrors renderer/app.js) ---
async function saveSettingsClick() {
  // blur + 1tick
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
  localStorage.setItem('valhalla_settings', JSON.stringify(settings));
  return settings;
}

function onModeChanged(nextModeRaw) {
  const nextMode = normalizeBetMode(nextModeRaw);
  const modal = $('#settingsModal');
  const modalOpen = modal && !modal.classList.contains('hidden');
  if (!modalOpen && $('#inputBetMode')) $('#inputBetMode').value = nextMode;
}

(async () => {
  // mode_changed should NOT overwrite while modal open
  onModeChanged('counter');
  assert($('#inputBetMode').value === 'counter_seq7', 'mode_changed overwrote bet mode while modal open');

  const s = await saveSettingsClick();
  const stored = JSON.parse(localStorage.getItem('valhalla_settings'));

  assert(s.chip_base === 10, 'chip_base not saved');
  assert(s.profit_target === 300, 'profit_target not saved');
  assert(s.bet_mode === 'counter_seq7', 'bet_mode not saved');
  assert(stored.chip_base === 10, 'stored chip_base not saved');
  assert(stored.profit_target === 300, 'stored profit_target not saved');
  assert(stored.bet_mode === 'counter_seq7', 'stored bet_mode not saved');

  // When modal closed, mode_changed can overwrite
  els.settingsModal.classList.add('hidden');
  onModeChanged('counter');
  assert($('#inputBetMode').value === 'counter', 'mode_changed did not update bet mode when modal closed');

  console.log('OK: settings save smoke test passed');
})();
