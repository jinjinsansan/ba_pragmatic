"""BACOPYMASTER 管理コンソールの UI レンダラ.

デスクトップ: 2カラム ダッシュボード (テーブル grid / 操作サイドパネル).
  - 案2: 2カラム活用
  - 案4: 集計ステータスバー
  - 案5: 新ハンド フラッシュ
  - 案6: 緊急 STOP
  - 案7: 選択テーブル詳細

モバイル (<900px): 2モード制.
  - 案1a 待機モード: 全情報表示
  - 案1b 実戦モード: LOOK/PLAYER 巨大ボタン + 最小情報のみ
"""
from __future__ import annotations


def render_master_app(csrf: str) -> str:
    return _HTML.replace("__CSRF__", csrf)


_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="csrf" content="__CSRF__"/>
<title>BACOPYMASTER</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;700;900&family=Share+Tech+Mono&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#05080f;
  --bg-card:rgba(15,20,35,0.85);
  --bg-glass:rgba(20,28,50,0.60);
  --accent:#00e5ff;
  --accent-dim:rgba(0,229,255,0.15);
  --win:#00ff88;
  --lose:#ff3366;
  --tie:#ffcc00;
  --banker:#ff5c8a;
  --player:#00bfff;
  --text:#e0e8f0;
  --text-muted:#7888a0;
  --text-dim:#4a5568;
  --border:rgba(0,229,255,0.12);
  --border-h:rgba(0,229,255,0.38);
  --glow:0 0 20px rgba(0,229,255,0.15);
  --radius:10px;
  --font-hud:'Orbitron',sans-serif;
  --font-mono:'Share Tech Mono',monospace;
  --font-body:'Inter','Segoe UI',sans-serif;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:var(--font-body);background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,229,255,0.035) 1px,transparent 1px),linear-gradient(90deg,rgba(0,229,255,0.035) 1px,transparent 1px);background-size:40px 40px;pointer-events:none;z-index:0;opacity:0.5}
body::after{content:'';position:fixed;top:-100px;left:50%;transform:translateX(-50%);width:900px;height:280px;background:radial-gradient(ellipse at center,rgba(0,229,255,0.06) 0%,transparent 70%);pointer-events:none;z-index:0}
header,main,aside,.stats-bar{position:relative;z-index:1}

/* ======= Header ======= */
header{position:sticky;top:0;background:rgba(3,6,12,0.96);backdrop-filter:blur(10px);border-bottom:1px solid var(--border);padding:12px 24px;display:flex;justify-content:space-between;align-items:center;z-index:20;flex-wrap:wrap;gap:12px}
.brand{font-family:var(--font-hud);font-weight:900;font-size:18px;letter-spacing:6px;color:var(--accent);text-shadow:0 0 12px rgba(0,229,255,0.45)}
header .status{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.pill{display:inline-block;padding:4px 10px;border-radius:999px;font-family:var(--font-mono);font-size:11px;letter-spacing:1px;background:var(--bg-glass);border:1px solid var(--border);color:var(--text-muted);white-space:nowrap}
.pill.ok{color:var(--win);border-color:rgba(0,255,136,0.35);box-shadow:0 0 8px rgba(0,255,136,0.15)}
.pill.warn{color:var(--tie);border-color:rgba(255,204,0,0.35)}
.pill.err{color:var(--lose);border-color:rgba(255,51,102,0.35)}
.pill.active{color:var(--accent);border-color:var(--border-h);box-shadow:0 0 8px rgba(0,229,255,0.3)}
.pill.standby{color:#9fb0c5;border-color:rgba(159,176,197,0.35)}
.pill.offline{color:var(--text-dim);border-color:var(--border)}
button.logout{background:transparent;border:1px solid var(--border);color:var(--text-muted);padding:6px 14px;border-radius:8px;font-family:var(--font-body);font-size:12px;cursor:pointer}
button.logout:hover{border-color:var(--lose);color:var(--lose)}

/* 実戦モード切替ボタン (モバイル) */
.mode-toggle{display:none;background:transparent;border:1px solid var(--accent);color:var(--accent);padding:6px 14px;border-radius:8px;font-family:var(--font-hud);font-size:11px;letter-spacing:2px;cursor:pointer}
@media(max-width:900px){.mode-toggle{display:inline-block}}

/* ======= 集計ステータスバー (案4) ======= */
.stats-bar{background:rgba(10,16,30,0.9);border-bottom:1px solid var(--border);padding:10px 24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px}
.stats-bar .stat-tile{padding:4px 10px;border-left:2px solid var(--border);min-width:0}
.stats-bar .stat-tile .sl{font-family:var(--font-hud);font-size:9px;letter-spacing:2px;color:var(--text-muted);text-transform:uppercase}
.stats-bar .stat-tile .sv{font-family:var(--font-mono);font-size:18px;color:var(--text);font-weight:400;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stats-bar .stat-tile.good .sv{color:var(--win)}
.stats-bar .stat-tile.bad .sv{color:var(--lose)}
.stats-bar .stat-tile.warn .sv{color:var(--tie)}
.stats-bar .stat-tile.accent{border-left-color:var(--accent)}
.stats-bar .stat-tile.accent .sv{color:var(--accent)}

/* ======= Dashboard 2カラム (案2) ======= */
.dashboard{display:grid;grid-template-columns:1fr 380px;gap:16px;padding:16px 24px 60px}
@media(max-width:1100px){.dashboard{grid-template-columns:1fr}}

.main-col{min-width:0}
.side-col{position:sticky;top:100px;align-self:start;display:flex;flex-direction:column;gap:12px;max-height:calc(100vh - 120px);overflow-y:auto}
@media(max-width:1100px){.side-col{position:static;max-height:none}}
.side-col::-webkit-scrollbar{width:6px}
.side-col::-webkit-scrollbar-track{background:transparent}
.side-col::-webkit-scrollbar-thumb{background:var(--border-h);border-radius:3px}

section{margin-bottom:18px}
h2{font-family:var(--font-hud);font-size:12px;letter-spacing:4px;color:var(--accent);margin-bottom:10px;text-transform:uppercase;border-bottom:1px solid var(--border);padding-bottom:6px;display:flex;justify-content:space-between;align-items:center}
h2 .hctl{font-family:var(--font-mono);font-size:10px;color:var(--text-muted);letter-spacing:1px;font-weight:400}

.glass-card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:12px;backdrop-filter:blur(8px);box-shadow:var(--glow)}
.glass-card.active{border-color:var(--border-h);box-shadow:0 0 24px rgba(0,229,255,0.25)}

.label{font-family:var(--font-hud);font-size:10px;letter-spacing:2px;color:var(--text-muted);text-transform:uppercase}
.value{font-family:var(--font-mono);font-size:14px;color:var(--text);margin-top:2px}
.value.big{font-size:20px}
.value.huge{font-size:28px}
.value.small{font-size:12px}
.value.accent{color:var(--accent)}
.value.win{color:var(--win)}
.value.lose{color:var(--lose)}
.value.tie{color:var(--tie)}
.divider{border-top:1px dashed var(--border);margin:10px 0}

/* ======= 選択テーブル詳細 (案7) ======= */
.detail-panel{padding:16px}
.detail-panel.empty{opacity:0.55}
.detail-table-name{font-family:var(--font-hud);font-size:20px;color:var(--accent);text-shadow:0 0 10px rgba(0,229,255,0.4);letter-spacing:1px;line-height:1.15;word-break:break-all}
.detail-roadmap{font-family:var(--font-mono);font-size:28px;letter-spacing:4px;line-height:1.1;margin:14px 0;padding:10px;background:var(--bg-glass);border:1px solid var(--border);border-radius:8px;word-break:break-all;min-height:50px}
.detail-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}
.detail-stats .ds{padding:6px 8px;background:var(--bg-glass);border:1px solid var(--border);border-radius:6px;text-align:center;min-width:0}
.detail-stats .ds .sl{font-size:9px;color:var(--text-muted);font-family:var(--font-hud);letter-spacing:1px}
.detail-stats .ds .sv{font-family:var(--font-mono);font-size:16px;margin-top:2px;word-break:break-all}
.detail-stats .ds.win .sv{color:var(--win)}
.detail-stats .ds.lose .sv{color:var(--lose)}
.detail-stats .ds.tie .sv{color:var(--tie)}
.detail-streak{font-family:var(--font-mono);font-size:12px;color:var(--text-muted);margin-top:8px;padding:6px 8px;border-top:1px dashed var(--border)}
.detail-streak.big{color:var(--tie);font-weight:600}
.favorite-btn{background:none;border:1px solid var(--border);color:var(--text-muted);padding:4px 10px;border-radius:6px;font-size:14px;cursor:pointer;margin-top:8px}
.favorite-btn.on{color:var(--tie);border-color:var(--tie);box-shadow:0 0 8px rgba(255,204,0,0.3)}

/* ======= 操作パネル (巨大 LOOK/PLAYER) ======= */
.action-panel{padding:14px;display:flex;flex-direction:column;gap:10px}
.action-panel .broadcast-info{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;background:var(--bg-glass);border:1px solid var(--border);border-radius:6px;font-family:var(--font-mono);font-size:12px}
.action-panel .broadcast-count{font-family:var(--font-hud);font-size:18px;color:var(--accent);letter-spacing:2px}
.big-btn{padding:22px 16px;background:var(--bg-glass);border:2px solid var(--border);border-radius:14px;color:var(--text);font-family:var(--font-hud);font-weight:900;font-size:22px;letter-spacing:8px;cursor:pointer;transition:all 0.15s;display:flex;flex-direction:column;align-items:center;gap:4px}
.big-btn .sub{font-size:10px;letter-spacing:3px;opacity:0.7;font-weight:400}
.big-btn:disabled{opacity:0.25;cursor:not-allowed}
.big-btn:not(:disabled):hover{transform:translateY(-2px);box-shadow:0 0 28px rgba(0,229,255,0.4)}
.big-btn.look{color:var(--text);border-color:var(--border-h)}
.big-btn.look:not(:disabled):hover{background:rgba(120,136,160,0.12)}
.big-btn.player{color:var(--player);border-color:rgba(0,191,255,0.55);background:linear-gradient(180deg,rgba(0,191,255,0.22),rgba(0,191,255,0.05));text-shadow:0 0 12px rgba(0,191,255,0.5)}
.big-btn.player:not(:disabled):hover{background:linear-gradient(180deg,rgba(0,191,255,0.35),rgba(0,191,255,0.1));box-shadow:0 0 30px rgba(0,191,255,0.5)}
.big-btn.banker{color:var(--banker);border-color:rgba(255,92,138,0.55);background:linear-gradient(180deg,rgba(255,92,138,0.16),rgba(255,92,138,0.03))}
.big-btn.tie{color:var(--tie);border-color:rgba(255,204,0,0.55);background:linear-gradient(180deg,rgba(255,204,0,0.16),rgba(255,204,0,0.03))}
.big-btn.hidden-by-default{display:none}
body.show-bt .big-btn.hidden-by-default{display:flex}
.act-status{font-family:var(--font-mono);font-size:12px;padding:8px 10px;border-radius:6px;background:var(--bg-glass);border:1px solid var(--border);color:var(--text-muted)}
.act-status.ok{color:var(--win);border-color:rgba(0,255,136,0.35)}
.act-status.err{color:var(--lose);border-color:rgba(255,51,102,0.35)}
.act-status.processing{color:var(--tie);border-color:rgba(255,204,0,0.35)}

input,select{width:100%;background:var(--bg-glass);border:1px solid var(--border);color:var(--text);padding:9px 11px;border-radius:6px;font-family:var(--font-body);font-size:12px}
input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 10px rgba(0,229,255,0.25)}

/* ======= テーブル grid ======= */
.filters{display:grid;grid-template-columns:140px 1fr auto;gap:12px;margin-bottom:12px;align-items:center}
@media(max-width:640px){.filters{grid-template-columns:1fr}}
#tableList{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.table-cell{background:var(--bg-glass);border:1px solid var(--border);border-radius:10px;padding:10px 12px;cursor:pointer;transition:all 0.12s;text-align:left;color:var(--text);font-family:var(--font-body);min-height:100px;display:flex;flex-direction:column;gap:5px;position:relative}
.table-cell:hover{border-color:var(--border-h);box-shadow:0 0 12px rgba(0,229,255,0.28);transform:translateY(-1px)}
.table-cell.selected{border-color:var(--accent);background:linear-gradient(180deg,rgba(0,229,255,0.16),rgba(0,229,255,0.03));box-shadow:0 0 22px rgba(0,229,255,0.38)}
.table-cell.selected::before{content:'● 選択中';position:absolute;top:6px;right:8px;font-family:var(--font-mono);font-size:9px;letter-spacing:1px;color:var(--accent)}
.table-cell.favorite::after{content:'★';position:absolute;top:6px;left:8px;color:var(--tie);font-size:14px;text-shadow:0 0 6px rgba(255,204,0,0.6)}
.table-cell .tname{font-family:var(--font-hud);font-weight:700;font-size:12px;letter-spacing:0.5px;color:var(--text);padding-right:60px;padding-left:18px}
.table-cell.selected .tname{color:var(--accent)}
.table-cell .roadmap{font-family:var(--font-mono);font-size:16px;letter-spacing:2px;word-break:break-all;line-height:1.2;min-height:20px}
.dot-B{color:var(--banker);text-shadow:0 0 6px rgba(255,92,138,0.5);font-weight:700}
.dot-P{color:var(--player);text-shadow:0 0 6px rgba(0,191,255,0.5);font-weight:700}
.dot-T{color:var(--tie);text-shadow:0 0 8px rgba(255,204,0,0.6);font-weight:700}
.detail-roadmap .dot-B,.detail-roadmap .dot-P,.detail-roadmap .dot-T{text-shadow:0 0 12px currentColor}
.table-cell .tmeta{display:flex;justify-content:space-between;font-family:var(--font-mono);font-size:10px;color:var(--text-muted);margin-top:auto}

/* 案5: 新ハンド フラッシュ */
@keyframes newHandFlash{0%{box-shadow:0 0 0 2px var(--win),0 0 20px rgba(0,255,136,0.7)}100%{box-shadow:0 0 0 1px var(--border),0 0 0 rgba(0,255,136,0)}}
.table-cell.flash-new{animation:newHandFlash 1.5s ease-out}
@keyframes switchPulse{0%,100%{box-shadow:0 0 22px rgba(255,204,0,0.3)}50%{box-shadow:0 0 36px rgba(255,204,0,0.6)}}
.table-cell.switching{animation:switchPulse 1.2s ease-in-out infinite}
@keyframes pulseWarn{0%,100%{opacity:0.7}50%{opacity:1}}

/* ======= 詳細情報セクション (折りたたみ可) ======= */
details{margin-bottom:18px}
details > summary{cursor:pointer;list-style:none;padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg-glass);font-family:var(--font-hud);font-size:11px;letter-spacing:3px;color:var(--accent);text-transform:uppercase;display:flex;justify-content:space-between;align-items:center}
details > summary::-webkit-details-marker{display:none}
details > summary::after{content:'▾';transition:transform 0.2s}
details[open] > summary::after{transform:rotate(180deg)}
details > .content{padding:10px 0}

/* 学習プログレス */
.progress-wrap{display:grid;grid-template-columns:1fr auto auto;gap:14px;align-items:center}
@media(max-width:640px){.progress-wrap{grid-template-columns:1fr}}
.progressbar{height:14px;background:var(--bg-glass);border:1px solid var(--border);border-radius:999px;overflow:hidden;position:relative}
.progressbar .fill{height:100%;background:linear-gradient(90deg,var(--accent),rgba(0,229,255,0.3));box-shadow:0 0 12px rgba(0,229,255,0.55);transition:width 0.5s}
.recent-bets{max-height:220px;overflow-y:auto;font-family:var(--font-mono);font-size:11px;margin-top:10px;padding-right:4px}
.bet-row{padding:5px 6px;border-bottom:1px dashed var(--border);display:grid;grid-template-columns:70px 55px 50px 1fr auto;gap:8px;align-items:center}
.bet-row.win{color:var(--win)}
.bet-row.lose{color:var(--lose)}
.bet-row.tie{color:var(--tie)}
.bet-row .bside{font-family:var(--font-hud);letter-spacing:1px}

/* Pilot / Executor カード */
#pilotList,#execList{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:10px}
.pilot-card{display:flex;flex-direction:column;gap:6px}
.pilot-card.offline{opacity:0.55}
.pilot-email{font-family:var(--font-mono);color:var(--text);font-size:13px;word-break:break-all;font-weight:500}
.pilot-meta{font-family:var(--font-mono);color:var(--text-muted);font-size:11px}
.pilot-head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.exec-card .top-row{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.exec-card .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}
.exec-card .stat{background:var(--bg-glass);border:1px solid var(--border);border-radius:8px;padding:6px 8px;min-width:0}
.exec-card .stat .label{font-size:9px}
.exec-card .stat .value{font-size:13px;word-break:break-all}
.exec-card .err{color:var(--lose);font-family:var(--font-mono);font-size:11px;padding:6px 8px;background:rgba(255,51,102,0.06);border:1px solid rgba(255,51,102,0.28);border-radius:6px;margin-top:8px}

/* 履歴 */
.history-wrap{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:900px){.history-wrap{grid-template-columns:1fr}}
pre.histlog{margin:0;font-family:var(--font-mono);font-size:11px;color:var(--text-muted);max-height:200px;overflow:auto;white-space:pre-wrap;word-break:break-all}

/* toast */
.toast{position:fixed;bottom:24px;right:90px;padding:12px 18px;background:rgba(5,10,22,0.96);border:1px solid var(--border);border-radius:10px;font-family:var(--font-mono);font-size:12px;z-index:100;transform:translateY(80px);opacity:0;transition:all 0.3s;max-width:360px;word-break:break-all}
.toast.show{transform:translateY(0);opacity:1}
.toast.ok{border-color:rgba(0,255,136,0.45);color:var(--win);box-shadow:0 0 20px rgba(0,255,136,0.2)}
.toast.err{border-color:rgba(255,51,102,0.45);color:var(--lose);box-shadow:0 0 20px rgba(255,51,102,0.2)}

/* ======= 緊急 STOP (案6) ======= */
.emergency-stop{position:fixed;bottom:24px;right:24px;width:56px;height:56px;border-radius:50%;border:2px solid var(--lose);background:rgba(40,5,15,0.9);color:var(--lose);font-family:var(--font-hud);font-weight:900;font-size:11px;letter-spacing:1px;cursor:pointer;box-shadow:0 0 20px rgba(255,51,102,0.4);z-index:50;transition:all 0.15s}
.emergency-stop:hover{transform:scale(1.08);box-shadow:0 0 30px rgba(255,51,102,0.7)}
body.stopped .emergency-stop{background:rgba(120,20,45,0.95);color:#fff;box-shadow:0 0 40px rgba(255,51,102,0.9);animation:stopPulse 1s ease-in-out infinite}
@keyframes stopPulse{0%,100%{box-shadow:0 0 30px rgba(255,51,102,0.5)}50%{box-shadow:0 0 45px rgba(255,51,102,0.9)}}
body.stopped .big-btn.player,body.stopped .big-btn.banker,body.stopped .big-btn.tie{pointer-events:none;opacity:0.15}
body.stopped .stop-banner{display:block}
.stop-banner{display:none;position:sticky;top:64px;background:rgba(120,20,45,0.95);color:#fff;padding:10px 24px;text-align:center;font-family:var(--font-hud);letter-spacing:4px;z-index:19;border-bottom:2px solid var(--lose);box-shadow:0 4px 20px rgba(255,51,102,0.3)}

/* ======= モバイル 2モード (案1) ======= */
@media(max-width:900px){
  body[data-mode="active"] .stats-bar,
  body[data-mode="active"] details,
  body[data-mode="active"] .tables-sec h2,
  body[data-mode="active"] .filters{display:none}
  body[data-mode="active"] #tableList{grid-template-columns:repeat(2,1fr);gap:6px;max-height:38vh;overflow-y:auto}
  body[data-mode="active"] .table-cell{min-height:72px;padding:6px 8px}
  body[data-mode="active"] .table-cell .tname{font-size:11px;padding-right:36px}
  body[data-mode="active"] .table-cell .roadmap{font-size:12px}
  body[data-mode="active"] .table-cell .tmeta{display:none}
  body[data-mode="active"] .big-btn{font-size:26px;padding:30px 10px;letter-spacing:10px}
  body[data-mode="active"] .detail-roadmap{font-size:34px;padding:14px}
  body[data-mode="active"] .detail-stats{display:none}
}

/* スクロールバー */
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:rgba(0,0,0,0.2)}
::-webkit-scrollbar-thumb{background:var(--border-h);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--accent)}
</style>
</head>
<body data-mode="setup">
<header>
  <div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap">
    <div class="brand">BACOPYMASTER</div>
    <span id="globalStatus" class="pill">読込中</span>
  </div>
  <div class="status">
    <button id="modeToggle" class="mode-toggle" title="待機/実戦 切替">実戦モード</button>
    <span id="updatedPill" class="pill" title="スナップショット更新">更新:-</span>
    <form method="POST" action="/master/logout" style="margin:0">
      <button type="submit" class="logout">ログアウト</button>
    </form>
  </div>
</header>

<!-- STOP 作動中バナー -->
<div class="stop-banner">⚠ 緊急停止中 — 右下の STOP をもう一度押して解除 ⚠</div>

<!-- ======= 集計ステータスバー (案4) ======= -->
<div class="stats-bar">
  <div class="stat-tile accent"><div class="sl">稼働中 GUI</div><div class="sv" id="sbGuis">-</div></div>
  <div class="stat-tile"><div class="sl">本日 PnL 合計</div><div class="sv" id="sbPnl">-</div></div>
  <div class="stat-tile"><div class="sl">AI 学習</div><div class="sv" id="sbLearn">- / 5000</div></div>
  <div class="stat-tile"><div class="sl">勝率</div><div class="sv" id="sbWin">-</div></div>
  <div class="stat-tile"><div class="sl">最終操作</div><div class="sv" id="sbLast">-</div></div>
  <div class="stat-tile"><div class="sl">選択テーブル</div><div class="sv" id="sbTable">未選択</div></div>
</div>

<div class="dashboard">
  <main class="main-col">
    <!-- ======= テーブル一覧 ======= -->
    <section class="tables-sec">
      <h2>テーブル一覧 &nbsp;<span class="hctl" id="tableCountLabel">0 卓</span><span class="hctl">&middot; <span id="stakeLobbyUpdated">取得待ち</span></span></h2>
      <div class="filters">
        <select id="providerSel">
          <option value="pragmatic">Pragmatic Play</option>
          <option value="evolution">Evolution</option>
        </select>
        <input id="searchBox" placeholder="テーブル検索 (例: speed, japanese)"/>
        <label style="display:flex;gap:6px;align-items:center;color:var(--accent);font-family:var(--font-mono);font-size:11px;white-space:nowrap">
          <input id="autoSwitchToggle" type="checkbox" style="width:auto" checked/>
          クリック即テーブル移動
        </label>
      </div>
      <div id="tableList" style="min-height:80px">
        <div class="value small" style="color:var(--text-muted);padding:20px">テーブル読込中...</div>
      </div>
    </section>

    <!-- ======= 折りたたみ: 受け子 GUI 接続状態 ======= -->
    <details open>
      <summary>受け子 GUI 接続状態 — Bafather 承認ユーザー</summary>
      <div class="content"><div id="pilotList"></div></div>
    </details>

    <!-- ======= 折りたたみ: AI 学習進捗 ======= -->
    <details>
      <summary>AI 学習データ進捗 — 目標 5000 BET</summary>
      <div class="content">
        <div class="glass-card">
          <div class="progress-wrap">
            <div>
              <div style="display:flex;justify-content:space-between;font-family:var(--font-mono);font-size:12px;color:var(--text-muted);margin-bottom:6px">
                <span>AI 学習サンプル数</span>
                <span id="progressText">0 / 5000 (0.0%)</span>
              </div>
              <div class="progressbar"><div class="fill" id="progressFill" style="width:0%"></div></div>
            </div>
            <div><div class="label">結果未記録</div><div id="missingCount" class="value small">-</div></div>
            <div><div class="label">直近24h</div><div id="last24h" class="value small accent">-</div></div>
          </div>
          <div class="recent-bets" id="recentBets"></div>
        </div>
      </div>
    </details>

    <!-- ======= 折りたたみ: 受け子 GUI 詳細 ======= -->
    <details>
      <summary>受け子 GUI 詳細 — 残高 / SEQ / PnL / OS</summary>
      <div class="content"><div id="execList"></div></div>
    </details>

    <!-- ======= 折りたたみ: シグナル送信履歴 ======= -->
    <details>
      <summary>シグナル送信履歴</summary>
      <div class="content">
        <div class="history-wrap">
          <div class="glass-card"><div class="label">送信中 / 処理中</div><pre class="histlog" id="histPending">-</pre></div>
          <div class="glass-card"><div class="label">完了 / エラー (最新)</div><pre class="histlog" id="histDone">-</pre></div>
        </div>
      </div>
    </details>
  </main>

  <!-- ======= サイドカラム (案7 詳細 + 巨大操作ボタン) ======= -->
  <aside class="side-col">
    <div class="glass-card detail-panel empty" id="detailPanel">
      <div class="label">選択中のテーブル</div>
      <div id="detailTableName" class="detail-table-name">未選択</div>
      <div class="detail-roadmap" id="detailRoadmap"><span style="color:var(--text-muted);font-size:14px">テーブル未選択</span></div>
      <div class="detail-stats">
        <div class="ds"><div class="sl">Player</div><div class="sv" id="detailP">0</div></div>
        <div class="ds"><div class="sl">Banker</div><div class="sv" id="detailB">0</div></div>
        <div class="ds tie"><div class="sl">Tie</div><div class="sv" id="detailT">0</div></div>
      </div>
      <div class="detail-streak" id="detailStreak">-</div>
      <button id="favBtn" class="favorite-btn" title="お気に入りに登録 (グリッド上段に固定)">★ お気に入り</button>
    </div>

    <div class="glass-card action-panel">
      <div class="broadcast-info">
        <div>
          <div class="label" style="font-size:9px">配信先 GUI</div>
          <div id="broadcastInfo" class="broadcast-count">0 台</div>
        </div>
        <div id="switchStatus" class="value small" style="text-align:right;color:var(--text-muted);max-width:60%"></div>
      </div>
      <div id="broadcastList" class="value small" style="color:var(--text-muted);font-family:var(--font-mono)">-</div>
      <button id="btnLook" class="big-btn look" title="全GUIに様子見シグナル">LOOK<span class="sub">様子見</span></button>
      <button id="btnP" class="big-btn player" title="全GUIにPLAYER BET">PLAYER<span class="sub">勝負</span></button>
      <button id="btnB" class="big-btn banker hidden-by-default">BANKER<span class="sub">勝負</span></button>
      <button id="btnT" class="big-btn tie hidden-by-default">TIE<span class="sub">勝負</span></button>
      <div id="lastActionBox" class="act-status">待機中</div>
    </div>
  </aside>
</div>

<!-- ======= 緊急 STOP (案6) ======= -->
<button class="emergency-stop" id="emergencyStop" title="全操作を一時無効化 (ローカル)">STOP</button>

<!-- toast -->
<div id="toast" class="toast"></div>

<!-- 非表示: JS 互換用 -->
<div style="display:none">
  <input id="noteBox" value=""/>
  <input id="amountBox" type="hidden" value="0"/>
  <select id="execSel"><option value="">全待機GUI (デフォルト)</option></select>
  <button id="btnSwitch">SWITCH</button>
  <div id="selectedMeta"></div>
  <div id="sendMsg"></div>
  <div id="dbCounts"></div>
  <div id="winratePill"></div>
  <div id="snapUpdatedAt"></div>
  <div id="pilotHint"></div>
</div>

<script>
const csrf = document.querySelector('meta[name="csrf"]').content;

const LS = {
  provider:'bacopy_master_provider', exec:'bacopy_master_exec', tableId:'bacopy_master_table_id',
  tableName:'bacopy_master_table_name', note:'bacopy_master_note',
  search:'bacopy_master_search', autoSwitch:'bacopy_master_auto_switch',
  favorites:'bacopy_master_favorites', mode:'bacopy_master_mode',
  stopped:'bacopy_master_stopped',
};
let selected = { provider:'pragmatic', table_id:'', table_name:'' };
let favorites = new Set();
let prevHands = {};  // tid -> hands (for flash detection)
let lastDecisionWatch = null;
let switchWatch = null;

function loadFavs(){
  try{ favorites = new Set(JSON.parse(localStorage.getItem(LS.favorites)||'[]')); }catch(e){ favorites = new Set(); }
}
function saveFavs(){ try{ localStorage.setItem(LS.favorites, JSON.stringify([...favorites])); }catch(e){} }

function persistState(){
  try{ localStorage.setItem(LS.provider, document.getElementById('providerSel').value||''); }catch(e){}
  try{ localStorage.setItem(LS.exec, document.getElementById('execSel').value||''); }catch(e){}
  try{ localStorage.setItem(LS.tableId, selected.table_id||''); }catch(e){}
  try{ localStorage.setItem(LS.tableName, selected.table_name||''); }catch(e){}
  try{ localStorage.setItem(LS.note, document.getElementById('noteBox').value||''); }catch(e){}
  try{ localStorage.setItem(LS.autoSwitch, document.getElementById('autoSwitchToggle').checked?'1':'0'); }catch(e){}
}
function loadState(){
  try{ const v=localStorage.getItem(LS.provider); if(v) document.getElementById('providerSel').value=v; }catch(e){}
  try{ const v=localStorage.getItem(LS.note); if(v!==null) document.getElementById('noteBox').value=v; }catch(e){}
  try{ const v=localStorage.getItem(LS.autoSwitch); if(v==='0') document.getElementById('autoSwitchToggle').checked=false; }catch(e){}
  try{ localStorage.removeItem(LS.search); }catch(e){}  // 検索は毎回クリア
  try{
    const tid=localStorage.getItem(LS.tableId)||''; const tn=localStorage.getItem(LS.tableName)||'';
    if(tid){ selected.table_id=tid; selected.table_name=tn; }
  }catch(e){}
  try{
    const m = localStorage.getItem(LS.mode);
    if(m === 'active') document.body.setAttribute('data-mode','active');
  }catch(e){}
  try{
    if(localStorage.getItem(LS.stopped)==='1') document.body.classList.add('stopped');
  }catch(e){}
}
loadFavs();
loadState();

function fmt(o){ try{return JSON.stringify(o)}catch(e){return String(o)} }
function escHtml(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

// --- Pragmatic Play テーブル名 英→日 変換 (Stake ja ロケール表記に合わせる) ---
// dga WS は英語のみ返すので、表示レイヤで翻訳する.
// executor 側のテーブル判定は internal な operator_table_id を使うので、
// この翻訳は UI 表示専用で bet payload には影響しない。
const _TABLE_OVERRIDES = {
  // コード的な名前は翻訳しない
  'BACCARAT_MULTIPLAY': 'BACCARAT_MULTIPLAY',
  'STAKE SPEED BACCARAT': 'STAKE スピードバカラ',
  'Mega Sic Bac': 'Mega Sic Bac',
  'MEGA BACCARAT': 'メガバカラ',
};
function toJaTableName(en){
  if(en == null) return '';
  let n = String(en).trim();
  if(!n) return n;
  if(_TABLE_OVERRIDES[n]) return _TABLE_OVERRIDES[n];
  // 全大文字 + underscore = 内部コード (翻訳対象外)
  if(n === n.toUpperCase() && n.includes('_')) return n;
  // 連続パターンから先に置換 (長いマッチ優先)
  n = n.replace(/Priv[e\u00e9]\s*Lounge\s*Baccarat\s*Squeeze/gi, 'プライベラウンジ・スクイーズバカラ');
  n = n.replace(/Priv[e\u00e9]\s*Lounge\s*Baccarat/gi, 'プライベラウンジバカラ');
  n = n.replace(/Korean\s+Priv[e\u00e9]\s*Lounge\s*Baccarat/gi, '韓国プライベラウンジバカラ');
  n = n.replace(/Korean\s+Turbo\s+Baccarat/gi, '韓国ターボバカラ');
  n = n.replace(/Korean\s+Speed\s+Baccarat/gi, '韓国スピードバカラ');
  n = n.replace(/Korean\s+Baccarat/gi, '韓国バカラ');
  n = n.replace(/Japanese\s+Speed\s+Baccarat/gi, '日本語スピードバカラ');
  n = n.replace(/Japanese\s+Baccarat/gi, '日本語バカラ');
  n = n.replace(/Chinese\s+Speed\s+Baccarat/gi, '中国スピードバカラ');
  n = n.replace(/Chinese\s+Baccarat/gi, '中国バカラ');
  n = n.replace(/Thai\s+Speed\s+Baccarat/gi, 'タイスピードバカラ');
  n = n.replace(/Thai\s+Baccarat/gi, 'タイバカラ');
  n = n.replace(/Vietnamese\s+Speed\s+Baccarat/gi, 'ベトナムスピードバカラ');
  n = n.replace(/Vietnamese\s+Baccarat/gi, 'ベトナムバカラ');
  n = n.replace(/Indonesian\s+Speed\s+Baccarat/gi, 'インドネシアスピードバカラ');
  n = n.replace(/Indonesian\s+Baccarat/gi, 'インドネシアバカラ');
  n = n.replace(/Fortune\s*6\s+Baccarat/gi, 'フォーチュン6バカラ');
  n = n.replace(/Super\s*8\s+Baccarat/gi, 'スーパー8バカラ');
  n = n.replace(/Speed\s+Baccarat/gi, 'スピードバカラ');
  n = n.replace(/Turbo\s+Baccarat/gi, 'ターボバカラ');
  n = n.replace(/Squeeze\s+Baccarat/gi, 'スクイーズバカラ');
  n = n.replace(/Baccarat\s+Squeeze/gi, 'バカラスクイーズ');
  n = n.replace(/Baccarat\s+Lobby/gi, 'バカラロビー');
  n = n.replace(/Mega\s+Baccarat/gi, 'メガバカラ');
  n = n.replace(/\bBaccarat\b/gi, 'バカラ');
  // 余分なスペースを整理
  return n.replace(/\s+/g, ' ').replace(/\s*バカラ\s*/g, 'バカラ').trim();
}
function ageSecFromIso(iso){ if(!iso) return 99999; try{ return Math.max(0,(Date.now()-Date.parse(iso))/1000); }catch(e){ return 99999; } }
function fmtAge(sec){ if(sec==null||!isFinite(sec)) return '-'; if(sec<60) return Math.floor(sec)+'s'; if(sec<3600) return Math.floor(sec/60)+'m'; return Math.floor(sec/3600)+'h'; }
function decisionId(){ const a=crypto.getRandomValues(new Uint8Array(8)); return 'dec_'+Array.from(a).map(x=>x.toString(16).padStart(2,'0')).join(''); }
function showToast(msg, kind){
  const el=document.getElementById('toast'); el.textContent=msg;
  el.className='toast show '+(kind||'ok');
  clearTimeout(window.__toastT);
  window.__toastT=setTimeout(()=>{ el.className='toast '+(kind||'ok'); }, 4000);
}
async function apiGet(path){
  try{ const r=await fetch(path,{credentials:'same-origin'}); const t=await r.text();
    try{return JSON.parse(t);}catch(e){return{ok:false,error:'non_json',raw:t,status:r.status};}
  }catch(e){ return{ok:false,error:'network',detail:String(e)}; }
}
async function apiPost(path, body){
  try{ const r=await fetch(path,{method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}, body:JSON.stringify(body||{})});
    const t=await r.text();
    try{return JSON.parse(t);}catch(e){return{ok:false,error:'non_json',raw:t,status:r.status};}
  }catch(e){ return{ok:false,error:'network',detail:String(e)}; }
}

let _state = {
  snapshots: {}, executors: [], approvedUsers: [], approvedErr: '',
  decisions: { pending:[], processing:[], done:[], error:[] }, stats: null,
};

async function refreshOnce(){
  const provider=document.getElementById('providerSel').value;
  selected.provider=provider;
  const [st, snaps, execs, pend, proc, done, err, appr] = await Promise.all([
    apiGet('/api/status'),
    apiGet('/api/snapshots'),
    apiGet('/api/executors'),
    apiGet('/api/decisions?status=pending&limit=100'),
    apiGet('/api/decisions?status=processing&limit=100'),
    apiGet('/api/decisions?status=done&limit=2000'),
    apiGet('/api/decisions?status=error&limit=500'),
    apiGet('/api/approved-users'),
  ]);
  _state.stats = st;
  _state.snapshots = snaps;
  _state.executors = (execs && execs.executors)||[];
  _state.decisions.pending = (pend && pend.decisions)||[];
  _state.decisions.processing = (proc && proc.decisions)||[];
  _state.decisions.done = (done && done.decisions)||[];
  _state.decisions.error = (err && err.decisions)||[];
  _state.approvedUsers = (appr && appr.users)||[];
  _state.approvedErr = (appr && !appr.ok) ? (appr.error||'approved-users fetch failed') : '';

  renderHeader();
  renderStatsBar();
  renderPilots();
  renderExecutors();
  renderExecutorSelect();
  renderLearning();
  renderTables();
  renderHistory();
  renderDetailPanel();
  updateButtonsGating();
  updateButtonVisibility();
  updateSwitchWatch();
  updateActionWatch();
}

function renderHeader(){
  const st=_state.stats||{};
  const ok = !!(st && st.ok);
  const pill=document.getElementById('globalStatus');
  pill.textContent = ok?'接続 OK':'接続 ERROR';
  pill.className = 'pill '+(ok?'ok':'err');
  const up = st.snapshots_updated_at||'-';
  const hms = (up.split('T')[1]||up).replace('Z','').split('.')[0];
  document.getElementById('updatedPill').textContent = '更新:'+hms;
}

function renderStatsBar(){
  // 稼働中 GUI = online
  const standby = getStandbyExecutors();
  const total = (_state.executors||[]).length;
  document.getElementById('sbGuis').textContent = `${standby.length} / ${total}`;
  // 本日 PnL = executor.daily_pnl の合計
  let pnl = 0, hasPnl = false;
  for(const e of _state.executors){ if(e.daily_pnl!=null){ pnl += Number(e.daily_pnl)||0; hasPnl = true; } }
  const pnlEl = document.getElementById('sbPnl');
  const pnlTile = pnlEl.closest('.stat-tile');
  if(hasPnl){
    pnlEl.textContent = (pnl>=0?'+':'')+'$'+pnl.toFixed(2);
    pnlTile.className = 'stat-tile '+(pnl>0?'good':(pnl<0?'bad':''));
  } else {
    pnlEl.textContent = '-';
    pnlTile.className = 'stat-tile';
  }
  // 学習進捗
  const bets = _state.decisions.done.concat(_state.decisions.error).filter(x=>String((x.friend_action||{}).action||'').toUpperCase()==='BET');
  document.getElementById('sbLearn').textContent = `${bets.length} / 5000`;
  // 勝率
  const wr = computeWinrate(_state.decisions.done);
  document.getElementById('sbWin').textContent = wr.bet>0 ? `${wr.wr}% (${wr.win}/${wr.bet})` : '-';
  // 最終操作
  const all = [..._state.decisions.done,..._state.decisions.error,..._state.decisions.processing,..._state.decisions.pending];
  all.sort((a,b)=> String(b.received_at||'').localeCompare(String(a.received_at||'')));
  const last = all[0];
  document.getElementById('sbLast').textContent = last ? `${(last.friend_action||{}).action||''} ${fmtAge(ageSecFromIso(last.received_at))}前` : '-';
  // 選択テーブル
  const sbT = document.getElementById('sbTable');
  sbT.textContent = selected.table_name ? toJaTableName(selected.table_name) : '未選択';
  sbT.title = selected.table_name || '';
}

// ---------- 受け子 GUI 接続パネル ----------
function renderPilots(){
  const wrap=document.getElementById('pilotList');
  wrap.innerHTML='';
  const execsByEmail = {};
  for(const e of _state.executors){
    const em=String((e.user_email||'')).toLowerCase();
    if(!em) continue;
    if(!execsByEmail[em]) execsByEmail[em]=[];
    execsByEmail[em].push(e);
  }
  const approvedList = _state.approvedUsers || [];
  const seen = new Set();
  const all = [];
  for(const u of approvedList){
    const em=String(u.email||'').toLowerCase();
    seen.add(em);
    all.push({ email: u.email, approved: true, info: u, execs: execsByEmail[em]||[] });
  }
  for(const em of Object.keys(execsByEmail)){
    if(seen.has(em)) continue;
    all.push({ email: em, approved: false, info: null, execs: execsByEmail[em] });
  }
  if(_state.approvedErr){
    const warn=document.createElement('div');
    warn.className='glass-card';
    warn.innerHTML='<div class="label">承認ユーザー取得失敗</div><div class="value small lose">'+escHtml(_state.approvedErr)+'</div>';
    wrap.appendChild(warn);
  }
  if(all.length===0){
    const empty=document.createElement('div');
    empty.className='glass-card';
    empty.innerHTML='<div class="value small" style="color:var(--text-muted)">受け子 GUI の接続がありません。GUI を起動すると表示されます。</div>';
    wrap.appendChild(empty);
    return;
  }
  for(const u of all){
    const card=document.createElement('div'); card.className='glass-card pilot-card';
    const exec = u.execs[0]||null;
    let pillHtml='', execLine='';
    if(!exec){ pillHtml='<span class="pill offline">オフライン</span>'; }
    else {
      const ageSec = ageSecFromIso(exec.updated_at);
      const online = ageSec < 60;
      const busy = Array.isArray(_state.decisions.processing) && _state.decisions.processing.some(d=>(d.target_executor_id||'')===exec.executor_id);
      if(!online) pillHtml='<span class="pill offline">オフライン</span>';
      else if(exec.recovering) pillHtml='<span class="pill warn">復旧中</span>';
      else if(busy) pillHtml='<span class="pill active">処理中</span>';
      else if(exec.bettable && exec.table_name) pillHtml='<span class="pill ok">待機中</span>';
      else pillHtml='<span class="pill warn">準備未完</span>';
      if(exec.recovering && exec.recovering_reason) pillHtml += ' <span class="pill warn">'+escHtml(exec.recovering_reason.slice(0,40))+'</span>';
      if(exec.inactivity_modal_unresolved) pillHtml += ' <span class="pill err">無操作モーダル</span>';
      if(!exec.bettable && !exec.recovering) pillHtml += ' <span class="pill err">BET不可</span>';
      if(exec.session_elsewhere_unresolved) pillHtml += ' <span class="pill err">セッション奪取</span>';
      execLine = `GUI: ${escHtml(exec.label||exec.executor_id)} &middot; OS: ${escHtml(exec.os||'-')} &middot; 最終通信: ${fmtAge(ageSec)}前`;
    }
    if(!exec && u.approved) card.classList.add('offline');
    let licensePill='';
    if(u.info){
      const s = u.info.status;
      let label=s, cls='pill';
      if(s==='admin'){ cls='pill ok'; label='管理者'; }
      else if(s==='approved'){ cls='pill ok'; label='承認済'; }
      else if(s==='suspended'){ cls='pill err'; label='停止中'; }
      else if(s==='expired'){ cls='pill err'; label='期限切れ'; }
      else if(s==='empty_balance'){ cls='pill err'; label='残高なし'; }
      else if(s==='not_approved'){ cls='pill warn'; label='未承認'; }
      else cls='pill warn';
      licensePill=`<span class="${cls}">${escHtml(label||'?')}</span>`;
    } else { licensePill=`<span class="pill warn">未承認</span>`; }
    card.innerHTML = `
      <div class="pilot-head">
        <div class="pilot-email">${escHtml(u.email)}</div>
        <div>${licensePill}</div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">${pillHtml}</div>
      <div class="pilot-meta">${execLine||'(GUI 未接続)'}</div>`;
    wrap.appendChild(card);
  }
}

// ---------- 受け子 GUI 詳細カード ----------
function renderExecutors(){
  const wrap=document.getElementById('execList');
  wrap.innerHTML='';
  if(_state.executors.length===0){
    wrap.innerHTML='<div class="glass-card value small" style="color:var(--text-muted)">接続中の受け子 GUI はありません。</div>';
    return;
  }
  const selExecId = document.getElementById('execSel').value||'';
  for(const e of _state.executors){
    const ageSec = ageSecFromIso(e.updated_at);
    const online = ageSec<60;
    const c=document.createElement('div'); c.className='glass-card exec-card';
    if(selExecId && selExecId===e.executor_id) c.classList.add('active');
    const ws=e.ws||{}, caps=e.caps||{}, seq=e.seq||{};
    const wsSil = (ws.silence_sec!=null) ? Number(ws.silence_sec).toFixed(0)+'s' : '-';
    const recExh = !!ws.recover_exhausted;
    const bal = (e.balance==null) ? '-' : ('$'+Number(e.balance).toFixed(2));
    const dpnlRaw = (e.daily_pnl!=null) ? Number(e.daily_pnl) : null;
    const dpnl = dpnlRaw==null ? '-' : ((dpnlRaw>=0?'+':'')+'$'+dpnlRaw.toFixed(2));
    const dpnlCls = dpnlRaw==null ? '' : (dpnlRaw>0?'win':(dpnlRaw<0?'lose':''));
    const seqStr = (seq && seq.bet_amount!=null) ? `単位=${escHtml(seq.unit||1)} 額=${escHtml(seq.bet_amount)}` : '-';
    const capsStr = [caps.allow_banker?'B':'-', caps.allow_tie?'T':'-', caps.allow_switch_table?'SW':'-'].join('/');
    let pills='';
    pills += online ? '<span class="pill ok">オンライン</span>' : '<span class="pill offline">オフライン</span>';
    if(e.recovering) pills += ' <span class="pill warn" style="animation:pulseWarn 1s ease-in-out infinite">復旧中</span>';
    pills += e.bettable ? ' <span class="pill ok">BET可</span>' : ' <span class="pill err">BET不可</span>';
    if(recExh) pills += ' <span class="pill err">復旧失敗</span>';
    if(e.inactivity_modal_unresolved) pills += ' <span class="pill err">無操作モーダル検知</span>';
    if((e.inactivity_dismissed_count||0) > 0) pills += ` <span class="pill">モーダル自動解除×${e.inactivity_dismissed_count}</span>`;
    if(e.session_elsewhere_unresolved) pills += ' <span class="pill err">セッション奪取</span>';
    const errHtml = e.error ? `<div class="err">${escHtml(e.error)}</div>` : '';
    c.innerHTML = `
      <div class="top-row">
        <div>
          <div class="value big">${escHtml(e.label||e.executor_id)}</div>
          <div class="value small" style="color:var(--text-muted)">${escHtml(e.user_email||'(メール未設定)')} &middot; ${escHtml(e.os||'-')}</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;text-align:right">
          <div>${pills}</div>
          <div class="value small" style="color:var(--text-muted)">${fmtAge(ageSec)}前</div>
        </div>
      </div>
      <div class="value small" style="margin-top:6px;color:var(--text-muted)">卓: <span class="accent">${escHtml(e.table_name||e.table_id||'-')}</span></div>
      <div class="stats">
        <div class="stat"><div class="label">残高</div><div class="value accent">${escHtml(bal)}</div></div>
        <div class="stat"><div class="label">本日損益</div><div class="value ${dpnlCls}">${escHtml(dpnl)}</div></div>
        <div class="stat"><div class="label">次SEQ</div><div class="value">${seqStr}</div></div>
        <div class="stat"><div class="label">WS静寂</div><div class="value">${escHtml(wsSil)}</div></div>
        <div class="stat"><div class="label">権限</div><div class="value small">${escHtml(capsStr)}</div></div>
        <div class="stat"><div class="label">状態</div><div class="value small">${escHtml(e.status||'-')}</div></div>
      </div>
      ${errHtml}`;
    wrap.appendChild(c);
  }
}

function renderExecutorSelect(){
  const sel=document.getElementById('execSel');
  let cur=sel.value;
  if(!cur){ try{ cur=localStorage.getItem(LS.exec)||''; }catch(e){} }
  sel.innerHTML='<option value="">全待機GUI (デフォルト)</option>';
  for(const e of _state.executors){
    const opt=document.createElement('option');
    opt.value=e.executor_id;
    const em = e.user_email ? ' ['+e.user_email+']' : '';
    opt.textContent=(e.label||e.executor_id)+em;
    sel.appendChild(opt);
  }
  sel.value=cur;
}

// ---------- 学習進捗 ----------
function renderLearning(){
  const done = _state.decisions.done||[];
  const err = _state.decisions.error||[];
  let betCount=0, missing=0;
  const recent=[];
  const cutoff24 = Date.now() - 24*3600*1000;
  let last24 = 0;
  const all = [...done,...err].slice().sort((a,b)=> String(b.received_at||'').localeCompare(String(a.received_at||'')));
  for(const d of all){
    const fa = d.friend_action||{};
    if(String(fa.action||'').toUpperCase()!=='BET') continue;
    betCount++;
    const rcv = Date.parse(d.received_at||'') || 0;
    if(rcv >= cutoff24) last24++;
    const res = d.result||{};
    const hasConfirmed = (res.outcome||res.bet_confirm) ? true : false;
    if(!hasConfirmed && d.status==='done') missing++;
    if(recent.length < 40){ recent.push({d, fa, res}); }
  }
  const target=5000;
  const pct = Math.min(100, betCount/target*100);
  document.getElementById('progressText').textContent = `${betCount} / ${target} (${pct.toFixed(1)}%)`;
  document.getElementById('progressFill').style.width = pct.toFixed(1)+'%';
  const mEl = document.getElementById('missingCount');
  mEl.textContent = String(missing);
  mEl.className = 'value small '+(missing>0?'lose':'win');
  document.getElementById('last24h').textContent = String(last24);
  const recWrap = document.getElementById('recentBets');
  recWrap.innerHTML='';
  for(const r of recent){
    const row=document.createElement('div');
    const side=String(r.fa.side||'').toUpperCase();
    const out=String((r.res.outcome)||'').toUpperCase();
    let cls='';
    if(out==='TIE') cls='tie';
    else if(side && out && out===side) cls='win';
    else if(side && out) cls='lose';
    row.className='bet-row '+cls;
    const tname=escHtml(toJaTableName(r.d.table_name||r.d.table_id||'-'));
    const deltaRaw = r.res.stake_delta;
    let delta='';
    if(deltaRaw!=null){ const n=Number(deltaRaw); delta = (n>=0?'+':'') + '$'+n.toFixed(2); }
    else if (r.d.status==='error' && r.res.error) delta = r.res.error.slice(0,24);
    const timeStr = (r.d.received_at||'').split('T')[1]?.slice(0,8)||'-';
    row.innerHTML = `<span>${escHtml(timeStr)}</span><span class="bside">${escHtml(side)}</span><span>${escHtml(out||'-')}</span><span style="color:var(--text-muted)">${tname}</span><span>${escHtml(delta)}</span>`;
    recWrap.appendChild(row);
  }
}

// ---------- テーブル grid ----------
function renderTables(){
  const provider=document.getElementById('providerSel').value;
  const search=(document.getElementById('searchBox').value||'').toLowerCase().trim();
  const list = (_state.snapshots && _state.snapshots.snapshots && _state.snapshots.snapshots[provider]) || {};
  const wrap=document.getElementById('tableList');
  wrap.innerHTML='';
  // Pragmatic lobby の別カテゴリで到達不可のテーブルは除外 (友人の誤選択防止).
  // 根拠: scroll では default カテゴリしか走査できず, Fortune/MEGA/Super 8/Squeeze は別タブにあり到達不能.
  const _UNREACHABLE_PATTERNS = [
    /fortune\s*6/i,
    /^mega\s*baccarat$/i,
    /super\s*8/i,
    /^squeeze\s*baccarat$/i,
    /mega\s*sic/i,
  ];
  function _isUnreachable(name){
    const n = String(name||'').trim();
    if(!n) return false;
    return _UNREACHABLE_PATTERNS.some(p => p.test(n));
  }
  const items = Object.entries(list)
    .filter(([tid,s])=>{
      if(!s || typeof s!=='object') return false;
      if(provider==='pragmatic'){
        const tt = String(s.table_type||'').toUpperCase();
        if(tt && tt!=='BACCARAT') return false;
        if(!s.captured_at) return false;
        if(ageSecFromIso(s.captured_at) > 300) return false;
        if(_isUnreachable(s.table_name)) return false;
      }
      return true;
    })
    .map(([tid,s])=>({tid, stakeName: String((s||{}).table_name||''), s:s||{}}));
  if(provider==='pragmatic' && items.length===0){
    const msg=document.createElement('div');
    msg.style.cssText='padding:20px;color:var(--text-muted);grid-column:1/-1;text-align:center;border:1px dashed var(--border);border-radius:10px';
    msg.innerHTML='コレクタからの snapshot 待機中...';
    wrap.appendChild(msg);
    document.getElementById('tableCountLabel').textContent='0 卓';
    return;
  }
  // ソート: お気に入り→名前
  items.sort((a,b)=>{
    const af = favorites.has(a.tid)||favorites.has(a.stakeName);
    const bf = favorites.has(b.tid)||favorites.has(b.stakeName);
    if(af !== bf) return af ? -1 : 1;
    return String(a.stakeName||'').localeCompare(String(b.stakeName||''));
  });
  const stakeUpdated = (_state.snapshots && _state.snapshots.updated_at) || null;
  let shown=0;
  for(const it of items){
    const name = String(it.stakeName||it.s.table_name||'');
    if(search && !name.toLowerCase().includes(search)) continue;
    shown++;
    const btn=document.createElement('button');
    btn.className='table-cell';
    const isFav = favorites.has(it.tid)||favorites.has(name);
    if(isFav) btn.classList.add('favorite');
    if(selected.table_id===String(it.tid)) btn.classList.add('selected');
    // flash on new hand
    const h = it.s.hands;
    if(prevHands[it.tid]!=null && h!=null && h > prevHands[it.tid]){
      btn.classList.add('flash-new');
    }
    prevHands[it.tid] = h;
    // roadmap
    let roadHtml='';
    const last = it.s.last_10 || it.s.last_results || it.s.recent_results || '';
    let lastStr = Array.isArray(last) ? last.join('') : String(last||'');
    lastStr = lastStr.slice(-14);
    for(const ch of lastStr){
      const u=ch.toUpperCase();
      if(u==='B') roadHtml+=`<span class="dot-B">${escHtml(ch)}</span>`;
      else if(u==='P') roadHtml+=`<span class="dot-P">${escHtml(ch)}</span>`;
      else if(u==='T') roadHtml+=`<span class="dot-T">${escHtml(ch)}</span>`;
      else roadHtml+=escHtml(ch);
    }
    const players = (it.s.players!=null) ? it.s.players : '-';
    const hands = (it.s.hands!=null) ? it.s.hands : '-';
    const displayName = toJaTableName(name || it.tid);
    btn.innerHTML = `
      <div class="tname" title="${escHtml(name||it.tid)}">${escHtml(displayName)}</div>
      <div class="roadmap">${roadHtml||'<span style="color:var(--text-muted)">罫線データなし</span>'}</div>
      <div class="tmeta"><span>${escHtml(players)}人</span><span>${escHtml(hands)}ハンド</span></div>`;
    btn.onclick = () => {
      const wasSame = selected.table_id === String(it.tid);
      selected = { provider, table_id:String(it.tid), table_name:name };
      // UI には日本語表示を優先.
      const selT = document.getElementById('selectedTable');
      if(selT){ selT.textContent = toJaTableName(name||it.tid); selT.title = name||''; }
      document.getElementById('selectedMeta').textContent='provider='+provider+' table_id='+it.tid;
      persistState();
      renderTables();
      renderDetailPanel();
      renderStatsBar();
      const clickToSwitch = document.getElementById('autoSwitchToggle').checked;
      if(clickToSwitch && !wasSame) sendDecision('SWITCH_TABLE','');
    };
    wrap.appendChild(btn);
  }
  document.getElementById('tableCountLabel').textContent = shown+'卓';
  const stakeEl = document.getElementById('stakeLobbyUpdated');
  if(stakeEl){
    if(stakeUpdated){
      const sec = ageSecFromIso(stakeUpdated);
      stakeEl.textContent = fmtAge(sec)+'前に更新';
      stakeEl.style.color = sec<5 ? 'var(--win)' : (sec<60 ? 'var(--tie)' : 'var(--text-muted)');
    } else {
      stakeEl.textContent = '取得待ち';
    }
  }
}

// ---------- 選択テーブル詳細 (案7) ----------
function renderDetailPanel(){
  const panel = document.getElementById('detailPanel');
  if(!selected.table_id){
    panel.classList.add('empty');
    document.getElementById('detailTableName').textContent = '未選択';
    document.getElementById('detailRoadmap').innerHTML = '<span style="color:var(--text-muted);font-size:14px">テーブル未選択</span>';
    document.getElementById('detailP').textContent = '0';
    document.getElementById('detailB').textContent = '0';
    document.getElementById('detailT').textContent = '0';
    document.getElementById('detailStreak').textContent = '-';
    document.getElementById('favBtn').classList.remove('on');
    return;
  }
  panel.classList.remove('empty');
  const list = (_state.snapshots && _state.snapshots.snapshots && _state.snapshots.snapshots[selected.provider]) || {};
  const s = list[selected.table_id] || {};
  const detailEn = s.table_name || selected.table_name || selected.table_id;
  const detailEl = document.getElementById('detailTableName');
  detailEl.textContent = toJaTableName(detailEn);
  detailEl.title = detailEn;  // 元の英語名を tooltip で確認可
  // 大きな罫線は直近 20 を表示
  const lastArr = s.last_results || s.last_10 || '';
  const str = (Array.isArray(lastArr) ? lastArr.join('') : String(lastArr||'')).slice(-20);
  let roadHtml = '';
  for(const ch of str){
    const u = ch.toUpperCase();
    if(u==='B') roadHtml+=`<span class="dot-B">${escHtml(ch)}</span>`;
    else if(u==='P') roadHtml+=`<span class="dot-P">${escHtml(ch)}</span>`;
    else if(u==='T') roadHtml+=`<span class="dot-T">${escHtml(ch)}</span>`;
  }
  document.getElementById('detailRoadmap').innerHTML = roadHtml||'<span style="color:var(--text-muted);font-size:14px">まだ罫線データなし</span>';
  // P/B/T カウントは シュー全体 (sequence) を数える → ハンド数と一致させる
  const fullSeq = String(s.sequence || str || '');
  let p=0, b=0, t=0;
  for(const ch of fullSeq){
    const u = ch.toUpperCase();
    if(u==='B') b++;
    else if(u==='P') p++;
    else if(u==='T') t++;
  }
  document.getElementById('detailP').textContent = p;
  document.getElementById('detailB').textContent = b;
  document.getElementById('detailT').textContent = t;
  // 連続検出
  let streak = '';
  if(str.length > 0){
    const lastCh = str[str.length-1];
    let run = 1;
    for(let i=str.length-2; i>=0; i--){ if(str[i]===lastCh) run++; else break; }
    const jaName = lastCh==='B'?'バンカー':(lastCh==='P'?'プレイヤー':(lastCh==='T'?'タイ':''));
    if(run >= 3){
      streak = `${jaName} ${run}連続`;
      document.getElementById('detailStreak').className = 'detail-streak big';
    } else {
      streak = `直近: ${jaName}`;
      document.getElementById('detailStreak').className = 'detail-streak';
    }
  }
  document.getElementById('detailStreak').textContent = streak || '-';
  // お気に入り
  const isFav = favorites.has(selected.table_id) || favorites.has(selected.table_name);
  document.getElementById('favBtn').classList.toggle('on', isFav);
  document.getElementById('favBtn').textContent = isFav ? '★ 登録済' : '☆ お気に入り';
}

// ---------- 履歴 ----------
function renderHistory(){
  const p = [..._state.decisions.pending, ..._state.decisions.processing].slice(-20);
  const d = [..._state.decisions.error, ..._state.decisions.done].slice(-30).reverse();
  const statusJa = (s)=>({pending:'待機',processing:'処理中',done:'完了',error:'エラー',acked:'受理'})[s]||s||'';
  const fmtRow = (x)=>{
    const fa = x.friend_action||{}, r = x.result||{};
    const out = r.outcome||r.error||'';
    const time = (x.received_at||'').split('T')[1]?.slice(0,8)||'';
    return `[${time}] ${statusJa(x.status)} ${toJaTableName(x.table_name||x.table_id||'')} ${fa.action||''} ${fa.side||''} -> ${out} (${(x.decision_id||'').slice(-10)})`;
  };
  document.getElementById('histPending').textContent = p.map(fmtRow).join('\n')||'(なし)';
  document.getElementById('histDone').textContent = d.map(fmtRow).join('\n')||'(なし)';
}

function computeWinrate(done){
  let bet=0, win=0, lose=0, tie=0;
  for(const x of (done||[])){
    const fa=x.friend_action||{};
    if(String(fa.action||'').toUpperCase()!=='BET') continue;
    const side=String(fa.side||'').toLowerCase();
    const out=String((x.result||{}).outcome||'').toLowerCase();
    bet++;
    if(out==='tie'&&side!=='tie'){ tie++; continue; }
    if(out===side) win++; else lose++;
  }
  const wr = bet?(win/bet*100).toFixed(2):'0.00';
  return {bet,win,lose,tie,wr};
}

function getStandbyExecutors(){
  return (_state.executors||[]).filter(e=>{
    const ageSec = ageSecFromIso(e.updated_at);
    if(ageSec >= 60) return false;
    if(e.session_elsewhere_unresolved) return false;
    if(e.inactivity_modal_unresolved) return false;
    if(e.recovering) return false;
    return !!e.bettable;
  });
}

function updateButtonsGating(){
  const provider=document.getElementById('providerSel').value;
  const selExecId=document.getElementById('execSel').value||'';
  const isPrag = provider==='pragmatic';
  const standby = getStandbyExecutors();
  const targets = selExecId ? standby.filter(e=>e.executor_id===selExecId) : standby;
  const nTargets = targets.length;
  const anyAllowB = targets.some(e=>!!(e.caps&&e.caps.allow_banker));
  const anyAllowT = targets.some(e=>!!(e.caps&&e.caps.allow_tie));
  const allowAny = nTargets > 0 && !document.body.classList.contains('stopped');
  const allowB = allowAny && (!isPrag || anyAllowB);
  const allowT = allowAny && (!isPrag || anyAllowT);
  document.getElementById('btnP').disabled = !allowAny;
  document.getElementById('btnLook').disabled = nTargets===0;
  document.getElementById('btnB').disabled = !allowB;
  document.getElementById('btnT').disabled = !allowT;
  document.getElementById('btnSwitch').disabled = !selected.table_id || !allowAny;
  const info=document.getElementById('broadcastInfo');
  const listEl=document.getElementById('broadcastList');
  if(selExecId){
    info.textContent = nTargets>0 ? '1 台 (個別)' : '0 台 (該当なし)';
    listEl.textContent = targets.map(e=>e.label||e.executor_id).join(', ')||'-';
  } else {
    info.textContent = nTargets + ' 台';
    if(nTargets===0){
      const total=(_state.executors||[]).length;
      listEl.textContent = total>0 ? '登録済み GUI なし（セッション確認）' : '受け子 GUI 未接続';
    } else {
      listEl.textContent = targets.map(e=>(e.user_email||e.label||e.executor_id)).join(', ');
    }
  }
}

function updateButtonVisibility(){
  const anyB = _state.executors.some(e=>e.caps&&e.caps.allow_banker);
  const anyT = _state.executors.some(e=>e.caps&&e.caps.allow_tie);
  document.body.classList.toggle('show-bt', !!(anyB||anyT));
  document.getElementById('btnB').style.display = anyB?'':'none';
  document.getElementById('btnT').style.display = anyT?'':'none';
}

// ---------- シグナル送信 ----------
function sendDecision(action, side){
  if(document.body.classList.contains('stopped')){ showToast('緊急停止中です','err'); return; }
  if(action==='BET' || action==='SWITCH_TABLE'){
    if(!selected.table_id){ showToast('テーブルを選択してください','err'); return; }
  }
  const provider=document.getElementById('providerSel').value;
  const target_executor_id=document.getElementById('execSel').value||'';
  const note=document.getElementById('noteBox').value||'';
  persistState();
  const did=decisionId();
  const payload = {
    decision_id:did, provider,
    table_id:selected.table_id, table_name:selected.table_name,
    target_executor_id,
    friend_action:{action, side:side||'', amount:0, note},
  };
  const actJa = {LOOK:'様子見','BET':'BET','SWITCH_TABLE':'テーブル移動'}[action]||action;
  apiPost('/api/decisions', payload).then(res=>{
    if(res && res.accepted){
      showToast('送信: '+actJa+(side?('/'+side):'')+' (ID:'+did.slice(-8)+')','ok');
      lastDecisionWatch = { id:did, action, side:side||'', startedAt:Date.now(), targetExecId:target_executor_id, targetTable:selected.table_name };
      setActionBox('処理中...','processing');
      if(action==='SWITCH_TABLE'){
        switchWatch = { id:did, targetTable:selected.table_name, startedAt:Date.now(), targetExecId:target_executor_id };
        setSwitchStatus('移動中... → '+(selected.table_name||selected.table_id), 'pending');
      }
    } else {
      showToast('送信失敗: '+fmt(res),'err');
      setActionBox('送信失敗: '+(res && (res.error||res.reason)||'?'),'err');
    }
  });
}
function setActionBox(msg, kind){
  const el=document.getElementById('lastActionBox');
  el.textContent = msg;
  el.className = 'act-status '+(kind||'');
}
function setSwitchStatus(msg, kind){
  const el=document.getElementById('switchStatus');
  el.textContent = msg||'';
  el.className = 'value small '+(kind||'');
}
function updateActionWatch(){
  if(!lastDecisionWatch) return;
  const did = lastDecisionWatch.id;
  const all = [..._state.decisions.done, ..._state.decisions.error, ..._state.decisions.processing, ..._state.decisions.pending];
  const d = all.find(x=>x.decision_id===did);
  if(!d) return;
  const status=d.status||'', res=d.result||{}, fa=d.friend_action||{};
  const actJa = {LOOK:'様子見','BET':'BET','SWITCH_TABLE':'テーブル移動'}[fa.action]||fa.action||'';
  const actLabel = actJa + (fa.side?('/'+fa.side):'');
  if(status==='processing'||status==='pending'){ setActionBox('['+actLabel+'] 処理中...','processing'); return; }
  if(status==='error'){ setActionBox('['+actLabel+'] エラー: '+(res.error||'error'),'err'); lastDecisionWatch=null; return; }
  if(status==='done'){
    let detail = res.outcome || (res.bet_confirm?'BET確定':'') || res.note || '完了';
    if(res.stake_delta!=null){ const n=Number(res.stake_delta); detail += ' ('+(n>=0?'+':'')+'$'+n.toFixed(2)+')'; }
    setActionBox('['+actLabel+'] '+detail,'ok');
    lastDecisionWatch=null;
    return;
  }
}
function updateSwitchWatch(){
  if(!switchWatch) return;
  const elapsed = (Date.now()-switchWatch.startedAt)/1000;
  if(elapsed > 90){ setSwitchStatus('移動タイムアウト','err'); switchWatch=null; return; }
  const target = (switchWatch.targetTable||'').toLowerCase();
  const ex = switchWatch.targetExecId ? _state.executors.find(x=>x.executor_id===switchWatch.targetExecId) : null;
  if(ex){
    const cur = (ex.table_name||'').toLowerCase();
    if(target && cur && (cur.includes(target) || target.includes(cur))){
      if(ex.bettable){ setSwitchStatus('✓ 到着 & BET可能: '+(ex.table_name||''),'ok'); switchWatch=null; }
      else { setSwitchStatus('到着、BET可能待ち...','pending'); }
    } else { setSwitchStatus('移動中... ('+Math.floor(elapsed)+'s)','pending'); }
  } else {
    setSwitchStatus('全GUI へ移動シグナル送信済 ('+Math.floor(elapsed)+'s)','pending');
    if(elapsed > 20) switchWatch = null;
  }
}

// ---------- 案1: モード切替 ----------
function toggleMode(){
  const cur = document.body.getAttribute('data-mode');
  const next = cur === 'active' ? 'setup' : 'active';
  document.body.setAttribute('data-mode', next);
  try{ localStorage.setItem(LS.mode, next); }catch(e){}
  document.getElementById('modeToggle').textContent = next==='active' ? '待機モードへ' : '実戦モードへ';
}
(function initModeBtn(){
  const m = document.body.getAttribute('data-mode');
  document.getElementById('modeToggle').textContent = m==='active' ? '待機モードへ' : '実戦モードへ';
})();

// ---------- 案6: 緊急 STOP ----------
function toggleStop(){
  if(document.body.classList.contains('stopped')){
    if(confirm('緊急停止を解除しますか？ (BETボタンが再び有効になります)')){
      document.body.classList.remove('stopped');
      try{ localStorage.removeItem(LS.stopped); }catch(e){}
      showToast('緊急停止 解除','ok');
    }
  } else {
    if(confirm('緊急停止: BET/SWITCH ボタンを即座に無効化します。解除するまで誤送信を防ぎます。')){
      document.body.classList.add('stopped');
      try{ localStorage.setItem(LS.stopped,'1'); }catch(e){}
      showToast('緊急停止 発動','err');
    }
  }
  updateButtonsGating();
}

// ---------- お気に入り ----------
function toggleFavorite(){
  if(!selected.table_id) return;
  const key = selected.table_id;
  if(favorites.has(key) || favorites.has(selected.table_name)){
    favorites.delete(key);
    favorites.delete(selected.table_name);
  } else {
    favorites.add(key);
  }
  saveFavs();
  renderTables();
  renderDetailPanel();
}

// ---------- bind ----------
document.getElementById('providerSel').onchange = ()=>{ persistState(); refreshOnce(); };
document.getElementById('execSel').onchange = ()=>{ persistState(); refreshOnce(); };
document.getElementById('noteBox').oninput = ()=>{ persistState(); };
document.getElementById('searchBox').oninput = ()=>{ clearTimeout(window.__t); window.__t=setTimeout(renderTables,150); };
document.getElementById('autoSwitchToggle').onchange = ()=>{ persistState(); };
document.getElementById('btnSwitch').onclick = ()=> sendDecision('SWITCH_TABLE','');
document.getElementById('btnLook').onclick = ()=> sendDecision('LOOK','');
document.getElementById('btnP').onclick = ()=> sendDecision('BET','PLAYER');
document.getElementById('btnB').onclick = ()=> sendDecision('BET','BANKER');
document.getElementById('btnT').onclick = ()=> sendDecision('BET','TIE');
document.getElementById('modeToggle').onclick = toggleMode;
document.getElementById('emergencyStop').onclick = toggleStop;
document.getElementById('favBtn').onclick = toggleFavorite;

// キーボードショートカット
document.addEventListener('keydown', (e)=>{
  if(e.target.tagName==='INPUT' || e.target.tagName==='SELECT' || e.target.tagName==='TEXTAREA') return;
  if(e.ctrlKey||e.metaKey||e.altKey) return;
  const k = e.key.toLowerCase();
  if(k==='p' && !document.getElementById('btnP').disabled){ e.preventDefault(); sendDecision('BET','PLAYER'); }
  else if(k==='l' && !document.getElementById('btnLook').disabled){ e.preventDefault(); sendDecision('LOOK',''); }
  else if(k==='b' && !document.getElementById('btnB').disabled && !document.getElementById('btnB').style.display.includes('none')){ e.preventDefault(); sendDecision('BET','BANKER'); }
  else if(k==='s'){ e.preventDefault(); toggleStop(); }
});

refreshOnce();
setInterval(refreshOnce, 1000);
</script>
</body></html>
"""
