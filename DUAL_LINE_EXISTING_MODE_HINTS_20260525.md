# Dual Line Existing Mode Hints - 2026-05-25

## Purpose

This note records the design decision from the 2026-05-25 discussion:
dual-line mode should not continue to chase click accuracy alone. The existing
modes already contain the correct answer for bet confirmation, GUI updates, and
SEQ synchronization. Dual-line mode must be aligned with that model before
further live tuning.

No implementation was made as part of this note.

## Current Problem

Dual-line mode is still too ambiguous about whether a real Stake/Pragmatic bet
was placed.

Repeated failure classes:

- `lpbet_not_confirmed`
  - The click path ran, but no trusted LPBET confirmation was observed.
  - This must not be counted as a real bet.
- `hand_id_mismatch_before_click`
  - The VPS signal hand and GUI-visible hand did not match.
  - This is a correct safety stop, not a bug to bypass.
- GUI/SEQ/result state can diverge from actual bet state.
  - GUI may appear to advance while the real Stake bet was not confirmed.
  - Small SEQ must not advance unless a real bet was confirmed.

## Key Finding From Existing Modes

Existing modes do not treat a click as a bet.

They use a bet-confirmation model:

- Check `executor.game_ws._last_confirmed`.
- Check DOM total bet amount with `executor._get_total_bet()`.
- Treat unconfirmed bets as skipped.
- Do not send GUI round results when the bet was not confirmed.
- Do not advance SEQ when the bet was not confirmed.
- Log clearly, for example:
  - `[counter] Unconfirmed - skipped`
  - `[bet] result observed but bet not confirmed - skipping GUI result`

This is the most important design hint for dual-line mode.

## Stronger Confirmation Hints

The existing Pragmatic live executor also uses Stake-side confirmation signals:

- Stake balance delta.
- Stake balance drop from before/after balance.
- Pragmatic game WS bet confirmation.
- DOM bet amount confirmation.
- LPBET send/observe as a weaker signal.

For dual-line mode, LPBET alone should not be the highest-trust final proof.

Recommended trust order:

1. Stake balance delta or Stake balance drop.
2. Pragmatic game WS bet confirmation.
3. DOM visible total bet amount.
4. LPBET observed only.

`lpbet_only` should be treated as untrusted unless paired with a stronger
confirmation source.

## Required Dual-Line Model

Dual-line mode should maintain a confirmed bet ledger.

Only confirmed real bets may enter this ledger:

```text
decision_id
bet_id
table_id
table_name
game_id
side
planned_amount
confirmed_amount
confirm_type
confirmed_at
```

Suggested `confirm_type` values:

```text
stake_delta
stake_balance_drop
game_ws_bet
dom_total
lpbet_only_untrusted
```

Only the first four should be allowed to advance GUI/SEQ/result state.

## GUI And SEQ Rules

The GUI signal panel, Small SEQ, LIVEFEED, W/L/T, and result display must use one
source of truth:

```text
confirmed_bets
```

Rules:

- If a `decision_id` has no confirmed bet, do not advance GUI result state.
- If a `decision_id` has no confirmed bet, do not advance Small SEQ.
- If VPS settlement arrives without a confirmed local bet, ignore it for GUI/SEQ.
- Record it as `ignored_settlement_without_confirmed_bet`.
- Only send `round_result` to the GUI after confirmed bet plus matching result.

## VPS Settlement Rules

VPS result settlement is not proof that Stake accepted the bet.

A VPS settlement should be applied only when all of these match:

```text
decision_id matches
bet_id exists in confirmed_bets
table_id matches
game_id matches
side matches
```

If any required field is missing or mismatched, do not advance GUI/SEQ.

## Hand Mismatch Rule

`hand_id_mismatch_before_click` is a safety feature.

It should become an explicit safe skip:

```text
safe_skip_wrong_hand
expected_game_id
actual_game_id
table_id
decision_id
```

This prevents betting on a hand that does not match the six-pattern signal.

## API Status Rule

`bet_sent` is an intermediate state.

Recommended status model:

- `processing`: decision accepted or bet sent but not settled.
- `error`: confirmed failure or safe skip that should close the decision.
- `done`: final settled result after a confirmed local bet.

Avoid posting `bet_sent` as final `done`.

## Recovery Hints From Existing Modes

Dual-line mode should reuse or mirror existing recovery behavior:

- Dismiss `session elsewhere` modal.
- Dismiss inactivity modal.
- For hard session-ended modal, do not press OK blindly; return to lobby.
- Recover when Stake balance WS becomes silent.
- Recover when Pragmatic game WS becomes silent.
- Continue modal recovery while waiting for bet confirmation.
- Keep the browser in a recoverable lobby/multi-area state.

Relevant existing implementation areas:

- `agent_api.py`
  - Existing bet-confirmed gating before GUI/result updates.
- `bacopy_executor_pragmatic_ws_live.py`
  - Stake balance delta/balance-drop confirmation.
  - Session/inactivity/modal recovery.
  - WS silence recovery.
- `dual_line_live_executor.py`
  - Current LPBET detection and hand mismatch guard.
- `dual_line_pragmatic_bot.py`
  - Current decision polling, pending decisions, money status, and settlement flow.
- `dual_line_money.py`
  - Small SEQ state and result application.
- `copytrade_gui/src/renderer/app.js`
  - GUI `round_result` and `money_status` rendering.

## Implementation Plan To Review Before Coding

1. Read existing bet confirmation flow in `agent_api.py`.
2. Read Stake balance confirmation flow in `bacopy_executor_pragmatic_ws_live.py`.
3. Add a dual-line confirmed-bet ledger.
4. Change dual-line settlement so only confirmed bets advance GUI/SEQ.
5. Treat unconfirmed LPBET/click paths as safe skip or error, not GUI result.
6. Keep hand mismatch as safe skip.
7. Change `bet_sent` posting to remain intermediate, not final `done`.
8. Add decision trace logging:

```text
decision_id
table_id
table_name
signal_game_id
preposition_game_id
before_click_game_id
click_result
lpbet_observed
stake_confirmed
dom_confirmed
final_confirm_type
final_status
```

## Expected Result

After this refactor, the remaining problem should no longer be ambiguous bet
state. The system should clearly classify every decision as one of:

```text
confirmed_bet_then_settled
safe_skip_wrong_hand
safe_skip_unconfirmed_bet
bet_rejected
table_not_found
bet_window_missed
session_recovery_needed
```

At that point, remaining work becomes reducing speed-table missed opportunities,
not guessing whether a bet really happened.
