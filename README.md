# LAPLACE2 — Pragmatic Play Baccarat Strategy Client

Fork of LAPLACE targeted at **Pragmatic Play** baccarat tables on Stake.com.

## Why a separate project

LAPLACE (Evolution Gaming) was found to have structurally unexploitable P/B predictions during testing on 2026-04-15. The friend's working strategy has been operating on **Pragmatic Play** tables, which appear to have different (more predictable) shoe arrangements.

This is a ground-up reuse of LAPLACE's scraper/agent/GUI stack, rebuilt to:
- Scrape Pragmatic Play baccarat lobby and tables (different iframe/WebSocket structure)
- Store data in `analytics_pragmatic.sqlite3` (fresh)
- Implement the friend's strategy: dual-signal Player-only betting with 7-turn SEQ
- Validate against collected Pragmatic shoe data before live deployment

## Status

Initial clone — 2026-04-15. Following components need rework:
- [ ] `scraper.py`: Pragmatic Play DOM/WS (Evolution-specific, will not run as-is)
- [ ] `table_selector.py`: Pragmatic lobby layout
- [ ] `counter_logic.py` / `marubatsu_strategy.py`: swap to friend's Player-only dual-signal logic
- [ ] `agent_api.py`: rewire entry/exit to new signals
- [ ] `gui/`: branding + dependencies reinstall (`npm install`)
- [ ] VPS endpoint: eventually separate from LAPLACE's VPS

## Differences from LAPLACE

| Item | LAPLACE | LAPLACE2 |
|---|---|---|
| Provider | Evolution Gaming | Pragmatic Play |
| Strategy | Counter-bet on both sides (逆張り) | Player-only, dual-signal confirmation |
| SEQ | New SEQ × 5-turn | Old SEQ × 7-turn |
| Entry | Realtime 15-col tereko 80% | Big road chaos → 1+1 tereko trigger |
| Signals | Single (tereko column rate) | Dual (珠盤路 horizontal + 大路 pattern) |
| DB | `analytics_vps.sqlite3` | `analytics_pragmatic.sqlite3` |
| GUI | LAPLACE | LAPLACE2 |

## Setup (after cloning)

```bash
# Python deps (reuse LAPLACE's environment or fresh venv)
pip install -r requirements.txt

# Node deps
cd gui && npm install

# Copy .env from LAPLACE (or create fresh) — DO NOT COMMIT
```

See `cloud_scripts/` for deployment scripts (inherited from LAPLACE).
