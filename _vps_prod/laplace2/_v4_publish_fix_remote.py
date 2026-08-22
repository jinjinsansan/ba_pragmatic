#!/usr/bin/env python3
"""Idempotent in-place fix for the VPS dual-line bot's _publish_v4_decision.

Root cause (2026-05-31): _publish_v4_decision resolved its POST base/key from
BACOPY_API_URL/BACOPY_API_KEY, which on the VPS point at the LOCAL API
(127.0.0.1:8010). v4 decisions were therefore trapped on the VPS-local master and
never reached master.bafather.uk where bafather polls -> bafather (v4 mode) never
saw a v4 decision -> READY but never NOW -> zero v4 bets.

Fix: make _publish_v4_decision resolve base/key remote-first, exactly like the v3
path (_publish_live_decision) and the already-correct _settle_v4_decision.

Idempotent: if the v4 publish block already references BACOPY_REMOTE_API_URL it is
left untouched. Only edits inside the BACOPY_V4_PUBLISH_FN block; v3 path untouched.
"""
from __future__ import annotations
import sys, time, py_compile
from pathlib import Path

TARGET = Path("/opt/laplace2/dual_line_pragmatic_bot.py")
MARK_FN = "# === BACOPY_V4_PUBLISH_FN ==="

OLD_BASE = '            base = (_os.getenv("BACOPY_API_URL", "") or "").strip().rstrip("/")'
OLD_KEY  = '            key = (_os.getenv("BACOPY_API_KEY", "") or "").strip()'
# Some injected copies may use `os` instead of `_os`.
OLD_BASE2 = '            base = (os.getenv("BACOPY_API_URL", "") or "").strip().rstrip("/")'
OLD_KEY2  = '            key = (os.getenv("BACOPY_API_KEY", "") or "").strip()'

NEW_BASE = ('            base = (_os.getenv("BACOPY_REMOTE_API_URL", "") or _os.getenv("BACOPY_API_URL", "") or "").strip().rstrip("/")\n'
            '            base = base or "https://master.bafather.uk"')
NEW_KEY  = '            key = (_os.getenv("BACOPY_REMOTE_API_KEY", "") or _os.getenv("LAPLACE_API_KEY", "") or _os.getenv("BACOPY_API_KEY", "") or "").strip()'


def main() -> int:
    if not TARGET.exists():
        print("ABORT: target missing", TARGET); return 1
    src = TARGET.read_text()
    if MARK_FN not in src:
        print("ABORT: v4 publish marker not found (Phase2 patch not applied?)"); return 1

    i = src.index(MARK_FN)
    # function block = marker .. next top-level method def after it
    j = src.find("\n    def ", i + len(MARK_FN))
    if j < 0:
        j = len(src)
    head, block, tail = src[:i], src[i:j], src[j:]

    if "BACOPY_REMOTE_API_URL" in block:
        print("NOOP: v4 publish already remote-first"); return 0

    fixed = block
    use_os = OLD_BASE not in fixed  # which variable spelling does this copy use?
    ob, ok = (OLD_BASE2, OLD_KEY2) if use_os else (OLD_BASE, OLD_KEY)
    nb = NEW_BASE if not use_os else NEW_BASE.replace("_os.getenv", "os.getenv")
    nk = NEW_KEY if not use_os else NEW_KEY.replace("_os.getenv", "os.getenv")

    if ob not in fixed or ok not in fixed:
        print("ABORT: expected base/key lines not found in v4 publish block")
        print("---block head---"); print(fixed[:600]); return 1
    fixed = fixed.replace(ob, nb).replace(ok, nk)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = TARGET.with_suffix(TARGET.suffix + f".bak_v4remote_{stamp}")
    bak.write_text(src)
    TARGET.write_text(head + fixed + tail)
    try:
        py_compile.compile(str(TARGET), doraise=True)
    except Exception as e:
        TARGET.write_text(src)  # rollback
        print("ABORT: py_compile failed, rolled back:", e); return 1
    print("FIXED v4 publish -> remote. backup:", bak.name)
    print("VERIFY:")
    k = head + fixed + tail
    bi = k.index(MARK_FN)
    bj = k.find("\n    def ", bi + 1)
    for ln in k[bi:bj].splitlines():
        if "getenv" in ln and ("base" in ln or "key" in ln) or "master.bafather" in ln:
            print("   " + ln.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
