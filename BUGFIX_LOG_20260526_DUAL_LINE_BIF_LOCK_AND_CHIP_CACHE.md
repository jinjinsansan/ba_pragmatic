# BUGFIX LOG 2026-05-26 Dual-Line BIF Lock / Chip Cache Follow-up

## Baseline

- Working directory: `E:\dev\Cusor\bacopy`
- Baseline commit before this session: `ceb0be8 Document dual-line stable checkpoint`
- The session first restored `dual_line_pragmatic_bot.py` and `dual_line_live_executor.py` to the latest commit baseline, then rebuilt and deployed `bacopy_engine.exe` to Bafather.

## Fix Applied

### 1. Partial BET should not block later signals forever

Observed live log:

- A `$3` BET was attempted.
- Local confirmation repeatedly reported `planned=$3.00 actual=$2.00`.
- While that partial confirmation stayed pending, later VPS decisions were skipped with `SKIP: bet in flight`.

Change:

- In `dual_line_pragmatic_bot.py`, when a confirmed local BET is marked `partial_bet=True` and remains partial for `BACOPY_PARTIAL_BET_BIF_RELEASE_SEC` seconds, the executor-side in-flight lock is released by consuming the confirmed/sent bet id.
- The pending decision still retains `confirmed_bet` and `actual_amount`, so later settlement can continue if the matching hand result is observed.
- Default release delay: `20` seconds.

Purpose:

- Prevent one partial BET confirmation from blocking subsequent VPS signals for the full settlement timeout.

### 2. Live engine duplicate lock recovery

Observed live log after deployment:

- `[dual-line] another live engine is already running; exiting duplicate process`
- No real `bacopy_engine.exe dual-line` process was visible.
- Browser did not launch because the engine exited before Camoufox startup.

Change:

- In `bacopy_engine.py`, stale lockdir PID rejection now verifies that the PID belongs to an actual `bacopy_engine` dual-line process before refusing startup.
- Windows mutex fallback is disabled by default unless `BACOPY_ENABLE_MUTEX_LOCK=1`.

Purpose:

- Avoid stale or unrelated process state preventing GUI engine startup.

## Deployment

Built and deployed to Bafather:

- Remote path: `C:\Users\Administrator\AppData\Local\Programs\bacopy-copytrade-gui\resources\engine\bacopy_engine.exe`
- Backup before latest deploy: `bacopy_engine.exe.bak_20260526_033446`
- Latest deployed exe size: `224481247`
- Latest deployed exe timestamp: `2026-05-26 03:35:34`

## Live Verification

After the lock fix:

- GUI launched.
- `bacopy_engine.exe dual-line --live ...` started.
- `camoufox.exe` started.
- `LIVE mode: LiveBetExecutor loaded` appeared.
- `EXEC-SETUP` completed.
- Multi-area WS traffic (`BETSOPEN`) was flowing.
- Preposition candidates were received and queued.
- User observed that BET occurred again.

## Remaining Issue

### `$3` planned but `$2` confirmed

This is not a money manager issue. It means the click sequence did not complete all planned chip/side clicks inside the active BET window.

Likely cause:

- Chip selection and/or side button clicks are still too slow or not sufficiently cached/prewarmed in the Pragmatic multi-area flow.
- For a `$3` small sequence bet, the plan is normally three `$1` clicks. If only `$2` is confirmed, one click did not land before close or did not register.

Next investigation:

- Inspect the original GUI implementation under the `ba` directory.
- The original Evolution baccarat GUI reportedly placed high-value chip bets reliably using click events.
- Compare its approach for:
  - chip/button coordinate caching,
  - preselected chip handling,
  - direct trusted click dispatch,
  - batching/multiple clicks,
  - avoiding repeated DOM search during the betting window,
  - recovering from stale coordinates.

Next intended fix:

- Cache the target tile, chip controls, and P/B side click coordinates before the signal hand opens.
- At BET time, use verified cached coordinates first.
- Revalidate qpid/table/game id/side before clicking.
- For multi-click plans like `$3 = 1 + 1 + 1`, reuse cached side coordinates for all repeated clicks with minimal inter-click delay.
- Keep the target tile visible/centered during and after BET so the operator can visually confirm.

## Original GUI Reference (`E:\dev\Cusor\ba`)

Checked the original Evolution baccarat GUI implementation:

- `executor.py` scans available chips after table entry in `_scan_available_chips`.
- It precomputes chip plans for `$1` through `$50` into `_chip_plan_cache`.
- `place_bet` reuses one locator and skips chip reselection when the same chip denomination repeats.
- `_calc_chip_plan` prefers fewer clicks and fewer chip switches.
- `_select_chip` uses direct locator clicks with forced click fallback and stack expansion fallback.
- `test_bet.py` and `test_locator.py` confirm that Evolution used direct chip selectors such as `[data-role="chip"][data-value="1"]` and direct bet spots such as `[data-betspot-destination="Player"]`.

The transferable idea is not the exact selector set, because Pragmatic multi-area uses different DOM and stricter table/hand matching. The useful pattern is:

- scan/cache before the bet window,
- avoid repeated DOM searches during the bet window,
- keep the selected chip if the next click uses the same denomination,
- reuse the same bet spot coordinates for repeated clicks,
- confirm actual placed amount after clicking.

## Follow-up Fix: Pragmatic Side Coordinate Cache

Applied after checking the original GUI:

- Added `_bet_click_coord_cache` in `dual_line_live_executor.py`.
- During preposition or same-thread decision queueing, the executor now caches the target tile's P/B/T side button coordinates after preselecting the first chip.
- At BET time, cached coordinates are considered from the first click, not only after the first successful click.
- Before using cached coordinates, the executor validates the point with `document.elementFromPoint` and checks the same `qpid` tile and the expected game id when available.
- Repeated clicks such as `$3 = $1 x 3` can reuse the same validated side coordinates with minimal delay.

Safety rule:

- If the cached coordinate cannot be validated against the target tile/hand, the code falls back to the existing JS coordinate lookup path.
