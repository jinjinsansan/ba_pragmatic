"""網羅的エッジスキャナー

Evolutionバカラは乱数ではない（非乱数性99.99%）。
組み罫線である以上、必ずどこかに統計的シグネチャが残る。
20個の仮説を一気に検証し、50%勝率を大きく超える "エッジ候補" を洗い出す。

Usage:
  python brute_force_edge_scanner.py
"""
import sqlite3
import os
import math
from collections import defaultdict, Counter
from datetime import datetime

DB_PATH = "analytics_vps.sqlite3"
DATE_FROM = "2026-04-06"
MIN_HANDS = 50


def load_shoes():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name, result_sequence, started_at FROM shoes_analytics "
        "WHERE hand_count >= ? AND started_at >= ? ORDER BY started_at",
        (MIN_HANDS, DATE_FROM)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


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


def z_score(wins, total, p0=0.5):
    """二項分布の z-score"""
    if total == 0:
        return 0.0
    p = wins / total
    se = math.sqrt(p0 * (1 - p0) / total)
    return (p - p0) / se


def win_rate_stats(wins, total):
    """勝率と z-score を返す"""
    if total == 0:
        return (0.0, 0.0, 0)
    wr = wins / total * 100
    z = z_score(wins, total)
    return (wr, z, total)


# ═══════════════════════════════════════════════════
# 仮説 1: 位置バイアス (ハンド#N の P/B 比)
# ═══════════════════════════════════════════════════
def test_position_bias(shoes):
    position_pb = defaultdict(lambda: Counter())
    for _, seq, _ in shoes:
        pb = strip_ties(seq)
        for i, ch in enumerate(pb):
            position_pb[i][ch] += 1
    results = []
    for pos in sorted(position_pb.keys())[:70]:
        c = position_pb[pos]
        total = c['P'] + c['B']
        if total < 500: continue
        # Banker側の勝率を見る（Pが50%超えはレア）
        wr_p, z_p, _ = win_rate_stats(c['P'], total)
        wr_b, z_b, _ = win_rate_stats(c['B'], total)
        if abs(z_p) > abs(z_b):
            results.append(('位置バイアス', f'#{pos} P BET', wr_p, z_p, total))
        else:
            results.append(('位置バイアス', f'#{pos} B BET', wr_b, z_b, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 2: N-gram先読み (直前3手から次)
# ═══════════════════════════════════════════════════
def test_ngram(shoes, n):
    ngram_next = defaultdict(Counter)
    for _, seq, _ in shoes:
        pb = strip_ties(seq)
        for i in range(len(pb) - n):
            prefix = pb[i:i+n]
            nxt = pb[i+n]
            ngram_next[prefix][nxt] += 1
    results = []
    for prefix, c in ngram_next.items():
        total = c['P'] + c['B']
        if total < 1000: continue
        wr_p, z_p, _ = win_rate_stats(c['P'], total)
        wr_b, z_b, _ = win_rate_stats(c['B'], total)
        if abs(z_p) > abs(z_b):
            results.append((f'{n}-gram先読み', f'{prefix}→P', wr_p, z_p, total))
        else:
            results.append((f'{n}-gram先読み', f'{prefix}→B', wr_b, z_b, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 3: ストリーク破断率
# ═══════════════════════════════════════════════════
def test_streak_break(shoes):
    # N連続後、次手が break (逆転) or continue
    streak_outcome = defaultdict(Counter)
    for _, seq, _ in shoes:
        pb = strip_ties(seq)
        if len(pb) < 2: continue
        streak = 1
        for i in range(1, len(pb)):
            if pb[i] == pb[i-1]:
                streak += 1
            else:
                # 次手は break
                streak_outcome[streak]['break'] += 1
                streak = 1
            if i + 1 < len(pb):
                # 次手があるならカウント
                pass
        # 最後のストリークはカウントしない (未完)
    # 実は continue vs break を見たいので作り直し
    streak_outcome = defaultdict(Counter)
    for _, seq, _ in shoes:
        pb = strip_ties(seq)
        if len(pb) < 3: continue
        streak = 1
        for i in range(1, len(pb) - 1):
            if pb[i] == pb[i-1]:
                streak += 1
            else:
                streak = 1
            nxt = pb[i+1]
            if nxt == pb[i]:
                streak_outcome[streak]['continue'] += 1
            else:
                streak_outcome[streak]['break'] += 1
    results = []
    for s, c in sorted(streak_outcome.items()):
        total = c['continue'] + c['break']
        if total < 500: continue
        # BREAK率（逆張りbetの勝率）
        wr_break, z_break, _ = win_rate_stats(c['break'], total)
        results.append(('ストリーク破断', f'{s}連続→break BET', wr_break, z_break, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 4: 時間帯バイアス (hour-of-day)
# ═══════════════════════════════════════════════════
def test_hour_bias(shoes):
    hour_pb = defaultdict(Counter)
    for _, seq, ts in shoes:
        try:
            hour = int(ts[11:13])
        except: continue
        pb = strip_ties(seq)
        for ch in pb:
            hour_pb[hour][ch] += 1
    results = []
    for h in sorted(hour_pb.keys()):
        c = hour_pb[h]
        total = c['P'] + c['B']
        if total < 5000: continue
        wr_p, z_p, _ = win_rate_stats(c['P'], total)
        wr_b, z_b, _ = win_rate_stats(c['B'], total)
        if abs(z_p) > abs(z_b):
            results.append(('時間帯バイアス', f'{h:02d}時 P BET', wr_p, z_p, total))
        else:
            results.append(('時間帯バイアス', f'{h:02d}時 B BET', wr_b, z_b, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 5: 曜日バイアス
# ═══════════════════════════════════════════════════
def test_weekday_bias(shoes):
    dow_pb = defaultdict(Counter)
    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for _, seq, ts in shoes:
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            dow = dt.weekday()
        except: continue
        pb = strip_ties(seq)
        for ch in pb:
            dow_pb[dow][ch] += 1
    results = []
    for d in sorted(dow_pb.keys()):
        c = dow_pb[d]
        total = c['P'] + c['B']
        if total < 5000: continue
        wr_p, z_p, _ = win_rate_stats(c['P'], total)
        wr_b, z_b, _ = win_rate_stats(c['B'], total)
        name = days[d] if d < 7 else str(d)
        if abs(z_p) > abs(z_b):
            results.append(('曜日バイアス', f'{name} P BET', wr_p, z_p, total))
        else:
            results.append(('曜日バイアス', f'{name} B BET', wr_b, z_b, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 6: テーブル固有 P/B バイアス
# ═══════════════════════════════════════════════════
def test_table_bias(shoes):
    tbl_pb = defaultdict(Counter)
    for tn, seq, _ in shoes:
        pb = strip_ties(seq)
        for ch in pb:
            tbl_pb[tn][ch] += 1
    results = []
    for tn, c in tbl_pb.items():
        total = c['P'] + c['B']
        if total < 5000: continue
        wr_p, z_p, _ = win_rate_stats(c['P'], total)
        wr_b, z_b, _ = win_rate_stats(c['B'], total)
        if abs(z_p) > abs(z_b):
            results.append(('テーブル固有', f'{tn} P BET', wr_p, z_p, total))
        else:
            results.append(('テーブル固有', f'{tn} B BET', wr_b, z_b, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 7: ミラー対称性 (hand#N vs hand#(last-N))
# ═══════════════════════════════════════════════════
def test_mirror_symmetry(shoes):
    matches = 0
    total = 0
    for _, seq, _ in shoes:
        pb = strip_ties(seq)
        L = len(pb)
        if L < 30: continue
        for i in range(L // 2):
            if pb[i] == pb[L - 1 - i]:
                matches += 1
            total += 1
    if total == 0:
        return []
    wr, z, _ = win_rate_stats(matches, total)
    return [('ミラー対称性', 'hand#N == hand#(len-N)', wr, z, total)]


# ═══════════════════════════════════════════════════
# 仮説 8: Q1パターン → Q2-Q4 予測
# ═══════════════════════════════════════════════════
def test_q1_to_rest(shoes):
    # Q1のPの割合(%) ビン vs Q2-Q4 の勝率
    bin_outcomes = defaultdict(lambda: Counter())
    for _, seq, _ in shoes:
        pb = strip_ties(seq)
        if len(pb) < 60: continue
        q_len = len(pb) // 4
        q1 = pb[:q_len]
        rest = pb[q_len:]
        if len(q1) == 0: continue
        p_rate = q1.count('P') / len(q1)
        # Q1のP率ビン: <0.4, 0.4-0.5, 0.5-0.6, >0.6
        if p_rate < 0.4: b = 'Q1_P<0.4'
        elif p_rate < 0.5: b = 'Q1_P0.4-0.5'
        elif p_rate < 0.6: b = 'Q1_P0.5-0.6'
        else: b = 'Q1_P>0.6'
        for ch in rest:
            bin_outcomes[b][ch] += 1
    results = []
    for b, c in bin_outcomes.items():
        total = c['P'] + c['B']
        if total < 2000: continue
        wr_p, z_p, _ = win_rate_stats(c['P'], total)
        wr_b, z_b, _ = win_rate_stats(c['B'], total)
        if abs(z_p) > abs(z_b):
            results.append(('Q1→Q2-4予測', f'{b} → P', wr_p, z_p, total))
        else:
            results.append(('Q1→Q2-4予測', f'{b} → B', wr_b, z_b, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 9: 同じテーブルの連続シュー記憶
# ═══════════════════════════════════════════════════
def test_shoe_memory(shoes):
    # 前シューがP寄り/B寄り → 次シューの傾向
    # shoesをテーブルごとに時系列でグループ化
    tbl_shoes = defaultdict(list)
    for tn, seq, ts in shoes:
        tbl_shoes[tn].append((ts, strip_ties(seq)))
    for tn in tbl_shoes:
        tbl_shoes[tn].sort()

    bin_next = defaultdict(Counter)
    for tn, s_list in tbl_shoes.items():
        for i in range(len(s_list) - 1):
            _, prev = s_list[i]
            _, nxt = s_list[i + 1]
            if not prev or not nxt: continue
            p_prev = prev.count('P') / len(prev)
            if p_prev < 0.45: b = 'prev_P寄り以下'
            elif p_prev > 0.55: b = 'prev_B寄り以上'
            else: b = 'prev_中立'
            for ch in nxt:
                bin_next[b][ch] += 1
    results = []
    for b, c in bin_next.items():
        total = c['P'] + c['B']
        if total < 2000: continue
        wr_p, z_p, _ = win_rate_stats(c['P'], total)
        wr_b, z_b, _ = win_rate_stats(c['B'], total)
        if abs(z_p) > abs(z_b):
            results.append(('シュー記憶', f'{b} → next P', wr_p, z_p, total))
        else:
            results.append(('シュー記憶', f'{b} → next B', wr_b, z_b, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 10: シュー冒頭5手 → 残りシュー
# ═══════════════════════════════════════════════════
def test_opening_signature(shoes):
    bin_rest = defaultdict(Counter)
    for _, seq, _ in shoes:
        pb = strip_ties(seq)
        if len(pb) < 30: continue
        opening = pb[:5]
        rest = pb[5:]
        for ch in rest:
            bin_rest[opening][ch] += 1
    results = []
    for op, c in bin_rest.items():
        total = c['P'] + c['B']
        if total < 2000: continue
        wr_p, z_p, _ = win_rate_stats(c['P'], total)
        wr_b, z_b, _ = win_rate_stats(c['B'], total)
        if abs(z_p) > abs(z_b):
            results.append(('冒頭5手シグネチャ', f'{op} → P', wr_p, z_p, total))
        else:
            results.append(('冒頭5手シグネチャ', f'{op} → B', wr_b, z_b, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 11: シュー位置%（前半・中盤・終盤）
# ═══════════════════════════════════════════════════
def test_shoe_phase(shoes):
    phase_pb = defaultdict(Counter)
    for _, seq, _ in shoes:
        pb = strip_ties(seq)
        L = len(pb)
        if L < 40: continue
        for i, ch in enumerate(pb):
            phase = int(i / L * 10)  # 10-bins
            phase_pb[phase][ch] += 1
    results = []
    for p, c in sorted(phase_pb.items()):
        total = c['P'] + c['B']
        if total < 5000: continue
        wr_p, z_p, _ = win_rate_stats(c['P'], total)
        wr_b, z_b, _ = win_rate_stats(c['B'], total)
        if abs(z_p) > abs(z_b):
            results.append(('シュー位置%', f'{p*10}-{(p+1)*10}% P', wr_p, z_p, total))
        else:
            results.append(('シュー位置%', f'{p*10}-{(p+1)*10}% B', wr_b, z_b, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 12: 列長シグネチャ → 次列長
# ═══════════════════════════════════════════════════
def test_column_sequence(shoes):
    # 直近3列長 → 次列が短(≤2) or 長(≥3)
    prev3_next = defaultdict(Counter)
    for _, seq, _ in shoes:
        pb = strip_ties(seq)
        cols = columns_of(pb)
        if len(cols) < 4: continue
        for i in range(len(cols) - 3):
            prev3 = tuple(cols[i:i+3])
            nxt = cols[i+3]
            cat = 'short' if nxt <= 2 else 'long'
            prev3_next[prev3][cat] += 1
    results = []
    for p3, c in prev3_next.items():
        total = c['short'] + c['long']
        if total < 500: continue
        wr_s, z_s, _ = win_rate_stats(c['short'], total)
        wr_l, z_l, _ = win_rate_stats(c['long'], total)
        if abs(z_s) > abs(z_l):
            results.append(('列長シーケンス', f'{p3} → short({wr_s:.1f}%)', wr_s, z_s, total))
        else:
            results.append(('列長シーケンス', f'{p3} → long({wr_l:.1f}%)', wr_l, z_l, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 13: 大ドラゴン後の連発
# ═══════════════════════════════════════════════════
def test_post_dragon(shoes):
    post = Counter()
    for _, seq, _ in shoes:
        pb = strip_ties(seq)
        cols = columns_of(pb)
        # 5+の列があった直後の2列の振る舞い
        cum = 0
        for i, L in enumerate(cols):
            if L >= 5 and i + 2 < len(cols):
                next_short = cols[i+1] <= 2
                next2_short = cols[i+2] <= 2
                if next_short and next2_short:
                    post['both_short'] += 1
                elif next_short or next2_short:
                    post['one_short'] += 1
                else:
                    post['none_short'] += 1
    total = sum(post.values())
    if total < 500: return []
    results = [('ドラゴン後', f'{k}', v/total*100, (v/total - 0.5)/math.sqrt(0.25/total), total) for k, v in post.items()]
    return results


# ═══════════════════════════════════════════════════
# 仮説 14: シュー内タイ数 → 後半の偏り
# ═══════════════════════════════════════════════════
def test_tie_density(shoes):
    bin_rest = defaultdict(Counter)
    for _, seq, _ in shoes:
        ties = seq.count('T')
        pb = strip_ties(seq)
        if len(pb) < 40: continue
        if ties < 3: b = 'tie<3'
        elif ties < 6: b = 'tie3-5'
        elif ties < 10: b = 'tie6-9'
        else: b = 'tie>=10'
        for ch in pb[len(pb)//2:]:
            bin_rest[b][ch] += 1
    results = []
    for b, c in bin_rest.items():
        total = c['P'] + c['B']
        if total < 2000: continue
        wr_p, z_p, _ = win_rate_stats(c['P'], total)
        wr_b, z_b, _ = win_rate_stats(c['B'], total)
        if abs(z_p) > abs(z_b):
            results.append(('タイ密度→後半', f'{b} → P', wr_p, z_p, total))
        else:
            results.append(('タイ密度→後半', f'{b} → B', wr_b, z_b, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 15: テーブル × 時間帯 交互作用
# ═══════════════════════════════════════════════════
def test_table_hour(shoes):
    th_pb = defaultdict(Counter)
    for tn, seq, ts in shoes:
        try:
            hour = int(ts[11:13])
        except: continue
        pb = strip_ties(seq)
        key = f"{tn}@{hour:02d}"
        for ch in pb:
            th_pb[key][ch] += 1
    results = []
    for key, c in th_pb.items():
        total = c['P'] + c['B']
        if total < 2000: continue
        wr_p, z_p, _ = win_rate_stats(c['P'], total)
        wr_b, z_b, _ = win_rate_stats(c['B'], total)
        if abs(z_p) > abs(z_b):
            results.append(('テーブル×時間', f'{key} P', wr_p, z_p, total))
        else:
            results.append(('テーブル×時間', f'{key} B', wr_b, z_b, total))
    return results


# ═══════════════════════════════════════════════════
# 仮説 16: 偶数番手・奇数番手のバイアス
# ═══════════════════════════════════════════════════
def test_parity(shoes):
    parity_pb = defaultdict(Counter)
    for _, seq, _ in shoes:
        pb = strip_ties(seq)
        for i, ch in enumerate(pb):
            parity_pb['odd' if i % 2 == 1 else 'even'][ch] += 1
    results = []
    for p, c in parity_pb.items():
        total = c['P'] + c['B']
        if total < 5000: continue
        wr_p, z_p, _ = win_rate_stats(c['P'], total)
        wr_b, z_b, _ = win_rate_stats(c['B'], total)
        if abs(z_p) > abs(z_b):
            results.append(('奇偶バイアス', f'{p}番手 P', wr_p, z_p, total))
        else:
            results.append(('奇偶バイアス', f'{p}番手 B', wr_b, z_b, total))
    return results


def render_html(all_results):
    # エッジ強度（|z|）でソート
    all_results.sort(key=lambda x: -abs(x[3]))

    rows = ""
    for cat, hyp, wr, z, total in all_results[:100]:
        # 50%からの乖離
        dev = wr - 50
        dev_c = '#4ade80' if dev > 1.0 else ('#fbbf24' if dev > 0 else '#f87171')
        # 有意性
        sig_c = '#4ade80' if abs(z) > 3 else ('#fbbf24' if abs(z) > 2 else '#8a96a8')
        rows += (
            f"<tr>"
            f"<td>{cat}</td>"
            f"<td>{hyp}</td>"
            f"<td>{total:,}</td>"
            f"<td style='color:{dev_c};font-weight:bold'>{wr:.2f}%</td>"
            f"<td style='color:{dev_c}'>{dev:+.2f}%</td>"
            f"<td style='color:{sig_c}'>z={z:+.2f}</td>"
            f"</tr>"
        )

    # カテゴリ別ベストエッジ
    cat_best = {}
    for cat, hyp, wr, z, total in all_results:
        if cat not in cat_best or abs(z) > abs(cat_best[cat][3]):
            cat_best[cat] = (cat, hyp, wr, z, total)
    cat_rows = ""
    for cat, (_, hyp, wr, z, total) in sorted(cat_best.items(), key=lambda x: -abs(x[1][3])):
        dev = wr - 50
        dev_c = '#4ade80' if dev > 1.0 else ('#fbbf24' if dev > 0 else '#f87171')
        cat_rows += (
            f"<tr>"
            f"<td style='color:#ffd700;font-weight:bold'>{cat}</td>"
            f"<td>{hyp}</td>"
            f"<td>{total:,}</td>"
            f"<td style='color:{dev_c};font-weight:bold'>{wr:.2f}%</td>"
            f"<td style='color:{dev_c}'>{dev:+.2f}%</td>"
            f"<td>z={z:+.2f}</td>"
            f"</tr>"
        )

    # 50% 以上エッジ数
    over_52 = sum(1 for r in all_results if r[2] > 52 and r[4] > 1000)
    over_53 = sum(1 for r in all_results if r[2] > 53 and r[4] > 1000)
    over_55 = sum(1 for r in all_results if r[2] > 55 and r[4] > 1000)
    sig_3 = sum(1 for r in all_results if abs(r[3]) > 3 and r[4] > 1000)

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>網羅的エッジスキャナー — 16仮説検証</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "Segoe UI", "Yu Gothic UI", sans-serif;
       background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5; }}
.container {{ max-width: 1400px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 26px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #c084fc; margin-top: 32px; font-size: 20px; }}
.nav {{ margin: 16px 0 24px; }}
.nav a {{ display: inline-block; margin-right: 12px; padding: 8px 16px;
         background: #1a2332; color: #6dd5ed; text-decoration: none;
         border-radius: 4px; border: 1px solid #2a3441; font-size: 13px; }}
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
           gap: 12px; margin: 20px 0; }}
.card {{ background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed; }}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.g .value {{ color: #4ade80; }}
.card.y .value {{ color: #fbbf24; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0; }}
table th {{ background: #0f1419; color: #c084fc; padding: 8px; text-align: left;
           border-bottom: 2px solid #2a3441; }}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
table tr:hover {{ background: rgba(255,255,255,0.03); }}
.note {{ background: #11192a; border-left: 4px solid #fbbf24;
        padding: 12px 16px; border-radius: 4px; margin: 16px 0; }}
</style></head><body><div class="container">
<h1>🔍 網羅的エッジスキャナー — 16仮説検証</h1>
<div class="nav"><a href="index.html">← レポートTOP</a></div>

<div class="summary">
  <div class="card"><div class="label">検証仮説数</div><div class="value">{len(all_results):,}</div></div>
  <div class="card g"><div class="label">勝率55%超え</div><div class="value">{over_55}</div></div>
  <div class="card y"><div class="label">勝率53%超え</div><div class="value">{over_53}</div></div>
  <div class="card"><div class="label">勝率52%超え</div><div class="value">{over_52}</div></div>
  <div class="card g"><div class="label">統計的有意 (|z|>3)</div><div class="value">{sig_3}</div></div>
</div>

<h2>🏆 カテゴリ別ベストエッジ</h2>
<p class="note"><strong>見方:</strong> 各カテゴリで |z|（標準化乖離）が最大の仮説。<br>
z > 3 = 統計的に強く有意（偶然の確率 0.3%以下）。<br>
勝率 > 53% かつ サンプル > 5,000 かつ z > 3 のものが実用ライン。</p>
<table>
<thead><tr><th>カテゴリ</th><th>仮説</th><th>サンプル</th><th>勝率</th><th>50%乖離</th><th>z-score</th></tr></thead>
<tbody>{cat_rows}</tbody>
</table>

<h2>📊 全仮説トップ100（|z|降順）</h2>
<table>
<thead><tr><th>カテゴリ</th><th>仮説</th><th>サンプル</th><th>勝率</th><th>50%乖離</th><th>z-score</th></tr></thead>
<tbody>{rows}</tbody>
</table>

<p style="color:#8a96a8;font-size:11px;margin-top:32px;">
生成元: <code>brute_force_edge_scanner.py</code> / データ: {DB_PATH} / {DATE_FROM}〜
</p>
</div></body></html>
"""
    out = os.path.join("report", "edge_scanner.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out}")


def main():
    shoes = load_shoes()
    print(f"Loaded {len(shoes):,} shoes")

    all_results = []
    tests = [
        ("位置バイアス", test_position_bias),
        ("2-gram", lambda s: test_ngram(s, 2)),
        ("3-gram", lambda s: test_ngram(s, 3)),
        ("4-gram", lambda s: test_ngram(s, 4)),
        ("5-gram", lambda s: test_ngram(s, 5)),
        ("ストリーク破断", test_streak_break),
        ("時間帯", test_hour_bias),
        ("曜日", test_weekday_bias),
        ("テーブル", test_table_bias),
        ("ミラー対称", test_mirror_symmetry),
        ("Q1→Q2-4", test_q1_to_rest),
        ("シュー記憶", test_shoe_memory),
        ("冒頭5手", test_opening_signature),
        ("シュー位置", test_shoe_phase),
        ("列長シーケンス", test_column_sequence),
        ("ドラゴン後", test_post_dragon),
        ("タイ密度", test_tie_density),
        ("奇偶", test_parity),
    ]

    for name, fn in tests:
        print(f"  Testing {name}...")
        try:
            results = fn(shoes)
            all_results.extend(results)
            print(f"    → {len(results)} hypotheses tested")
        except Exception as e:
            print(f"    → ERROR: {e}")

    print(f"\nTotal: {len(all_results)} hypotheses scored")

    # テーブル×時間（サンプル大きくなるので別途）
    print("  Testing テーブル×時間...")
    th_results = test_table_hour(shoes)
    all_results.extend(th_results)
    print(f"    → {len(th_results)} hypotheses")

    # Top edges print
    all_results.sort(key=lambda x: -abs(x[3]))
    print(f"\n{'='*80}")
    print(f"TOP 20 エッジ候補 (|z|降順):")
    print(f"{'='*80}")
    print(f"{'カテゴリ':18s} {'仮説':40s} {'サンプル':>8s} {'勝率':>7s} {'z':>7s}")
    for cat, hyp, wr, z, total in all_results[:20]:
        print(f"{cat[:18]:18s} {hyp[:40]:40s} {total:>8,} {wr:>6.2f}% {z:>+6.2f}")

    render_html(all_results)


if __name__ == "__main__":
    main()
