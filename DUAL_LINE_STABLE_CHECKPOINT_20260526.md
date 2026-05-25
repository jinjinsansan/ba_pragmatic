# Dual Line Stable Checkpoint - 2026-05-26

## Checkpoint Commit

- Commit: `9b42dbf Stabilize dual-line live bet flow`
- Branch at commit time: `feat/dual-line`
- Main changed files:
  - `dual_line_pragmatic_bot.py`
  - `dual_line_live_executor.py`
  - `copytrade_gui/src/main.js`

This checkpoint is the current return point for Dual Line Mode. At this point, live BET execution is significantly more stable than earlier dual-line builds.

## What Works Now

- GUI dual-line live mode starts and enters Pragmatic multi-area monitoring.
- Multi-area WS receives `BETSOPEN` / `BETSCLOSED` / `GAME-ID` events for many baccarat tables.
- Speed/Turbo baccarat tables are not excluded.
- Private / unsupported baccarat tables remain excluded from target BET handling.
- VPS decisions are received through normal polling and a short-poll fallback.
- Stale prepositions and stale decisions are guarded so old signals are not bet blindly.
- Wrong-hand protection remains active through `game_id` mismatch checks.
- Actual Stake/Pragmatic bet confirmation is detected through game WS.
- Confirmed actual bet amount is stored separately from planned amount.
- SmallSEQ `$3` style bets can use repeated chip clicks more quickly.
- Repeated chip clicks reuse cached side coordinates after the first successful side click.
- GUI receives money/SEQ status on startup and resume, so the signal panel no longer has to remain blank until a result.
- Locally confirmed bets can be settled from observed hand buffers by exact confirmed `game_id`.
- When a settled result arrives while the DB row is still `processing`, the GUI rewrites the decision as `done` with actual bet amount and bet id.
- Dual-line engine auto-restart is disabled by default to avoid killing the live browser during an unexpected engine stop.
- Spawn failure now resets GUI process state instead of leaving the app stuck in `EBUSY`.

## Verified

- Local syntax check passed:
  - `python -m py_compile dual_line_pragmatic_bot.py dual_line_live_executor.py`
- Build completed:
  - `copytrade_gui/build_staging/engine/bacopy_engine.exe`
- Built engine was deployed to Bafather:
  - `C:/Users/Administrator/AppData/Local/Programs/bacopy-copytrade-gui/resources/engine/bacopy_engine.exe`
- After launch on 2026-05-26 around 00:17 JST:
  - `bacopy_engine` and `camoufox` were running.
  - Multi-area table ID capture was active.
  - WS events were flowing.
  - Old stale decision rows were not allowed to drive new live betting.

## Important Runtime Notes

- The current working money mode during the latest verification was SmallSEQ `$3` start.
- The system should now be judged by the full chain:
  - `decision received`
  - `TRY-BET`
  - `BET confirmed`
  - `result posted settled/done`
  - GUI signal panel / SEQ / W-L-T update
- A BET click success alone is not enough. The settlement and GUI update must also be confirmed.

## Known Remaining Risks

- Some Speed Baccarat signals can still be skipped if the signal is already too old by the time GUI receives or prepares it.
- Some opportunities may still be missed when multiple prepositions compete and the selected target does not become the final signal.
- If the confirmed `game_id` is no longer present in recent local buffers, settlement can still fail or time out. The current fix improves this path but does not prove every edge case is impossible.
- Existing old DB rows may remain in `processing`, `skipped_stale`, or `error`. They should not be treated as current session behavior unless their timestamp is after this checkpoint launch.
- Build artifacts and unrelated untracked files were intentionally not committed.

## Rollback / Return Point

If future changes break dual-line live betting, return first to commit:

```text
9b42dbf Stabilize dual-line live bet flow
```

This is the current known-good baseline for:

- more reliable live BET execution,
- faster multi-chip click betting,
- GUI startup/SEQ display,
- and improved settlement propagation back to GUI/DB.
