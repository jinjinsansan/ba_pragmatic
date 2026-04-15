"""テレコ発生予測 & SEQ戦略成功予測 (ユーザー案 + 案C)

観測窓: 冒頭20手 (P/Bのみ、タイ除く)
予測対象1: 残りシューが「テレコ支配」か (短列率 >= 0.70)
予測対象2: SEQ戦略を残りシューで回した時、$30利確達成するか

ゴール: 観測窓の特徴量 → 予測対象の条件付き確率を測り、
       確率の高いビンを「狙い撃ちエントリー条件」にする。

Usage:
  python tereko_predictor.py
"""
import sqlite3
import os
import math
from collections import defaultdict, Counter
from datetime import datetime

DB_PATH = "analytics_vps.sqlite3"
DATE_FROM = "2026-04-06"
MIN_HANDS = 50
OBS_WINDOW = 20  # 冒頭20手（P/B）

# SEQ simulation parameters
SEQ = [1, 3, 5, 7, 10, 13, 16, 20, 25, 30, 35, 40, 45, 50,
       60, 70, 80, 90, 100, 110, 120, 130, 145, 160, 175, 190,
       205, 220, 235, 250, 265, 280, 300, 320, 340, 360, 380,
       400, 420, 440, 460, 480, 500]
SET_SIZE = 5
PROFIT_TARGET = 30.0
BANKER_COMMISSION = 0.05


def strip_ties(s):
    return ''.join(c for c in s if c in ('P', 'B'))


def columns_of(pb):
    if not pb:
        return []
    cols, cur, last = [], 0, None
    for ch in pb:
        if ch == last:
            cur += 1
        else:
            if last is not None:
                cols.append(cur)
            cur = 1
            last = ch
    if cur > 0:
        cols.append(cur)
    return cols


def extract_features(obs_pb):
    """冒頭20手(P/B)から特徴量抽出"""
    cols = columns_of(obs_pb)
    n = len(obs_pb)
    nc = len(cols)
    if nc == 0 or n == 0:
        return None
    p = obs_pb.count('P')
    b = obs_pb.count('B')
    p_ratio = p / n
    short = sum(1 for L in cols if L <= 2) / nc
    long_ = sum(1 for L in cols if L >= 4) / nc
    max_col = max(cols)
    mean_col = sum(cols) / nc
    var_col = sum((L - mean_col) ** 2 for L in cols) / nc
    # 前半 vs 後半
    half = n // 2
    fh_cols = columns_of(obs_pb[:half])
    sh_cols = columns_of(obs_pb[half:])
    fh_short = (sum(1 for L in fh_cols if L <= 2) / len(fh_cols)) if fh_cols else 0
    sh_short = (sum(1 for L in sh_cols if L <= 2) / len(sh_cols)) if sh_cols else 0
    self_sim = 1.0 - abs(fh_short - sh_short)
    return {
        'short': short,
        'long': long_,
        'max_col': max_col,
        'mean_col': mean_col,
        'var_col': var_col,
        'p_ratio': p_ratio,
        'n_cols': nc,
        'self_sim': self_sim,
    }


def simulate_seq(pb_sequence):
    """残りシューでSEQ戦略を回し、成功/失敗とmax DD を返す。

    テレコ戦略: 直前の非タイの逆を BET。SET_SIZE 手でセット完了、
    差分<0 なら SEQ++、>0 なら SEQ--（簡易 slashed なし版）。
    $30累積で成功、SEQ最終に達したら失敗。
    """
    if len(pb_sequence) < 2:
        return None
    cumulative = 0.0
    unit_idx = 0
    turns = []
    min_cum = 0.0
    reached_target = False
    last = pb_sequence[0]
    for ch in pb_sequence[1:]:
        bet_side = 'P' if last == 'B' else 'B'
        won = (ch == bet_side)
        turns.append((won, bet_side))
        last = ch
        if len(turns) == SET_SIZE:
            base = SEQ[min(unit_idx, len(SEQ)-1)]
            money = 0.0
            for w, bs in turns:
                if w:
                    money += base * (1.0 - BANKER_COMMISSION) if bs == 'B' else base
                else:
                    money -= base
            cumulative += money
            min_cum = min(min_cum, cumulative)
            wins = sum(1 for w, _ in turns if w)
            diff = wins - (SET_SIZE - wins)
            if diff < 0:
                unit_idx = min(unit_idx + 1, len(SEQ) - 1)
            elif diff > 0:
                unit_idx = max(unit_idx - 1, 0)
            turns = []
            if cumulative >= PROFIT_TARGET:
                reached_target = True
                break
    return {
        'reached_target': reached_target,
        'final_cumulative': cumulative,
        'max_dd': -min_cum,
        'last_unit_idx': unit_idx,
    }


def bin_features(f):
    """特徴量をビン化して組合せキーを作成"""
    def bin_short(v):
        if v < 0.5: return 'S<0.5'
        if v < 0.7: return 'S0.5-0.7'
        if v < 0.85: return 'S0.7-0.85'
        return 'S>=0.85'
    def bin_long(v):
        if v < 0.1: return 'L<0.1'
        if v < 0.3: return 'L0.1-0.3'
        return 'L>=0.3'
    def bin_sim(v):
        if v < 0.7: return 'Sim<0.7'
        if v < 0.85: return 'Sim0.7-0.85'
        return 'Sim>=0.85'
    def bin_max(v):
        if v <= 2: return 'Max<=2'
        if v <= 3: return 'Max=3'
        if v <= 4: return 'Max=4'
        return 'Max>=5'
    def bin_p(v):
        if v < 0.4: return 'P<0.4'
        if v < 0.5: return 'P0.4-0.5'
        if v < 0.6: return 'P0.5-0.6'
        return 'P>=0.6'
    return (
        bin_short(f['short']),
        bin_long(f['long']),
        bin_sim(f['self_sim']),
        bin_max(f['max_col']),
        bin_p(f['p_ratio']),
    )


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name, result_sequence, started_at FROM shoes_analytics "
        "WHERE hand_count >= ? AND started_at >= ? ORDER BY started_at",
        (MIN_HANDS, DATE_FROM)
    )
    shoes = cur.fetchall()
    conn.close()
    print(f"Loaded {len(shoes):,} shoes")

    # Collect samples
    samples = []  # list of (features, tereko_label, seq_success, tn, hour)
    for tn, seq, ts in shoes:
        pb = strip_ties(seq)
        if len(pb) < OBS_WINDOW + 20:
            continue
        obs = pb[:OBS_WINDOW]
        rest = pb[OBS_WINDOW:]
        feats = extract_features(obs)
        if not feats:
            continue
        # Target 1: 残りシューがテレコ支配か
        rest_cols = columns_of(rest)
        if len(rest_cols) == 0:
            continue
        rest_short_rate = sum(1 for L in rest_cols if L <= 2) / len(rest_cols)
        is_tereko = rest_short_rate >= 0.70
        # Target 2: SEQ戦略が成功するか
        sim = simulate_seq(rest)
        seq_success = sim['reached_target'] if sim else False
        try:
            hour = int(ts[11:13])
        except:
            hour = -1
        samples.append({
            'features': feats,
            'bin': bin_features(feats),
            'tereko': is_tereko,
            'seq_ok': seq_success,
            'table': tn,
            'hour': hour,
            'max_dd': sim['max_dd'] if sim else 0,
        })

    print(f"Collected {len(samples):,} samples")

    # ===== Baseline =====
    base_tereko = sum(1 for s in samples if s['tereko']) / len(samples)
    base_seq = sum(1 for s in samples if s['seq_ok']) / len(samples)
    print(f"Baseline テレコ率: {base_tereko*100:.2f}%")
    print(f"Baseline SEQ成功率: {base_seq*100:.2f}%")

    # ===== 特徴量ビン × テレコ発生率 =====
    bin_tereko = defaultdict(lambda: Counter())
    bin_seq = defaultdict(lambda: Counter())
    for s in samples:
        bin_tereko[s['bin']]['total'] += 1
        bin_seq[s['bin']]['total'] += 1
        if s['tereko']:
            bin_tereko[s['bin']]['yes'] += 1
        if s['seq_ok']:
            bin_seq[s['bin']]['yes'] += 1

    # テレコ予測ランキング
    tereko_ranking = []
    for bkey, c in bin_tereko.items():
        t = c['total']
        if t < 100:
            continue
        rate = c['yes'] / t
        se = math.sqrt(base_tereko * (1 - base_tereko) / t)
        z = (rate - base_tereko) / se if se > 0 else 0
        tereko_ranking.append((bkey, rate, z, t, c['yes']))
    tereko_ranking.sort(key=lambda x: -x[1])  # 確率降順

    # SEQ成功予測ランキング
    seq_ranking = []
    for bkey, c in bin_seq.items():
        t = c['total']
        if t < 100:
            continue
        rate = c['yes'] / t
        se = math.sqrt(base_seq * (1 - base_seq) / t)
        z = (rate - base_seq) / se if se > 0 else 0
        seq_ranking.append((bkey, rate, z, t, c['yes']))
    seq_ranking.sort(key=lambda x: -x[1])

    # ===== Table別 テレコ発生率 =====
    tbl_tereko = defaultdict(lambda: Counter())
    tbl_seq = defaultdict(lambda: Counter())
    for s in samples:
        tbl_tereko[s['table']]['total'] += 1
        tbl_seq[s['table']]['total'] += 1
        if s['tereko']:
            tbl_tereko[s['table']]['yes'] += 1
        if s['seq_ok']:
            tbl_seq[s['table']]['yes'] += 1

    table_stats = []
    for tn in tbl_tereko:
        t = tbl_tereko[tn]['total']
        if t < 50:
            continue
        tr = tbl_tereko[tn]['yes'] / t
        sr = tbl_seq[tn]['yes'] / t
        table_stats.append((tn, tr, sr, t))
    table_stats.sort(key=lambda x: -x[2])  # SEQ成功率順

    # Print top findings
    print(f"\n{'='*80}")
    print("[TEREKO] テレコ発生率 TOP 15 ビン (観測窓20手 -> 残りシューが短列70%以上)")
    print(f"{'='*80}")
    print(f"{'Bin':70s} {'率':>8s} {'z':>7s} {'N':>6s}")
    for bkey, rate, z, t, y in tereko_ranking[:15]:
        key_str = '+'.join(bkey)
        print(f"{key_str[:70]:70s} {rate*100:>6.2f}% {z:>+6.2f} {t:>6,}")

    print(f"\n{'='*80}")
    print("[SEQ] SEQ成功率 TOP 15 ビン")
    print(f"{'='*80}")
    print(f"{'Bin':70s} {'率':>8s} {'z':>7s} {'N':>6s}")
    for bkey, rate, z, t, y in seq_ranking[:15]:
        key_str = '+'.join(bkey)
        print(f"{key_str[:70]:70s} {rate*100:>6.2f}% {z:>+6.2f} {t:>6,}")

    print(f"\n{'='*80}")
    print("[TABLE TOP] テーブル別 SEQ成功率 TOP 10")
    print(f"{'='*80}")
    print(f"{'Table':45s} {'Tereko率':>10s} {'SEQ成功率':>10s} {'N':>6s}")
    for tn, tr, sr, t in table_stats[:10]:
        print(f"{tn[:45]:45s} {tr*100:>9.1f}% {sr*100:>9.1f}% {t:>6,}")

    print(f"\n{'='*80}")
    print("[TABLE BOT] テーブル別 SEQ成功率 BOTTOM 10")
    print(f"{'='*80}")
    for tn, tr, sr, t in table_stats[-10:]:
        print(f"{tn[:45]:45s} {tr*100:>9.1f}% {sr*100:>9.1f}% {t:>6,}")

    render_html(tereko_ranking, seq_ranking, table_stats, base_tereko, base_seq, len(samples))


def render_html(tereko_ranking, seq_ranking, table_stats, base_t, base_s, n):
    def bin_rows(ranking):
        h = ""
        for bkey, rate, z, t, y in ranking[:30]:
            key_str = ' + '.join(bkey)
            rc = '#4ade80' if z > 3 else ('#fbbf24' if z > 2 else '#8a96a8')
            h += (
                f"<tr><td>{key_str}</td>"
                f"<td>{t:,}</td><td>{y:,}</td>"
                f"<td style='color:{rc};font-weight:bold'>{rate*100:.2f}%</td>"
                f"<td style='color:{rc}'>z={z:+.2f}</td></tr>"
            )
        return h

    tbl_rows = ""
    for tn, tr, sr, t in table_stats[:30]:
        tc = '#4ade80' if sr > base_s * 1.2 else ('#fbbf24' if sr > base_s else '#f87171')
        tbl_rows += (
            f"<tr><td class='tname'>{tn}</td>"
            f"<td>{t:,}</td>"
            f"<td>{tr*100:.1f}%</td>"
            f"<td style='color:{tc};font-weight:bold'>{sr*100:.1f}%</td></tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>テレコ予測 & SEQ成功予測</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "Segoe UI", "Yu Gothic UI", sans-serif;
       background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5; }}
.container {{ max-width: 1400px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 26px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #c084fc; margin-top: 32px; font-size: 20px; }}
.nav a {{ display: inline-block; margin-right: 12px; padding: 8px 16px;
         background: #1a2332; color: #6dd5ed; text-decoration: none;
         border-radius: 4px; border: 1px solid #2a3441; font-size: 13px; }}
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
           gap: 12px; margin: 20px 0; }}
.card {{ background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed; }}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.g .value {{ color: #4ade80; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0; }}
table th {{ background: #0f1419; color: #c084fc; padding: 8px; text-align: left;
           border-bottom: 2px solid #2a3441; }}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
td.tname {{ font-weight: bold; color: #ffd700; }}
.note {{ background: #11192a; border-left: 4px solid #fbbf24;
        padding: 12px 16px; border-radius: 4px; margin: 16px 0; }}
</style></head><body><div class="container">
<h1>🎯 テレコ予測 & SEQ成功予測 — 冒頭20手の特徴量</h1>
<div class="nav"><a href="index.html">← レポートTOP</a></div>

<div class="note">
<strong>目的:</strong> 冒頭20手（観測窓）の特徴量から、<br>
<strong>(1) 残りシューがテレコ支配になるか</strong>、<strong>(2) SEQ戦略が$30利確に到達するか</strong> を予測。<br>
特徴量: short比率、long比率、内部自己相似性、最長ストリーク、P/B比率<br>
各特徴をビン化し、組合せ条件下の確率を算出。**ベースラインを大きく超えるビン = 狙い撃ち対象**。
</div>

<div class="summary">
  <div class="card"><div class="label">サンプル数</div><div class="value">{n:,}</div></div>
  <div class="card"><div class="label">テレコ支配率ベースライン</div><div class="value">{base_t*100:.1f}%</div></div>
  <div class="card"><div class="label">SEQ成功率ベースライン</div><div class="value">{base_s*100:.1f}%</div></div>
</div>

<h2>🎯 テレコ発生率 TOP 30 ビン条件</h2>
<p style="color:#8a96a8;font-size:13px">
短列比率・自己相似性・列長・P/B比率の5次元組合せ。<br>
<strong>狙い撃ち対象</strong>: テレコ率 &gt; 70% かつ z &gt; 3 のビン
</p>
<table>
<thead><tr><th>特徴量ビン (Short+Long+Sim+Max+P)</th><th>サンプル</th><th>テレコ数</th><th>テレコ率</th><th>z-score</th></tr></thead>
<tbody>{bin_rows(tereko_ranking)}</tbody>
</table>

<h2>💰 SEQ成功率 TOP 30 ビン条件</h2>
<p style="color:#8a96a8;font-size:13px">
SEQ戦略($1base, $30利確, 5ターン)が残りシューで利確到達する確率。<br>
<strong>狙い撃ち対象</strong>: SEQ成功率 &gt; {base_s*100*1.3:.0f}% のビン
</p>
<table>
<thead><tr><th>特徴量ビン</th><th>サンプル</th><th>成功数</th><th>成功率</th><th>z-score</th></tr></thead>
<tbody>{bin_rows(seq_ranking)}</tbody>
</table>

<h2>📊 テーブル別 SEQ成功率 TOP 30</h2>
<table>
<thead><tr><th>テーブル</th><th>サンプル</th><th>テレコ率</th><th>SEQ成功率</th></tr></thead>
<tbody>{tbl_rows}</tbody>
</table>

<p style="color:#8a96a8;font-size:11px;margin-top:32px;">
生成元: <code>tereko_predictor.py</code> / データ: {DB_PATH} / {DATE_FROM}〜 / 観測窓: {OBS_WINDOW}手
</p>
</div></body></html>
"""
    out = os.path.join("report", "tereko_predictor.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
