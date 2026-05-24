# Dual Line Stable Checkpoint - 2026-05-24

## Status

This commit is the current known-good checkpoint for Dual Line Mode.

As observed on 2026-05-24 JST, after this state was deployed to bafather:

- Telegram/VPS prepositions and decisions continued to arrive.
- When a signal arrived, live betting executed successfully.
- Speed Baccarat tables were not excluded and were bet through multi-area mode.
- The target table was scrolled into view and kept near the screen center.
- The bet tile stayed visible through bet placement and result display.
- Recent observed successful examples included:
  - Speed Baccarat 6: click bet completed, lpbet confirmed.
  - Speed Baccarat 17: bet showed as `$1.00`, then result showed `PLAYER won $2`.

## Important Behavior To Preserve

- Multi-area click betting is the working path.
- Do not revert to pure WS betting unless it is separately proven to change the real Stake balance.
- Speed Baccarat tables must remain eligible for betting.
- Click target resolution must avoid Player Pair / Banker Pair areas.
- The target table should stay visible after betting so the user can visually confirm both bet placement and result.
- Customer support / idle style dialogs should be auto-clicked when detected.

## Recovery Note

If a future change breaks Dual Line Mode, return to this commit first.

This point is considered the baseline where:

- VPS signal generation is alive.
- bafather receives decisions.
- `CLICK-BET` is emitted.
- `LPBET-CONFIRM` is emitted.
- `LIVE bet sent OK` is emitted.
- `VISIBLE-HOLD center` keeps the table visible.

Future edits should be compared against this behavior before changing the betting flow.
