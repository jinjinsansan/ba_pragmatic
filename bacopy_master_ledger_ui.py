"""Master UI 家計簿ページ HTML レンダラ.

bafather.uk/admin/ledger と同じ内容を、Master UI のダーク水色テーマで描画する。
データは bafather web の /api/master/ledger から LAPLACE_API_KEY で取得。

主要セクション (bafather/admin/ledger と同レイアウト):
  左カラム: 投資家情報 / 分配ルール / 口座残高
  右カラム: 投資家サマリ / 運用者損益 / 利益所在 / 1つめ口座未出金 / 経費受領
"""
from __future__ import annotations

import html
import json
import os
import urllib.error
import urllib.request


def _fetch_ledger_data() -> dict:
    """bafather Web の master ledger API を叩いて JSON を取得."""
    base = (os.getenv("BACOPY_BAFATHER_URL", "") or "https://www.bafather.uk").rstrip("/")
    api_key = (os.getenv("LAPLACE_API_KEY", "") or "").strip()
    if not api_key:
        return {"error": "LAPLACE_API_KEY not set", "summaries": [], "rules": []}
    req = urllib.request.Request(
        f"{base}/api/master/ledger",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "summaries": [], "rules": []}
    except Exception as e:
        return {"error": str(e), "summaries": [], "rules": []}


def _fmt_usd(value) -> str:
    """USD 表示。負値は (123.45) 形式、ゼロは '-'."""
    try:
        v = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return "-"
    if v == 0:
        return "-"
    abs_str = f"${abs(v):,.2f}"
    return f"({abs_str.lstrip('$')})" if v < 0 else abs_str


def _fmt_pct(value) -> str:
    """0.20 → 20.0% のような表示."""
    try:
        v = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return "-"
    return f"{v * 100:.1f}%"


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def render_master_ledger_page(csrf: str) -> str:
    data = _fetch_ledger_data()
    error = data.get("error")
    summaries = data.get("summaries") or []
    rules = data.get("rules") or []
    rules_by_inv = {r.get("investor_id"): r for r in rules if isinstance(r, dict)}

    sections_html = []
    for s in summaries:
        if not isinstance(s, dict):
            continue
        sections_html.append(_render_investor_section(s, rules_by_inv.get(s.get("investor_id"))))

    body = "\n".join(sections_html) if sections_html else _render_empty()
    err_banner = f'<div class="err-banner">⚠ データ取得失敗: {html.escape(error)}</div>' if error else ""

    return _PAGE.replace("__CSRF__", html.escape(csrf or "")) \
        .replace("__ERROR__", err_banner) \
        .replace("__BODY__", body)


def _render_empty() -> str:
    return (
        '<div class="empty-state">'
        '<p>投資家が登録されていません。</p>'
        '<p style="opacity:.6;font-size:12px">bafather.uk/admin/ledger で初期投資家を追加してください。</p>'
        '</div>'
    )


def _render_investor_section(s: dict, rule: dict | None) -> str:
    name = html.escape(str(s.get("investor_name") or "?"))
    investor_id = html.escape(str(s.get("investor_id") or ""))
    total_inv = _fmt_usd(s.get("total_investment"))

    # 投資家情報
    info = f"""
      <div class="ledger-card emerald">
        <div class="card-label">INVESTOR INFO</div>
        <div class="kv">
          <span>投資総額</span><span class="num">{_fmt_usd(s.get("total_investment"))}</span>
          <span>1つめ口座</span><span class="num">{_fmt_usd(s.get("account1_amount"))}</span>
          <span>2つめ口座</span><span class="num">{_fmt_usd(s.get("account2_amount"))}</span>
          <span>別チャージ資金</span><span class="num">{_fmt_usd(s.get("reserve_initial"))}</span>
          <span class="kv-sep">画面上初期チャージ</span><span class="num kv-sep bold">{_fmt_usd(s.get("initial_charge_display"))}</span>
        </div>
      </div>
    """

    # 分配ルール
    if rule:
        rule_total = _f(rule.get("investor_share_pct")) + _f(rule.get("j_share_pct")) + _f(rule.get("k_share_pct")) + _f(rule.get("company_share_pct"))
        rule_html = f"""
          <div class="ledger-card blue">
            <div class="card-label">DISTRIBUTION RULE (1つめ口座)</div>
            <div class="kv">
              <span>{name} 取り分</span><span class="num">{_fmt_pct(rule.get("investor_share_pct"))}</span>
              <span>J 取り分</span><span class="num">{_fmt_pct(rule.get("j_share_pct"))}</span>
              <span>K 取り分</span><span class="num">{_fmt_pct(rule.get("k_share_pct"))}</span>
              <span>会社内部保留</span><span class="num">{_fmt_pct(rule.get("company_share_pct"))}</span>
              <span class="kv-sep">合計</span><span class="num kv-sep bold">{_fmt_pct(rule_total)}</span>
            </div>
          </div>
        """
    else:
        rule_html = '<div class="ledger-card blue"><div class="card-label">DISTRIBUTION RULE</div><div class="muted">ルール未設定</div></div>'

    # 口座残高
    balances = f"""
      <div class="ledger-card slate">
        <div class="card-label">ACCOUNT BALANCES</div>
        <div class="kv">
          <span>2つめ口座 現在残高</span><span class="num">{_fmt_usd(s.get("account2_balance"))}</span>
          <span>別チャージ残高</span><span class="num">{_fmt_usd(s.get("reserve_balance"))}</span>
        </div>
      </div>
    """

    # 投資家サマリ
    investor_summary = f"""
      <div class="ledger-card emerald-strong">
        <div class="card-label">INVESTOR ({name}) — 投資家から見える数値</div>
        <div class="kv">
          <span>受け取った利益累計</span><span class="num bold accent-emerald">{_fmt_usd(s.get("investor_received_total"))}</span>
          <span>画面上のチャージ資金残高</span><span class="num bold accent-emerald">{_fmt_usd(s.get("displayed_charge_balance"))}</span>
        </div>
      </div>
    """

    # 運用者損益
    expense_from_acc2 = _f(s.get("expense_from_account2"))
    operator = f"""
      <div class="ledger-card amber">
        <div class="card-label">OPERATOR (運用者損益)</div>
        <div class="kv">
          <span>1つめ 80% 累計</span><span class="num">{_fmt_usd(s.get("account1_80pct_total"))}</span>
          <span>2つめ 利益累計</span><span class="num">{_fmt_usd(s.get("account2_total_profit"))}</span>
          <span class="kv-sep">総合計純利益</span><span class="num kv-sep bold accent-amber">{_fmt_usd(s.get("operator_net_profit"))}</span>
          <span>利益から出金</span><span class="num accent-red">({_fmt_usd(expense_from_acc2).strip("()")})</span>
          <span class="kv-sep">残利益 (未出金)</span><span class="num kv-sep bold big accent-amber">{_fmt_usd(s.get("operator_remaining_profit"))}</span>
        </div>
      </div>
    """

    # 利益所在
    rem_acc2 = _f(s.get("remaining_in_account2"))
    rem_charge = _f(s.get("remaining_charge_refund"))
    op_rem = _f(s.get("operator_remaining_profit"))
    location_total = rem_acc2 + rem_charge
    location_warn = '<div class="err-warn">⚠ 検算不一致</div>' if abs(location_total - op_rem) > 0.01 else ""

    location = f"""
      <div class="ledger-card orange">
        <div class="card-label">PROFIT LOCATION (利益の所在)</div>
        <div class="kv">
          <span>2つめ口座内残存</span><span class="num">{_fmt_usd(rem_acc2)}</span>
          <span>1つめチャージ返金分</span><span class="num">{_fmt_usd(rem_charge)}</span>
          <span class="kv-sep">所在合計 (検算)</span><span class="num kv-sep bold">{_fmt_usd(location_total)}</span>
        </div>
        {location_warn}
      </div>
    """

    # 1つめ口座 未出金取り分
    j_unpaid = _f(s.get("j_unpaid_in_account1"))
    k_unpaid = _f(s.get("k_unpaid_in_account1"))
    co_unpaid = _f(s.get("company_unpaid_in_account1"))
    unpaid_total = j_unpaid + k_unpaid + co_unpaid

    unpaid = f"""
      <div class="ledger-card cyan">
        <div class="card-label">UNPAID SHARES IN ACCOUNT1 (1つめ口座のみで計算した未出金取り分)</div>
        <div class="card-note">※ 計算式: 1つめ口座 累計取り分 − 既出金分。AI 開発費 ({_fmt_usd(s.get("ai_dev_total"))}) は運用益外例外で除外。</div>
        <div class="unpaid-row">
          <div class="unpaid-line">
            <span class="unpaid-label">J (利益の 20%)</span>
            <span class="unpaid-amount accent-cyan bold big">{_fmt_usd(j_unpaid)}</span>
          </div>
          <div class="unpaid-formula">累計取り分 {_fmt_usd(s.get("j_share_in_account1"))} − 既出金 {_fmt_usd(s.get("j_total"))}</div>
        </div>
        <div class="unpaid-row">
          <div class="unpaid-line">
            <span class="unpaid-label">K (利益の 30%)</span>
            <span class="unpaid-amount accent-cyan bold big">{_fmt_usd(k_unpaid)}</span>
          </div>
          <div class="unpaid-formula">累計取り分 {_fmt_usd(s.get("k_share_in_account1"))} − 既出金 K本人 {_fmt_usd(s.get("k_total"))} − Kの兄 {_fmt_usd(s.get("k_brother_total"))}</div>
        </div>
        <div class="unpaid-row">
          <div class="unpaid-line">
            <span class="unpaid-label">会社配当 (利益の 30%)</span>
            <span class="unpaid-amount accent-cyan bold big">{_fmt_usd(co_unpaid)}</span>
          </div>
          <div class="unpaid-formula">累計取り分 {_fmt_usd(s.get("company_share_in_account1"))} − 既出金 {_fmt_usd(s.get("company_total"))}</div>
        </div>
        <div class="unpaid-total">
          <span>未出金合計</span>
          <span class="num bold big">{_fmt_usd(unpaid_total)}</span>
        </div>
      </div>
    """

    # 経費受領
    expenses = f"""
      <div class="ledger-card purple">
        <div class="card-label">EXPENSE RECIPIENTS (経費受取累計)</div>
        <div class="kv">
          <span>J 受取累計</span><span class="num">{_fmt_usd(s.get("j_total"))}</span>
          <span>K 受取累計</span><span class="num">{_fmt_usd(s.get("k_total"))}</span>
          <span>K の兄 受取累計</span><span class="num">{_fmt_usd(s.get("k_brother_total"))}</span>
          <span>会社 (配当金) 累計</span><span class="num">{_fmt_usd(s.get("company_total"))}</span>
          <span>AI 開発費等</span><span class="num">{_fmt_usd(s.get("ai_dev_total"))}</span>
          <span class="kv-sep">出金合計</span><span class="num kv-sep bold">{_fmt_usd(s.get("expense_total"))}</span>
        </div>
      </div>
    """

    return f"""
      <section class="ledger-section">
        <div class="ledger-section-head">
          <h2>{name} さん</h2>
          <span class="hint">投資総額 {total_inv}</span>
        </div>
        <div class="ledger-grid">
          <div class="ledger-col">
            {info}
            {rule_html}
            {balances}
          </div>
          <div class="ledger-col">
            {investor_summary}
            {operator}
            {location}
            {unpaid}
            {expenses}
          </div>
        </div>
      </section>
    """


_PAGE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>FX 運用家計簿 — BACOPYMASTER</title>
<link rel="stylesheet" href="/master/theme.css"/>
<style>
:root{
  --bg:#03060c; --bg-glass:rgba(10,16,30,0.7); --border:rgba(0,229,255,0.18); --border-h:rgba(0,229,255,0.4);
  --accent:#00e5ff; --text:#e6f4ff; --text-muted:#8aa0b8; --text-dim:#506680;
  --win:#00ff88; --lose:#ff3366; --tie:#ffcc00;
  --font-hud:'Orbitron','Segoe UI',Roboto,sans-serif;
  --font-body:'Segoe UI',Roboto,sans-serif;
  --font-mono:'JetBrains Mono','Cascadia Code',Consolas,monospace;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--text);font-family:var(--font-body);min-height:100vh}
header{position:sticky;top:0;background:rgba(3,6,12,0.96);backdrop-filter:blur(10px);border-bottom:1px solid var(--border);padding:12px 24px;display:flex;justify-content:space-between;align-items:center;z-index:20;flex-wrap:wrap;gap:12px}
header .brand{font-family:var(--font-hud);font-size:18px;font-weight:900;letter-spacing:4px;color:var(--accent);text-shadow:0 0 12px rgba(0,229,255,0.6)}
header .nav{display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:12px}
header .nav a{color:var(--text-muted);text-decoration:none;padding:5px 10px;border-radius:6px}
header .nav a:hover{color:var(--text);background:rgba(0,229,255,0.08)}
header .nav a.active{color:var(--accent);background:rgba(0,229,255,0.12);border:1px solid var(--border)}

main{max-width:1280px;margin:0 auto;padding:24px}
.page-head{margin-bottom:24px}
.page-head .label{font-family:var(--font-hud);font-size:11px;letter-spacing:3px;color:var(--text-muted);margin-bottom:6px}
.page-head h1{font-family:var(--font-hud);font-size:26px;font-weight:900;margin:0 0 6px 0;color:var(--text)}
.page-head .sub{color:var(--text-muted);font-size:13px}

.err-banner{background:rgba(255,51,102,0.1);border:1px solid rgba(255,51,102,0.4);color:#ff8aa3;padding:10px 14px;border-radius:8px;margin-bottom:16px;font-size:13px}

.spec-summary{background:var(--bg-glass);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:24px;font-size:12px;color:var(--text-muted);line-height:1.7}
.spec-summary strong{color:var(--text)}
.spec-summary .warn{color:#ffd066}

.empty-state{text-align:center;padding:80px 0;color:var(--text-muted)}

.ledger-section{margin-bottom:48px}
.ledger-section-head{display:flex;align-items:baseline;gap:12px;border-bottom:1px solid var(--border);padding-bottom:8px;margin-bottom:18px}
.ledger-section-head h2{font-family:var(--font-hud);font-size:20px;font-weight:800;margin:0;color:var(--text)}
.ledger-section-head .hint{font-size:11px;color:var(--text-muted)}

.ledger-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:900px){.ledger-grid{grid-template-columns:1fr}}
.ledger-col{display:flex;flex-direction:column;gap:14px}

.ledger-card{border-radius:10px;padding:14px 16px;border:1px solid;background:rgba(10,16,30,0.5)}
.ledger-card.emerald{border-color:rgba(34,197,94,0.25);background:rgba(34,197,94,0.04)}
.ledger-card.emerald-strong{border-color:rgba(34,197,94,0.4);background:rgba(34,197,94,0.07)}
.ledger-card.blue{border-color:rgba(59,130,246,0.25);background:rgba(59,130,246,0.04)}
.ledger-card.slate{border-color:rgba(100,116,139,0.25);background:rgba(100,116,139,0.04)}
.ledger-card.amber{border-color:rgba(234,179,8,0.3);background:rgba(234,179,8,0.06)}
.ledger-card.orange{border-color:rgba(249,115,22,0.25);background:rgba(249,115,22,0.04)}
.ledger-card.cyan{border-color:rgba(6,182,212,0.35);background:rgba(6,182,212,0.05)}
.ledger-card.purple{border-color:rgba(168,85,247,0.25);background:rgba(168,85,247,0.04)}

.card-label{font-family:var(--font-hud);font-size:10px;letter-spacing:2px;font-weight:700;margin-bottom:10px}
.ledger-card.emerald .card-label, .ledger-card.emerald-strong .card-label{color:#34d399}
.ledger-card.blue .card-label{color:#60a5fa}
.ledger-card.slate .card-label{color:#94a3b8}
.ledger-card.amber .card-label{color:#fbbf24}
.ledger-card.orange .card-label{color:#fb923c}
.ledger-card.cyan .card-label{color:#22d3ee}
.ledger-card.purple .card-label{color:#c084fc}

.card-note{font-size:10px;color:var(--text-muted);line-height:1.5;margin-bottom:10px}

.kv{display:grid;grid-template-columns:1fr auto;gap:6px 12px;font-size:13px}
.kv > span{padding:2px 0}
.kv .num{font-family:var(--font-mono);text-align:right}
.kv .bold{font-weight:700}
.kv .big{font-size:16px}
.kv .kv-sep{border-top:1px solid rgba(0,229,255,0.1);padding-top:6px;margin-top:2px}
.kv .accent-emerald{color:#86efac}
.kv .accent-amber{color:#fcd34d}
.kv .accent-red{color:#fca5a5}
.kv .accent-cyan{color:#67e8f9}

.unpaid-row{margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,0.06)}
.unpaid-row:last-of-type{border-bottom:none}
.unpaid-line{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px}
.unpaid-label{font-size:13px;font-weight:600}
.unpaid-amount{font-family:var(--font-mono);font-size:18px}
.unpaid-formula{font-family:var(--font-mono);font-size:10px;color:var(--text-muted);padding-left:10px;line-height:1.5}
.unpaid-total{display:flex;justify-content:space-between;align-items:baseline;padding-top:8px;border-top:2px solid rgba(6,182,212,0.3);font-weight:700;font-size:14px}
.unpaid-total .num{font-family:var(--font-mono);font-size:18px}

.muted{color:var(--text-muted);font-size:12px}
.err-warn{margin-top:8px;color:#ff8aa3;font-size:11px}

footer{text-align:center;color:var(--text-dim);font-size:10px;padding:24px 0;letter-spacing:2px}
</style>
</head>
<body>
<header>
  <div class="brand">📊 BACOPYMASTER LEDGER</div>
  <nav class="nav">
    <a href="/master">← Master へ戻る</a>
    <a href="/master/ledger" class="active">家計簿</a>
    <a href="https://www.bafather.uk/admin/ledger" target="_blank" rel="noopener noreferrer">bafather で開く ↗</a>
  </nav>
</header>

<main>
  <div class="page-head">
    <div class="label">ADMIN CONSOLE</div>
    <h1>FX 運用家計簿</h1>
    <div class="sub">投資家・運用者・会社の資金フローをすべて記録・可視化</div>
  </div>

  __ERROR__

  <details class="spec-summary">
    <summary style="cursor:pointer;color:var(--accent);font-weight:700">📖 計算ルール / 用語の説明</summary>
    <div style="margin-top:10px;display:grid;gap:6px">
      <div><strong>1つめ口座</strong>: Hさんが BET する口座 ($50,000)。毎日の利益は<strong> H さんが全額出金</strong>するため口座は常に $50,000 維持。会計上は利益の 80% が J/K/会社 の取り分として残る。</div>
      <div><strong>2つめ口座</strong>: 運用者が管理する口座 ($46,900)。<strong>J/K/会社 への経費出金はここから物理的に行う</strong>。</div>
      <div><strong>別チャージ ($2,100)</strong>: 運用者自己資金。Hさん画面では「チャージ資金」表示。AI 開発費 (運用益外例外) の支払い財源。</div>
      <div><strong>未出金取り分 (1つめ口座のみで計算)</strong>: 1つめ口座累計取り分 − J/K/会社 既出金累計。AI 開発費は運用益外例外なので含めない。</div>
      <div><strong>運用者残利益</strong>: 1つめ 80% + 2つめ利益 − 2つめ口座からの出金累計。</div>
      <div class="warn">⚠ <strong>将来予定</strong>: Hさんの累積出金が投資総額 ($96,900) に近づくと、利益の 80% を chargeBalance に再入金する動きが発生。今後対応予定。</div>
    </div>
  </details>

  __BODY__
</main>

<footer>BACOPYMASTER LEDGER — read-only mirror of bafather.uk/admin/ledger</footer>

</body>
</html>
"""
