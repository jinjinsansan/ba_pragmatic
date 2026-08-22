import sqlite3, math
from collections import defaultdict

DB = "/opt/laplace2/analytics_pragmatic.sqlite3"
db = sqlite3.connect(DB)
shoes = db.execute(
    "SELECT result_sequence FROM shoes WHERE hand_count >= 20 AND result_sequence IS NOT NULL"
).fetchall()
print(f"shoes: {len(shoes):,}")

stats = defaultdict(lambda: {"bets":0,"wins":0,"losses":0})
total = {"bets":0,"wins":0,"losses":0}

for row in shoes:
    seq = row[0]
    pb = [c for c in seq if c in ("P","B")]  # TIE除く
    for g in range(len(pb) // 7):
        group = pb[g*7:(g+1)*7]
        first6 = group[:6]
        seventh = group[6]
        p_cnt = first6.count("P")
        b_cnt = first6.count("B")

        if   p_cnt == 4 and b_cnt == 2: bet, cond = "B", "P4B2->B"
        elif b_cnt == 4 and p_cnt == 2: bet, cond = "P", "B4P2->P"
        elif p_cnt == 5 and b_cnt == 1: bet, cond = "B", "P5B1->B"
        elif b_cnt == 5 and p_cnt == 1: bet, cond = "P", "B5P1->P"
        else: continue

        total["bets"] += 1
        stats[cond]["bets"] += 1
        if seventh == bet:
            total["wins"] += 1
            stats[cond]["wins"] += 1
        else:
            total["losses"] += 1
            stats[cond]["losses"] += 1

db.close()

eff = total["wins"] + total["losses"]
wr  = total["wins"] / eff * 100 if eff else 0
# PLAYER寄り想定でbaseline 49.32%
z   = (total["wins"]/eff - 0.4932) / math.sqrt(0.4932*0.5068/eff) if eff else 0

print()
print("=" * 55)
print("友人7ターン理論 バックテスト結果")
print("=" * 55)
print(f"総BET  : {total['bets']:,}")
print(f"WIN    : {total['wins']:,}")
print(f"LOSE   : {total['losses']:,}")
print(f"勝率   : {wr:.4f}%")
print(f"Z値    : {z:+.3f}")
print()
print("条件別内訳:")
print(f"  {'条件':<14} {'BET':>7} {'WIN':>7} {'LOSE':>7} {'勝率':>8}")
print("-" * 55)
for cond, s in sorted(stats.items()):
    e = s["wins"] + s["losses"]
    w = s["wins"] / e * 100 if e else 0
    print(f"  {cond:<14} {s['bets']:>7,} {s['wins']:>7,} {s['losses']:>7,} {w:>7.2f}%")
print("=" * 55)
