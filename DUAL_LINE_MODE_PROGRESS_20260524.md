# Dual Line Mode Progress 2026-05-24

## Scope

This note records the current state of the dual-line mode work as of 2026-05-24 02:30 JST.

The goal is to make the GUI receive VPS-owned six-pattern dual-line signals, prepare the target table in Pragmatic multi-area mode from preposition warnings, and place $1 bets reliably.

## Completed

- VPS-side signal ownership is now the intended source of truth for live GUI betting.
- Live GUI no longer uses local B-line/P-line style signal sources for betting decisions.
- The accepted dual-line signal set is limited to these six pattern keys:
  - `telecho|telecho|B`
  - `telecho|nikoichi|P`
  - `telecho|niconico|P`
  - `niconico|niconico|B`
  - `niconico|dragon|P`
  - `sansan|telecho|P`
- Speed/Turbo Baccarat tables are no longer excluded.
- Private/Priv/Prive, Seotda, and Sic Bac style tables are excluded because $1 betting is not supported or they are outside the target surface.
- VPS preposition output is consumed by the GUI through `/api/preposition`.
- Preposition score 1/2 now requests a multi-area table focus before the final signal.
- Multi-area table targeting now prioritizes qpid/tile IDs such as `TileHeight-<qpid>`.
- Multi-area target preparation now clicks/selects the target tile by default.
- Non-multi-baccarat DOM guard was added so the table focusing JavaScript does not click unrelated Stake page buttons or support/history modals.
- Short text matching was tightened so chip text like `1` does not match table names such as `Thai Baccarat 1`.
- Chip selection has a JS coordinate fallback.
- BET side clicking has qpid-based coordinate selection and reseek behavior.
- If preposition switches to another table after a decision arrives, the executor re-prepares the decision target before betting.
- `lpbet` confirmation wait was increased from the earlier short timeout to 35 seconds because observed Pragmatic confirmation lag was more than 20 seconds in multi-area mode.

## Verified In Logs

- VPS bot is alive and continues producing preposition and decision events.
- Speed/Turbo tables are included in preposition and decision flow.
- Private tables are filtered by code.
- bafather GUI process enters Pragmatic multi-area mode and receives multi-table WebSocket `betsopen` events.
- Preposition focus successfully selected multiple Speed tables, including:
  - `Korean Speed Baccarat 5`
  - `Chinese Speed Baccarat 1`
  - `Vietnamese Speed Baccarat 1`
  - `Speed Baccarat 18`
  - `Speed Baccarat 11`
- A real decision for `Japanese Speed Baccarat 3` was processed:
  - Decision received at `2026-05-24 01:57:59`.
  - `PLAYER $1` click succeeded at `01:58:09`.
  - `lpbet` was observed at `01:58:23`.
  - The old timeout was too short, so it was still marked as failed before this fix.
- A real decision for `Baccarat 2` was processed:
  - Decision received at `2026-05-24 02:11:26`.
  - `PLAYER $1` click succeeded at `02:11:33`.
  - `lpbet` was observed at `02:11:54`.
  - The then-current 20 second timeout was still too short by about 1.2 seconds.
- After increasing the default wait to 35 seconds and restarting at `2026-05-24 02:14:55`, no new real decision had arrived during the subsequent monitoring window, so the 35 second confirmation path still needs one fresh live decision to verify.

## Important Finding

The key remaining failure was not always that the click missed. In observed cases, the click reached Pragmatic and the client later emitted `<lpbet>`, but the executor had already timed out and marked the bet as failed.

Observed confirmation delays:

- `STAKE SPEED BACCARAT`: about 8 seconds after click.
- `Speed Baccarat 16`: about 13 seconds after click.
- `Japanese Speed Baccarat 3`: about 13 seconds after click.
- `Baccarat 2`: about 21 seconds after click.

Because of this, `BACOPY_LPBET_CONFIRM_WAIT_SEC` now defaults to `35.0`.

## Still Incomplete

- Need verify one fresh real decision after the 35 second timeout deployment.
- Some preposition diagnostic probes still show `coords ok=False` immediately after focusing, although subsequent reseek often finds the correct qpid tile. This needs more observation.
- The GUI status counters still show session-local counts and do not prove actual casino settlement by themselves.
- Older VPS DB rows remain in `processing` or `error` from earlier failed attempts. They are historical and should not be interpreted as current 35 second behavior.
- Live Telegram/result reporting may still mark old timed-out rows as errors when they age out. A future cleanup should reconcile cases where `lpbet` arrives after the previous timeout.
- The table focus logic is much better, but should be verified across more targets, especially Speed tables that are off-screen or whose visible labels differ by language.
- The project still has many unrelated untracked build/debug artifacts in the worktree. They were not intentionally included in this checkpoint commit.

## Commands Run

- `python -m py_compile dual_line_live_executor.py`
- `git diff --check -- dual_line_live_executor.py`
- `python test_dual_line.py` earlier in the session after Speed table re-inclusion
- bafather deploy via `.\_deploy_bafather.ps1`
- bafather task restart via scheduled task `BACOPY_DualLine_Live_OneShot`

## Next Steps

1. Keep bafather running on the 35 second confirmation build.
2. Watch the next real VPS decision.
3. Confirm expected sequence:
   - `DECISION`
   - `BET-QUEUED`
   - `BETSOPEN-HIT`
   - `CLICK-BET ... ok`
   - `LPBET-CONFIRM`
   - `bet sent OK + lpbet confirmed`
4. If a fresh 35 second build still times out, increase timeout again or change the logic to keep a delayed `lpbet` listener that retroactively marks the bet as sent when the game ID matches.
