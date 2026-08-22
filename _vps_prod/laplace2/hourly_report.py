#!/usr/bin/env python3
# Hourly Telegram report -> 勝率監視チャンネル (WINRATE_CHAT_ID; falls back to pattern ch).
#   6パターン編  = 6-pattern (V2) forward signals  -> "resolve ... pattern in V2"
#   10パターン編 = v4 10-pattern actual published signals -> "[v4-settle] result=..."
# Cron: 0 * * * * (JST).
#   01-23時: 本日の時刻別勝率 (従来通り)
#   00時   : 前日の「確定版」時刻別勝率 (23時台まで全部入り)
#            + 日別勝率 直近7日表 (JSON永続・毎日1行追加・週計つき)
# VPS path: /opt/laplace2/hourly_report.py  (repo copy: _vps_hourly_report.py)
import json, os, re, sys, urllib.request, urllib.parse
from datetime import datetime, timedelta
from collections import Counter

LOG = '/opt/laplace2/dual_line_pragmatic_bot.log'
ENV = '/opt/laplace2/.env'
DAILY_JSON = '/opt/laplace2/winrate_daily_history.json'
DAILY_KEEP_DAYS = 30
WEEK_ROWS = 7

# 6-pattern set (V2 = LIVE_SIGNAL_PATTERNS in dual_line_pragmatic_bot.py)
V2 = {'niconico|dragon|P', 'niconico|niconico|B', 'sansan|telecho|P',
      'telecho|niconico|P', 'telecho|nikoichi|P', 'telecho|telecho|B'}

def load_env(path):
    d = {}
    try:
        for ln in open(path, encoding='utf-8'):
            ln = ln.strip()
            if ln and not ln.startswith('#') and '=' in ln:
                k, v = ln.split('=', 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return d

def parse():
    """Return (v2_rows, v4_rows): each list of (datetime, win01)."""
    v2, v4 = [], []
    try:
        for ln in open(LOG, encoding='utf-8', errors='replace'):
            m = re.search(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*resolve [^:]+: '
                          r'pred=[PB] outcome=[PBT] (WIN|LOSE).*pattern=([a-z]+\|[a-z]+\|[BP])', ln)
            if m and m.group(3) in V2:
                v2.append((datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'), 1 if m.group(2) == 'WIN' else 0))
                continue
            m = re.search(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*\[v4-settle\] posted settled .*result=(WIN|LOSE)', ln)
            if m:
                v4.append((datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'), 1 if m.group(2) == 'WIN' else 0))
    except Exception:
        pass
    v2.sort(); v4.sort()
    return v2, v4

def build(rows, title, src, target_date, final):
    tr = [(ts, w) for ts, w in rows if ts.date() == target_date]
    label = '確定(全日)' if final else f'{datetime.now().hour:02d}時時点'
    head = f'<b>{title}</b>  {target_date.strftime("%-m/%-d")} {label}(JST)'
    if not tr:
        return head + '\n(データなし)\n' + src
    bn, bw = Counter(), Counter()
    for ts, w in tr:
        bn[ts.hour] += 1; bw[ts.hour] += w
    n = len(tr); wn = sum(w for _, w in tr)
    last_h = 23 if final else tr[-1][0].hour
    body = ['時  件  勝率', '─' * 14]
    for h in range(min(bn), last_h + 1):
        nh = bn.get(h, 0)
        if nh == 0:
            body.append(f'{h:02d}   —'); continue
        wr = bw[h] / nh
        mark = ' 🔥' if wr >= 0.60 else (' ⚠️' if wr <= 0.40 else '')
        body.append(f'{h:02d} {nh:>3} {wr*100:>4.0f}%{mark}')
    body.append('─' * 14)
    body.append(f'累計 {wn}/{n}  {wn/n*100:.1f}%')
    streak = ''
    if not final:
        s = 0
        for _, w in reversed(tr):
            if w == 0: s += 1
            else: break
        streak = f'\n⚠️ 現在 {s} 連敗中' if s >= 3 else ''
    return f'{head}\n<pre>{chr(10).join(body)}</pre>{streak}\n{src}'

def update_daily_history(v2, v4, upto_date):
    """Upsert per-day totals (auto-backfills all dates present in log), prune old, save."""
    hist = {'v2': {}, 'v4': {}}
    try:
        with open(DAILY_JSON, encoding='utf-8') as f:
            loaded = json.load(f)
        for k in ('v2', 'v4'):
            hist[k].update(loaded.get(k, {}))
    except Exception:
        pass
    for key, rows in (('v2', v2), ('v4', v4)):
        agg = {}
        for ts, w in rows:
            if ts.date() > upto_date:
                continue  # exclude the (new) current day still in progress
            d = ts.date().isoformat()
            wn, n = agg.get(d, (0, 0))
            agg[d] = (wn + w, n + 1)
        for d, (wn, n) in agg.items():
            hist[key][d] = [wn, n]
    cutoff = (upto_date - timedelta(days=DAILY_KEEP_DAYS)).isoformat()
    for k in ('v2', 'v4'):
        hist[k] = {d: v for d, v in hist[k].items() if d >= cutoff}
    try:
        tmp = DAILY_JSON + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(hist, f)
        os.replace(tmp, DAILY_JSON)
    except Exception as e:
        print('HIST_SAVE_FAILED:', e)
    return hist

def build_weekly(hist, upto_date):
    days = [upto_date - timedelta(days=i) for i in range(WEEK_ROWS - 1, -1, -1)]
    body = ['日付    6パタ       10パタ', '─' * 24]
    tot = {'v2': [0, 0], 'v4': [0, 0]}
    for d in days:
        k = d.isoformat()
        cells = [f'{d.strftime("%-m/%-d"):>5}']
        for key in ('v2', 'v4'):
            wn, n = hist.get(key, {}).get(k, [0, 0])
            tot[key][0] += wn; tot[key][1] += n
            cells.append(f'{wn/n*100:>4.1f}%/{n:<4}' if n else '   —      ')
        body.append(' '.join(cells))
    body.append('─' * 24)
    cells = ['週計 ']
    for key in ('v2', 'v4'):
        wn, n = tot[key]
        cells.append(f'{wn/n*100:>4.1f}%/{n:<4}' if n else '   —      ')
    body.append(' '.join(cells))
    head = f'<b>📅 日別勝率 直近{WEEK_ROWS}日</b>  {upto_date.strftime("%-m/%-d")}確定 (毎日0時更新)'
    return f'{head}\n<pre>{chr(10).join(body)}</pre><i>※6パタ=forward追跡 / 10パタ=v4実署名</i>'

def send(token, chat_id, text):
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
        return r.status

def main():
    env = load_env(ENV)
    token = env.get('TELEGRAM_BOT_TOKEN', '')
    # 宛先はカンマ区切りで複数指定できる(2026-08-03: 勝率3を追加)
    _raw = (env.get('WINRATE_CHAT_ID')
            or env.get('DUAL_LINE_CHAT_ID') or env.get('TELEGRAM_CHAT_ID', ''))
    chat_ids = [c.strip() for c in str(_raw).split(',') if c.strip()]
    chat_id = chat_ids[0] if chat_ids else ''
    now = datetime.now()
    for i, a in enumerate(sys.argv):
        if a == '--now' and i + 1 < len(sys.argv):
            now = datetime.strptime(sys.argv[i + 1], '%Y-%m-%d %H:%M')
    v2, v4 = parse()
    midnight = (now.hour == 0)
    target_date = (now - timedelta(days=1)).date() if midnight else now.date()
    msgs = [
        build(v2, '📊 6パターン編 時刻別勝率', '<i>※6パターン forward追跡</i>', target_date, midnight),
        build(v4, '📘 10パターン編 時刻別勝率', '<i>※v4実署名(Telegram【10】と同じ)</i>', target_date, midnight),
    ]
    if midnight:
        hist = update_daily_history(v2, v4, target_date)
        msgs.append(build_weekly(hist, target_date))
    dry = '--dry' in sys.argv or not token or not chat_id
    for m in msgs:
        if dry:
            print(m + '\n')
        else:
            for _cid in chat_ids:
                try:
                    print('SENT', _cid, send(token, _cid, m))
                except Exception as e:
                    print('SEND_FAILED:', _cid, e)

if __name__ == '__main__':
    main()
