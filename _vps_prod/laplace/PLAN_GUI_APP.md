# MaruBatsu Bot — GUI Desktop App Development Plan

> **Purpose**: Step-by-step plan to build a distributable desktop GUI application.
> **Core Requirement**: The betting logic must be completely hidden from users. All computation happens server-side.
> **Language**: All UI text in English.
> **Created**: 2026-04-04

---

## Architecture Overview

```
User's PC                                Your VPS Server
┌──────────────────────────┐          ┌─────────────────────────┐
│  Desktop App (Electron)  │          │  Control Server          │
│                          │          │                          │
│  ┌────────────────────┐  │          │  ┌──────────────────┐   │
│  │  Embedded Browser  │  │  REST    │  │  Logic Engine     │   │
│  │  (Stake.com table) │  │  API     │  │  (SEQ, OS, slash) │   │
│  └────────────────────┘  │ ◄──────► │  │  100% server-side │   │
│                          │          │  └──────────────────┘   │
│  ┌────────────────────┐  │          │                          │
│  │  UI Panel          │  │          │  ┌──────────────────┐   │
│  │  Start/Stop/Status │  │          │  │  License Manager  │   │
│  │  P&L Display       │  │          │  └──────────────────┘   │
│  └────────────────────┘  │          │                          │
│                          │          │  ┌──────────────────┐   │
│  ┌────────────────────┐  │          │  │  Admin Dashboard  │   │
│  │  Local Agent (Py)  │  │          │  │  (all users view) │   │
│  │  - Browser control │  │          │  └──────────────────┘   │
│  │  - WS intercept    │  │          │                          │
│  │  - BET execution   │  │          │  ┌──────────────────┐   │
│  │  - NO logic here   │  │          │  │  Telegram Notify  │   │
│  └────────────────────┘  │          │  └──────────────────┘   │
│                          │          │                          │
│  Stake credentials       │          │  No user credentials    │
│  NEVER leave this PC     │          │  stored here            │
└──────────────────────────┘          └─────────────────────────┘
```

## Logic Secrecy Design

The client (EXE) knows NOTHING about the betting logic:

1. **Client sends**: table results (Player/Banker/Tie) to server
2. **Server responds**: `{"action": "bet", "side": "player", "amount": 5}` or `{"action": "wait"}`
3. **Client executes**: the instruction blindly
4. The client has zero knowledge of SEQ, overshoot, slashed, sets, or any internal state
5. All state is maintained server-side per user session
6. The EXE binary contains no logic code — only browser control + API communication
7. Even if decompiled, no betting strategy can be extracted

---

## Phase 1: Server API (Week 1-2)

### 1.1 Tech Stack
- **Framework**: FastAPI (Python)
- **Database**: PostgreSQL
- **Auth**: License key + JWT token
- **Hosting**: VPS ($5-10/month, e.g. Hetzner, Vultr)

### 1.2 API Endpoints

```
POST /api/auth/activate
  Body: { license_key: "XXXX-XXXX-XXXX" }
  Response: { token, expires_at, user_id }

POST /api/session/start
  Header: Authorization: Bearer <token>
  Body: { table_name, balance }
  Response: { session_id, chip_base, loss_cut }

POST /api/session/result
  Header: Authorization: Bearer <token>
  Body: { session_id, result: "player"|"banker"|"tie", balance }
  Response: { action: "bet"|"wait"|"stop", side, amount, message }

POST /api/session/bet_result
  Header: Authorization: Bearer <token>
  Body: { session_id, won: bool, balance }
  Response: { status, cumulative_profit, message }

GET /api/session/status
  Header: Authorization: Bearer <token>
  Response: { running, profit, sets_count, current_turn, ... }

GET /api/app/version
  Response: { latest_version, download_url, force_update }

POST /api/support/message
  Body: { license_key, message }
```

### 1.3 Database Schema

```sql
users (id, license_key, email, plan, expires_at, active, created_at)
sessions (id, user_id, started_at, ended_at, total_bets, profit, status)
-- Logic state stored in server memory (Redis) or encrypted DB column
-- Never exposed via API
```

### 1.4 Admin Dashboard
- Simple web page (React or even just Telegram bot)
- View all active users, their P&L, session history
- Activate/deactivate licenses
- Broadcast messages to all users

---

## Phase 2: Electron Desktop App (Week 3-5)

### 2.1 Tech Stack
- **Framework**: Electron 30+
- **Frontend**: React 18 + TailwindCSS
- **Theme**: Dark mode, modern design
- **Browser**: Electron BrowserView (embedded Chromium for Stake.com)
- **Backend IPC**: Python child process communicating via stdin/stdout JSON

### 2.2 App Window Layout

```
┌─────────────────────────────────────────────────────────────┐
│  ≡  MaruBatsu Bot                    v1.0.0     ─  □  ×   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                                                       │  │
│  │              EMBEDDED BROWSER                         │  │
│  │              (Stake.com live table view)               │  │
│  │              User sees the actual game                 │  │
│  │                                                       │  │
│  │                                                       │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Status: ● Running          Balance: $297.29          │  │
│  │  Session: #3                Profit: +23 chips ($23)   │  │
│  │  Round: 47                  Win Rate: 57.4%           │  │
│  │  Today P&L: +$31.00        Bets: 34W / 26L / 2T      │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │  ▶ START │  │  ■ STOP  │  │ ↻ UPDATE │  │ ✉ SUPPORT │  │
│  └──────────┘  └──────────┘  └──────────┘  └───────────┘  │
│                                                             │
│  License: Active until May 4, 2026          ⚙ Settings    │
└─────────────────────────────────────────────────────────────┘
```

### 2.3 Settings Panel
- Stake login (username/password stored locally, encrypted)
- Chip base ($1, $2, $5)
- Profit target / Loss cut
- Telegram notification toggle + chat ID
- Language (English only for v1)

### 2.4 Color Scheme (Dark Theme)
```css
Background:   #0f1117 (deep dark)
Surface:      #1a1d27 (card background)
Primary:      #6366f1 (indigo, buttons)
Success:      #22c55e (green, profit)
Danger:       #ef4444 (red, loss)
Text:         #e2e8f0 (light gray)
Text Muted:   #94a3b8 (gray)
Border:       #2d3348
```

### 2.5 Key Features
- **Auto-update**: Electron-updater checks server on startup
- **License gate**: App won't start without valid license
- **Crash recovery**: On restart, resumes from server-side state
- **Tray icon**: Minimize to system tray, keep running
- **Activity log**: Scrollable log panel (no logic details, just "Bet placed", "Won", "Lost")

---

## Phase 3: Python Agent (Packaged inside Electron) (Week 4-5)

### 3.1 Role
The Python agent handles ONLY:
- Camoufox/Playwright browser automation
- Stake.com login
- WebSocket interception (game results)
- DOM manipulation (chip selection, bet spot click)
- Sending results to server API
- Receiving bet instructions from server API
- Executing bet instructions

### 3.2 What the agent does NOT contain
- No SEQ array
- No overshoot calculation
- No slashed logic
- No set management
- No profit tracking logic
- No strategy decisions

### 3.3 Communication Flow

```
1. Agent detects round result (Player/Banker/Tie)
2. Agent → Server: POST /api/session/result { result: "player" }
3. Server runs logic internally, responds:
   { action: "bet", side: "player", amount: 5.0 }
4. Agent executes: select $5 chip → click Player spot
5. Agent waits for outcome
6. Agent → Server: POST /api/session/bet_result { won: true }
7. Server responds: { status: "ok", message: "Set #3 complete" }
8. Agent displays message in UI log
```

### 3.4 Packaging
- PyInstaller → single folder dist
- Bundled inside Electron app's resources
- Electron spawns Python process on startup
- IPC via stdin/stdout (JSON lines)

---

## Phase 4: Build & Distribution (Week 6)

### 4.1 Build Pipeline
```
1. npm run build          → React frontend
2. pyinstaller agent.py   → Python agent
3. electron-builder       → Windows installer (.exe)
   - Includes: Electron + React + Python agent + Camoufox
   - Code signing (optional but recommended)
```

### 4.2 Installer
- NSIS installer (.exe) for Windows
- ~150-200MB (Electron + Camoufox + Python runtime)
- Install to Program Files
- Desktop shortcut + Start menu entry

### 4.3 Auto-Update
- electron-updater checks `/api/app/version` on startup
- If new version: "Update available" button lights up
- One-click update, auto-restart

### 4.4 License Distribution
- You generate license keys on admin dashboard
- Send key to user
- User enters key on first launch
- Server validates and activates

---

## Phase 5: Admin Dashboard (Week 6-7)

### 5.1 Web Dashboard (for you)
- Simple React page hosted on same VPS
- Protected by admin password

### 5.2 Features
```
┌─────────────────────────────────────────────────────────┐
│  Admin Dashboard                                        │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  Active Users: 12/20        Total Bets Today: 1,847     │
│  Total Profit Today: +$234  Server Load: 3%             │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │ User       Status   Profit   Bets   Version     │    │
│  │ user_001   ● Run    +$45     234    v1.0.0      │    │
│  │ user_002   ● Run    -$12     89     v1.0.0      │    │
│  │ user_003   ○ Stop   +$78     312    v0.9.2  ⚠  │    │
│  │ user_004   ● Run    +$5      47     v1.0.0      │    │
│  │ ...                                              │    │
│  └─────────────────────────────────────────────────┘    │
│                                                         │
│  [Generate License]  [Broadcast Message]  [View Logs]   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 5.3 License Management
- Generate single/bulk license keys
- Set expiry (30 days, 90 days, lifetime)
- Activate / Deactivate / Revoke
- View usage per license

---

## Development Strategy: You Are User #1

The GUI app should be built for YOU first. You use it to actually bet.
Once validated, add server integration and distribute to other users.

### Phase 0: Local GUI (You only, no server needed)
- Electron app with embedded browser + Python agent
- Logic runs locally (same as current Python scripts)
- You bet with it daily, find bugs, refine UX
- This is your personal tool — fully functional standalone

### Phase 1: Server API
- Move logic to server
- Add license system
- Your app switches from local logic to server API
- You keep using the same GUI — now server-powered

### Phase 2+: Distribution
- Other users get the same EXE (without local logic)
- They must connect to your server to operate

### Admin Mode
- Your license key has `role: admin`
- GUI shows an extra "Dashboard" button (hidden for normal users)
- Dashboard panel inside the app shows all users' status, P&L, controls

## Timeline Summary

| Phase | Task | Duration |
|-------|------|----------|
| 0 | Local GUI for you (Electron + React + Python agent) | Week 1-3 |
| - | You test with real BET using the GUI | Week 3-4 |
| 1 | Server API + Logic Engine | Week 4-5 |
| 2 | Server integration + License system | Week 5-6 |
| 3 | Admin Dashboard (in-app) | Week 6-7 |
| 4 | Build, packaging, installer | Week 7 |
| - | Polish + distribute to first users | Week 8 |

**Total estimated: 7-8 weeks**

---

## Security Checklist

- [ ] License key validated server-side on every API call
- [ ] Logic code exists ONLY on server — zero in client binary
- [ ] Stake credentials encrypted locally (AES-256), never sent to server
- [ ] API communication over HTTPS only
- [ ] Rate limiting on all API endpoints
- [ ] JWT tokens with short expiry (24h) + refresh
- [ ] Python agent obfuscated with PyArmor (additional layer)
- [ ] Electron app uses asar archive (not easily extractable)
- [ ] Admin dashboard behind authentication
- [ ] Server logs do not contain user credentials

---

## Cost Estimate

| Item | Monthly Cost |
|------|-------------|
| VPS (Hetzner CX22) | $5-10 |
| Domain + SSL | $1 (Cloudflare free SSL) |
| Code signing cert (optional) | $70/year |
| **Total** | **~$10/month** |

Revenue potential: 10 users × $50/month = $500/month

---

## Notes

- Start with Windows only (Stake.com users are primarily Windows)
- macOS support can be added later (Electron supports it natively)
- Mobile is NOT planned (browser automation requires desktop)
- The logic engine on the server can be updated instantly without any client changes
- If the server goes down, the client safely stops betting and preserves state
