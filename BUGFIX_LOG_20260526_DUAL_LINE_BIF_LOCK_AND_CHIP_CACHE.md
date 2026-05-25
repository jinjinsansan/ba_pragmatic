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

