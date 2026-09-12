"""stake_origin.py — Stake のミラードメインを管理し、451 で焼かれたら切り替える。

背景 (2026-09-12):
  総務省 → Cloudflare の通知で `stake.com` は日本向け HTTP 451 になった。
  Stake 公式のミラー一覧 https://playstake.io には 121本あるが、これらは
  「CDN を迂回している」のではなく **まだ通知対象リストに載っていないだけ**。
  通知はドメイン単位なので 1本ずつ焼かれる。実際、連番には歯抜けがあり
  (stake1022 → 1034 等)、Stake 自身も stake3000.com → 301 → stake3015.com と
  古いミラーを転送している = ローテーションは通常運転。

  したがってオリジンを exe に焼くと、焼かれる度に全インストーラを作り直す
  羽目になる。マスターが現在のオリジンを配り、受け子はそれに従う形にする。

★このモジュールが載る VPS は日本にあるため、**受け子と同じ視点で 451 を判定できる**。
  海外VPSから見ると 451 にならないので、この判定は日本国内から行う必要がある。

判定の区別 (2026-09-12 実測):
  451 + 本文 "Legal Reasons"      → 焼かれた。使えない
  403 + 本文 "Just a moment"      → Cloudflare の JSチャレンジ。**生きている**
                                     (実ブラウザなら通る。curl が弾かれるだけ)

CLI:
    python stake_origin.py show      現在の状態を表示
    python stake_origin.py check     現在のオリジンを判定し、焼けていたら切り替える
    python stake_origin.py refresh   playstake.io から候補一覧を取り直す
    python stake_origin.py set <url> 手動で指定する
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_ORIGIN = "https://stake.com"
PLAYSTAKE_URL = "https://playstake.io"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

_LOCK = threading.RLock()


def state_path() -> Path:
    p = os.getenv("BACOPY_ORIGIN_STATE_PATH", "").strip()
    if p:
        return Path(p)
    return Path(__file__).parent / "data" / "stake_origin.json"


def _blank_state() -> dict[str, Any]:
    return {
        "origin": (os.getenv("BACOPY_STAKE_ORIGIN", "") or DEFAULT_ORIGIN).strip().rstrip("/"),
        "candidates": [],
        "status": {},          # domain -> {"state": ok|blocked|error, "code": int, "at": iso}
        "updated_at": "",
        "last_check_at": "",
    }


def load_state() -> dict[str, Any]:
    with _LOCK:
        p = state_path()
        if not p.exists():
            return _blank_state()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return _blank_state()
        base = _blank_state()
        base.update({k: v for k, v in d.items() if k in base})
        return base


def save_state(st: dict[str, Any]) -> None:
    with _LOCK:
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        st["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)


def current_origin() -> str:
    """受け子に配る現在のオリジン。env が指定されていればそれを最優先する。"""
    forced = (os.getenv("BACOPY_STAKE_ORIGIN_FORCE", "") or "").strip().rstrip("/")
    if forced:
        return forced
    st = load_state()
    return (st.get("origin") or DEFAULT_ORIGIN).rstrip("/")


def _normalize(domain_or_url: str) -> str:
    d = (domain_or_url or "").strip().rstrip("/")
    if not d:
        return ""
    if "://" not in d:
        d = "https://" + d
    return d


def probe(origin: str, timeout: float = 12.0) -> dict[str, Any]:
    """1本のオリジンを判定する。

    戻り値の state:
      "ok"      … 使える (200/403/301 等。403 は Cloudflare の bot判定で正常)
      "blocked" … 451 または本文に法的遮断の文言 = 焼かれた
      "error"   … 名前解決不可・接続不可など
    """
    url = _normalize(origin) + "/"
    req = Request(url, headers={"User-Agent": _UA}, method="GET")
    code = 0
    body = ""
    try:
        with urlopen(req, timeout=timeout) as r:
            code = r.getcode() or 0
            body = r.read(4096).decode("utf-8", errors="replace")
    except HTTPError as e:
        code = e.code
        try:
            body = e.read(4096).decode("utf-8", errors="replace")
        except Exception:
            body = ""
    except (URLError, OSError, TimeoutError) as e:
        return {"state": "error", "code": 0, "detail": str(e)[:120]}

    low = body.lower()
    if code == 451 or "legal reasons" in low or "unavailable for legal" in low:
        return {"state": "blocked", "code": code, "detail": "451 / legal reasons"}
    # 403 + "just a moment" は Cloudflare の JSチャレンジ = 実ブラウザなら通る
    if code == 403 and ("just a moment" in low or "enable javascript" in low or "cf-" in low):
        return {"state": "ok", "code": code, "detail": "cloudflare js challenge (browser passes)"}
    if 200 <= code < 400 or code == 403:
        return {"state": "ok", "code": code, "detail": ""}
    return {"state": "error", "code": code, "detail": f"unexpected status {code}"}


def refresh_candidates(timeout: float = 20.0) -> list[str]:
    """playstake.io から公式ミラー一覧を取り直す。

    ★Stake 自身が更新してくれるので、こちらは取りに行くだけでよい。
    """
    req = Request(PLAYSTAKE_URL, headers={"User-Agent": _UA})
    with urlopen(req, timeout=timeout) as r:
        html = r.read().decode("utf-8", errors="replace")
    # ★小文字化してから重複排除する。順序を逆にすると "Stake.bet" と "stake.bet" が
    #   別物として残り、件数が倍近くに膨らむ (2026-09-12 に実際に踏んだ)。
    found = [d.lower() for d in re.findall(r"\bstake[0-9]*\.[a-z]{2,}\b", html, re.I)]
    doms = sorted({d for d in found if d != "stake.com"})
    st = load_state()
    st["candidates"] = doms
    save_state(st)
    return doms


def check_and_rotate(max_probe: int = 8) -> dict[str, Any]:
    """現在のオリジンを判定し、焼けていたら候補へ切り替える。

    戻り値: {"changed": bool, "origin": str, "previous": str, "reason": str}
    """
    st = load_state()
    cur = (st.get("origin") or DEFAULT_ORIGIN).rstrip("/")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    st["last_check_at"] = now

    res = probe(cur)
    st.setdefault("status", {})[cur] = {**res, "at": now}
    if res["state"] == "ok":
        save_state(st)
        return {"changed": False, "origin": cur, "previous": cur, "reason": f"current ok ({res['code']})"}

    # 焼けている/到達不能 → 候補を順に試す
    cands = [_normalize(d) for d in (st.get("candidates") or [])]
    tried = 0
    for cand in cands:
        if cand.rstrip("/") == cur:
            continue
        if tried >= max_probe:
            break
        tried += 1
        r = probe(cand)
        st["status"][cand] = {**r, "at": now}
        if r["state"] == "ok":
            st["origin"] = cand.rstrip("/")
            save_state(st)
            return {
                "changed": True,
                "origin": st["origin"],
                "previous": cur,
                "reason": f"{cur} is {res['state']} ({res['code']}); switched after {tried} probes",
            }

    save_state(st)
    return {
        "changed": False,
        "origin": cur,
        "previous": cur,
        "reason": f"{cur} is {res['state']} but no healthy candidate found in {tried} probes",
    }


def public_state() -> dict[str, Any]:
    """/api/origin で受け子に返す形。"""
    st = load_state()
    origin = current_origin()
    # 健全と分かっている候補を優先して並べる (受け子側のフェイルオーバー順)
    status = st.get("status") or {}
    cands = [_normalize(d).rstrip("/") for d in (st.get("candidates") or [])]
    ok = [c for c in cands if (status.get(c) or {}).get("state") == "ok" and c != origin]
    unknown = [c for c in cands if c not in ok and c != origin and c not in status]
    return {
        "origin": origin,
        "candidates": [origin] + ok + unknown[:20],
        "updated_at": st.get("updated_at", ""),
        "last_check_at": st.get("last_check_at", ""),
    }


def _main(argv: list[str]) -> int:
    cmd = (argv[1] if len(argv) > 1 else "show").lower()
    if cmd == "show":
        print(json.dumps(public_state(), ensure_ascii=False, indent=2))
        return 0
    if cmd == "refresh":
        doms = refresh_candidates()
        print(f"candidates: {len(doms)}")
        print(", ".join(doms[:12]) + (" ..." if len(doms) > 12 else ""))
        return 0
    if cmd == "check":
        r = check_and_rotate()
        print(json.dumps(r, ensure_ascii=False))
        return 0 if not r["changed"] else 10  # 切替時は 10 (cron から検知しやすく)
    if cmd == "set":
        if len(argv) < 3:
            print("usage: stake_origin.py set <origin>", file=sys.stderr)
            return 2
        st = load_state()
        st["origin"] = _normalize(argv[2]).rstrip("/")
        save_state(st)
        print(st["origin"])
        return 0
    if cmd == "probe":
        if len(argv) < 3:
            print("usage: stake_origin.py probe <origin>", file=sys.stderr)
            return 2
        print(json.dumps(probe(argv[2]), ensure_ascii=False))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
