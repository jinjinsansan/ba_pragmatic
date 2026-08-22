"""AI Pattern Analysis -- 純粋統計 + 組み罫線疑惑分析

72万ハンドのデータから、ユーザーが伝えた大路/中国罫線の概念とは独立に、
AIとして純粋にパターンを発見する。

分析角度:
  基本: P/B/T比率、自然ランダムとの乖離（カイ二乗検定）
  1. マルコフ連鎖（N=2,3,5,7）
  2. 時間特徴量との相関（曜日/時間帯/月末）
  3. テーブル個体差
  4. 連続後の反転/継続確率
  5. シューフェーズ別偏り
  6. Tieの位置パターン
  組み罫線疑惑:
  A. ランダム性検定（カイ二乗）
  B. テーブル×時間×曜日クロス
  C. シュー間連続性（自己相関）
  D. テーブル間同期性
  E. 「分かりやすいパターン」異常発生率
  F. シュー初期の偏り
  G. 引っかけパターン検出
  H. 周期性検出
"""
import subprocess
import sys
import io
import json
import math
from collections import Counter, defaultdict
import statistics

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

SSH_KEY = r'C:\Users\USER\.ssh\laplace_vps'
SSH_HOST = 'laplace@210.131.215.116'

# Theoretical baccarat probabilities (8-deck)
P_PROB = 0.4462
B_PROB = 0.4586
T_PROB = 0.0952
# Player BET break-even (with house edge)
PLAYER_BREAKEVEN = 0.5050


def fetch(query: str) -> str:
    cmd = [
        'ssh', '-i', SSH_KEY, SSH_HOST,
        f'sqlite3 -separator "|" /opt/laplace/analytics.sqlite3 "{query}"'
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                       encoding='utf-8', errors='replace')
    return r.stdout.strip() if r.returncode == 0 else ''


def fetch_all_shoes():
    """全シューのデータを取得"""
    print("Fetching all shoes from VPS...")
    out = fetch(
        "SELECT id, table_id, table_name, day_of_week, hour_of_day, "
        "is_weekend, is_month_end, hand_count, result_sequence, started_at "
        "FROM shoes_analytics WHERE hand_count >= 30;"
    )
    rows = []
    for line in out.split('\n'):
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) >= 9:
            rows.append({
                'id': int(parts[0]),
                'table_id': parts[1],
                'table_name': parts[2],
                'dow': int(parts[3]),
                'hour': int(parts[4]),
                'is_weekend': int(parts[5]),
                'is_month_end': int(parts[6]),
                'hand_count': int(parts[7]),
                'sequence': parts[8],
                'started_at': parts[9] if len(parts) > 9 else '',
            })
    print(f"Loaded {len(rows)} shoes")
    return rows


def chi_square_test(observed: dict, expected: dict, label: str):
    """カイ二乗検定 — 観測値が期待値と有意に違うか"""
    chi2 = 0
    for key in expected:
        obs = observed.get(key, 0)
        exp = expected.get(key, 0)
        if exp > 0:
            chi2 += (obs - exp) ** 2 / exp
    return chi2


# ============================
# 基本統計
# ============================

def basic_stats(shoes):
    print("\n" + "=" * 60)
    print("[基本統計] 全結果の比率")
    print("=" * 60)
    counter = Counter()
    for s in shoes:
        for ch in s['sequence']:
            if ch in ('P', 'B', 'T'):
                counter[ch] += 1
    total = sum(counter.values())
    print(f"Total hands: {total:,}")
    p_ratio = counter['P'] / total
    b_ratio = counter['B'] / total
    t_ratio = counter['T'] / total
    print(f"  P: {counter['P']:,} ({p_ratio*100:.2f}%) [theory: {P_PROB*100:.2f}%]")
    print(f"  B: {counter['B']:,} ({b_ratio*100:.2f}%) [theory: {B_PROB*100:.2f}%]")
    print(f"  T: {counter['T']:,} ({t_ratio*100:.2f}%) [theory: {T_PROB*100:.2f}%]")

    # カイ二乗検定
    expected = {'P': total * P_PROB, 'B': total * B_PROB, 'T': total * T_PROB}
    chi2 = chi_square_test(counter, expected, "基本")
    print(f"  Chi-square vs theory: {chi2:.2f} (>5.99 = 95% significant)")
    if chi2 > 5.99:
        print(f"  >>> NOT TRULY RANDOM <<<")


# ============================
# 1. マルコフ連鎖
# ============================

def markov_analysis(shoes, n: int):
    print(f"\n--- マルコフ連鎖 N={n} ---")
    transitions = defaultdict(Counter)
    for s in shoes:
        non_tie = [c for c in s['sequence'] if c in ('P', 'B')]
        for i in range(len(non_tie) - n):
            prev = ''.join(non_tie[i:i+n])
            nxt = non_tie[i+n]
            transitions[prev][nxt] += 1

    # 各先行パターンで Player の確率
    best = []  # (prev, total, p_count, p_ratio)
    for prev, c in transitions.items():
        total = c['P'] + c['B']
        if total >= 100:  # 十分なサンプル
            p_ratio = c['P'] / total
            best.append((prev, total, c['P'], p_ratio))

    # Player ratio が高い順
    best.sort(key=lambda x: x[3], reverse=True)
    print(f"  Top 5 Player-favoring patterns (>= 100 samples):")
    for prev, total, p, ratio in best[:5]:
        marker = " <<< PROFITABLE" if ratio > PLAYER_BREAKEVEN else ""
        print(f"    {prev} -> P: {p}/{total} = {ratio*100:.2f}%{marker}")

    print(f"  Top 5 Banker-favoring patterns:")
    for prev, total, p, ratio in best[-5:]:
        print(f"    {prev} -> P: {p}/{total} = {ratio*100:.2f}%")


# ============================
# 2. 時間特徴量
# ============================

def time_analysis(shoes):
    print("\n" + "=" * 60)
    print("[時間特徴量] 曜日・時間帯・月末")
    print("=" * 60)

    # 曜日別
    dow_stats = defaultdict(lambda: Counter())
    dow_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for s in shoes:
        for ch in s['sequence']:
            if ch in ('P', 'B'):
                dow_stats[s['dow']][ch] += 1

    print("\n  By day of week:")
    for d in range(7):
        c = dow_stats[d]
        total = c['P'] + c['B']
        if total > 0:
            ratio = c['P'] / total
            marker = " <<<" if ratio > PLAYER_BREAKEVEN else ""
            print(f"    {dow_names[d]}: P={c['P']:,}/{total:,} = {ratio*100:.2f}%{marker}")

    # 時間帯別（4時間区切り）
    print("\n  By hour bucket (4h):")
    hour_stats = defaultdict(lambda: Counter())
    for s in shoes:
        bucket = s['hour'] // 4
        for ch in s['sequence']:
            if ch in ('P', 'B'):
                hour_stats[bucket][ch] += 1
    for b in sorted(hour_stats.keys()):
        c = hour_stats[b]
        total = c['P'] + c['B']
        ratio = c['P'] / total if total else 0
        marker = " <<<" if ratio > PLAYER_BREAKEVEN else ""
        print(f"    {b*4:02d}-{b*4+3:02d}h: P={c['P']:,}/{total:,} = {ratio*100:.2f}%{marker}")

    # 月末 vs 月中
    print("\n  Month-end vs mid-month:")
    me_stats = {0: Counter(), 1: Counter()}
    for s in shoes:
        for ch in s['sequence']:
            if ch in ('P', 'B'):
                me_stats[s['is_month_end']][ch] += 1
    for me in (0, 1):
        c = me_stats[me]
        total = c['P'] + c['B']
        ratio = c['P'] / total if total else 0
        label = "Month-end" if me else "Mid-month"
        marker = " <<<" if ratio > PLAYER_BREAKEVEN else ""
        print(f"    {label}: P={c['P']:,}/{total:,} = {ratio*100:.2f}%{marker}")


# ============================
# 3. テーブル個体差
# ============================

def table_analysis(shoes):
    print("\n" + "=" * 60)
    print("[テーブル個体差] Player偏りの強いテーブル")
    print("=" * 60)
    table_stats = defaultdict(lambda: {'P': 0, 'B': 0, 'name': ''})
    for s in shoes:
        for ch in s['sequence']:
            if ch in ('P', 'B'):
                table_stats[s['table_id']][ch] += 1
                table_stats[s['table_id']]['name'] = s['table_name']

    results = []
    for tid, c in table_stats.items():
        total = c['P'] + c['B']
        if total >= 1000:
            ratio = c['P'] / total
            results.append((c['name'], total, c['P'], ratio))

    results.sort(key=lambda x: x[3], reverse=True)
    print(f"\n  Top 10 Player-favoring tables (>= 1000 hands):")
    for name, total, p, ratio in results[:10]:
        marker = " <<<" if ratio > PLAYER_BREAKEVEN else ""
        print(f"    {name[:35]:<35} P={p:,}/{total:,} = {ratio*100:.2f}%{marker}")

    print(f"\n  Bottom 5 (Banker-favoring):")
    for name, total, p, ratio in results[-5:]:
        print(f"    {name[:35]:<35} P={p:,}/{total:,} = {ratio*100:.2f}%")


# ============================
# 4. 連続後の反転/継続
# ============================

def streak_analysis(shoes):
    print("\n" + "=" * 60)
    print("[連続後の反転/継続] ストリーク後の偏り")
    print("=" * 60)

    # streak_stats[streak_side][streak_len] = {continue: x, reverse: y}
    stats = defaultdict(lambda: defaultdict(lambda: {'cont': 0, 'rev': 0}))
    for s in shoes:
        non_tie = [c for c in s['sequence'] if c in ('P', 'B')]
        if len(non_tie) < 3:
            continue
        i = 0
        while i < len(non_tie) - 1:
            side = non_tie[i]
            j = i
            while j < len(non_tie) and non_tie[j] == side:
                j += 1
            streak_len = j - i
            if j < len(non_tie):
                if non_tie[j] == side:
                    stats[side][streak_len]['cont'] += 1
                else:
                    stats[side][streak_len]['rev'] += 1
            i = j

    print("\n  After P streak (continue P vs reverse to B):")
    for sl in sorted(stats['P'].keys())[:8]:
        d = stats['P'][sl]
        total = d['cont'] + d['rev']
        if total >= 50:
            cont_pct = d['cont'] / total * 100
            print(f"    P x{sl}: continue={d['cont']}, reverse={d['rev']}  cont={cont_pct:.1f}%")

    print("\n  After B streak (next is P=Player BET wins):")
    for sl in sorted(stats['B'].keys())[:8]:
        d = stats['B'][sl]
        total = d['cont'] + d['rev']
        if total >= 50:
            rev_pct = d['rev'] / total * 100
            marker = " <<<" if rev_pct > PLAYER_BREAKEVEN * 100 else ""
            print(f"    B x{sl}: continue={d['cont']}, reverse-to-P={d['rev']}  P-win={rev_pct:.1f}%{marker}")


# ============================
# 5. シューフェーズ別
# ============================

def phase_analysis(shoes):
    print("\n" + "=" * 60)
    print("[シューフェーズ別] 序盤・中盤・終盤")
    print("=" * 60)

    phase_stats = {'early': Counter(), 'mid': Counter(), 'late': Counter()}
    for s in shoes:
        for i, ch in enumerate(s['sequence']):
            if ch not in ('P', 'B'):
                continue
            if i < 20:
                phase_stats['early'][ch] += 1
            elif i < 50:
                phase_stats['mid'][ch] += 1
            else:
                phase_stats['late'][ch] += 1

    for phase in ['early', 'mid', 'late']:
        c = phase_stats[phase]
        total = c['P'] + c['B']
        ratio = c['P'] / total if total else 0
        marker = " <<<" if ratio > PLAYER_BREAKEVEN else ""
        print(f"  {phase}: P={c['P']:,}/{total:,} = {ratio*100:.2f}%{marker}")


# ============================
# A. テーブル×時間×曜日クロス（組み罫線疑惑）
# ============================

def table_time_cross(shoes):
    print("\n" + "=" * 60)
    print("[組み罫線疑惑A] テーブル×曜日×時間帯クロス")
    print("=" * 60)
    cross = defaultdict(lambda: Counter())
    for s in shoes:
        key = (s['table_name'], s['dow'], s['hour'] // 4)
        for ch in s['sequence']:
            if ch in ('P', 'B'):
                cross[key][ch] += 1

    results = []
    for key, c in cross.items():
        total = c['P'] + c['B']
        if total >= 200:
            ratio = c['P'] / total
            results.append((key, total, c['P'], ratio))

    results.sort(key=lambda x: x[3], reverse=True)
    dow_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    print(f"\n  Top 10 Player-favoring (table x dow x hour, >= 200 hands):")
    for key, total, p, ratio in results[:10]:
        tn, dow, hb = key
        marker = " <<<" if ratio > PLAYER_BREAKEVEN else ""
        print(f"    {tn[:25]:<25} {dow_names[dow]} {hb*4:02d}-{hb*4+3:02d}h: {p}/{total} = {ratio*100:.2f}%{marker}")


# ============================
# B. テーブル間同期性
# ============================

def table_sync(shoes):
    print("\n" + "=" * 60)
    print("[組み罫線疑惑B] テーブル間同期性（同時刻に同じパターン?）")
    print("=" * 60)
    # 1時間ごとに、同じ時間帯に開始したシューの結果列を比較
    # 簡易版: 同じ時間帯のシューのP比率の分散を見る
    hour_groups = defaultdict(list)
    for s in shoes:
        non_tie = [c for c in s['sequence'] if c in ('P', 'B')]
        if len(non_tie) < 30:
            continue
        p_ratio = non_tie.count('P') / len(non_tie)
        # started_at のYYYY-MM-DDTHH をキーにする
        if s['started_at']:
            hour_key = s['started_at'][:13]
            hour_groups[hour_key].append(p_ratio)

    # 各時間帯のP比率の分散
    variances = []
    for hk, ratios in hour_groups.items():
        if len(ratios) >= 5:
            var = statistics.variance(ratios)
            variances.append(var)

    if variances:
        avg_var = statistics.mean(variances)
        print(f"  Mean variance of P ratio across simultaneous shoes: {avg_var:.4f}")
        print(f"  (Lower = more synchronized = suspicious)")
        # 真にランダムなら 0.4462*0.5538/40 ≈ 0.0062 (40ハンドのSE^2)
        expected_var = (P_PROB * (1-P_PROB)) / 40
        print(f"  Expected variance (random, n=40): {expected_var:.4f}")
        if avg_var < expected_var * 0.8:
            print(f"  >>> SIGNIFICANTLY LOWER VARIANCE — POSSIBLE SYNC <<<")


# ============================
# C. パターン異常発生率
# ============================

def pattern_anomaly(shoes):
    print("\n" + "=" * 60)
    print("[組み罫線疑惑C] テレコ・ニコニコ等の異常発生率")
    print("=" * 60)
    # 各シューの最大連続テレコ長を計測
    tereko_counts = []
    nikoniko_counts = []
    for s in shoes:
        non_tie = [c for c in s['sequence'] if c in ('P', 'B')]
        if len(non_tie) < 20:
            continue
        # テレコ長: 連続して交互になっている長さ
        max_tereko = 0
        cur = 1
        for i in range(1, len(non_tie)):
            if non_tie[i] != non_tie[i-1]:
                cur += 1
                max_tereko = max(max_tereko, cur)
            else:
                cur = 1
        tereko_counts.append(max_tereko)

    if tereko_counts:
        avg = statistics.mean(tereko_counts)
        print(f"  Avg max tereko length per shoe: {avg:.2f}")
        # 期待値（ランダム）: 約 log(N) / log(2) ≈ 5.6 for N=50
        print(f"  Expected (random, ~50 hands): ~5.6")
        if avg > 7:
            print(f"  >>> HIGHER THAN RANDOM — possible inserted patterns <<<")
        # 異常に長いテレコ（10以上）の頻度
        long_tereko = sum(1 for t in tereko_counts if t >= 10)
        print(f"  Shoes with tereko >= 10: {long_tereko}/{len(tereko_counts)} = {long_tereko/len(tereko_counts)*100:.2f}%")


# ============================
# D. シュー初期の偏り
# ============================

def early_shoe_bias(shoes):
    print("\n" + "=" * 60)
    print("[組み罫線疑惑D] シュー初期パターンとその後の関係")
    print("=" * 60)
    # 最初の5ハンドのP比率と、6ハンド目以降のP比率の相関
    early_and_late = []
    for s in shoes:
        non_tie = [c for c in s['sequence'] if c in ('P', 'B')]
        if len(non_tie) < 30:
            continue
        early = non_tie[:5]
        late = non_tie[5:]
        early_p = early.count('P') / 5
        late_p = late.count('P') / len(late)
        early_and_late.append((early_p, late_p))

    if early_and_late:
        # 早期P比率が高いグループのその後のP比率
        high_early = [late for early, late in early_and_late if early >= 0.6]
        low_early = [late for early, late in early_and_late if early <= 0.4]
        if high_early and low_early:
            print(f"  Early P>=60% (n={len(high_early)}): later P avg = {statistics.mean(high_early)*100:.2f}%")
            print(f"  Early P<=40% (n={len(low_early)}): later P avg = {statistics.mean(low_early)*100:.2f}%")
            diff = statistics.mean(high_early) - statistics.mean(low_early)
            print(f"  Diff: {diff*100:+.2f}%")
            if abs(diff) > 0.02:
                print(f"  >>> SIGNIFICANT EARLY->LATE CORRELATION <<<")


# ============================
# Main
# ============================

def main():
    print("=" * 60)
    print("LAPLACE AI Pattern Analysis")
    print("=" * 60)
    shoes = fetch_all_shoes()
    if not shoes:
        print("No data")
        return

    basic_stats(shoes)

    print("\n" + "=" * 60)
    print("[1. マルコフ連鎖] 過去N手→次の手")
    print("=" * 60)
    for n in (2, 3, 5, 7):
        markov_analysis(shoes, n)

    time_analysis(shoes)
    table_analysis(shoes)
    streak_analysis(shoes)
    phase_analysis(shoes)
    table_time_cross(shoes)
    table_sync(shoes)
    pattern_anomaly(shoes)
    early_shoe_bias(shoes)

    print("\n" + "=" * 60)
    print("Analysis complete")
    print("=" * 60)


if __name__ == "__main__":
    main()
