"""Pragmatic BET WS sniff tool (operator-assisted).

Goal:
  Capture the *outgoing* WebSocket payload(s) emitted when you place a $1 bet
  manually in the Pragmatic baccarat table UI.

Usage (Windows PowerShell):
  python sniff_pragmatic_bet_ws.py "speed baccarat 1" --seconds 240

Then:
  1) Wait until the table is fully loaded.
  2) During a betting phase, manually place exactly ONE $1 bet (Player or Banker).
  3) Do not place a second bet.
  4) Wait until the script ends and saves ws_messages.jsonl.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BA_ROOT = Path(__file__).parent.parent / "ba"
sys.path.insert(0, str(BA_ROOT))

from camoufox.sync_api import Camoufox  # type: ignore

LOBBY_URL = "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat"


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _atomic_write_jsonl(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def find_game_frame(page, attempts: int = 30):
    for _ in range(attempts):
        for f in page.frames:
            if "qpidreoxcc.net" in f.url or "pragmaticplaylive" in f.url:
                return f
        page.wait_for_timeout(1000)
    return None


def find_shell_app_frame(page, attempts: int = 30):
    for _ in range(attempts):
        for f in page.frames:
            if "apps/lobby" in f.url or f.name == "shell-app":
                return f
        page.wait_for_timeout(1000)
    return None


def dump_frames(out_dir: Path, page, stage: str) -> None:
    frames = []
    for f in page.frames:
        frames.append({"name": f.name, "url": f.url, "is_main": f == page.main_frame})
    (out_dir / f"{stage}_frames.json").write_text(json.dumps(frames, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / f"{stage}_url.txt").write_text(page.url, encoding="utf-8")
    try:
        page.screenshot(path=str(out_dir / f"{stage}.png"), full_page=False)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("table_substr", nargs="?", default="speed baccarat 1")
    ap.add_argument("--seconds", type=int, default=240)
    ap.add_argument("--auto-click-wait-sec", type=int, default=60)
    ap.add_argument("--no-auto-click", action="store_true", help="Do not auto-click table; operate manually.")
    ap.add_argument(
        "--flush-every-sec",
        type=int,
        default=5,
        help="Write incremental ws_messages.partial.jsonl periodically to avoid data loss on interruption.",
    )
    ap.add_argument("--profile-dir", default=str(Path(__file__).parent / "auth_state" / "camoufox_profile"))
    ap.add_argument(
        "--cookies-file",
        default="",
        help="Optional stake cookies JSON to restore into the browser context (useful with a fresh profile-dir).",
    )
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args(argv)

    table_substr = str(args.table_substr or "").lower().strip()
    out_dir = Path(__file__).parent / "pragmatic_bet_sniffs" / f"{_now_id()}_{table_substr.replace(' ', '_')}"
    out_dir.mkdir(parents=True, exist_ok=True)

    ws_msgs: list[dict] = []
    partial_path = out_dir / "ws_messages.partial.jsonl"
    last_flushed = 0

    def on_ws(ws):
        url = ws.url

        def _add(dir_: str, frame_data) -> None:
            try:
                p = frame_data.payload if hasattr(frame_data, "payload") else frame_data
                if isinstance(p, bytes):
                    p = p.decode("utf-8", errors="replace")
                ws_msgs.append(
                    {"ts": datetime.now().isoformat(), "url": url, "dir": dir_, "payload": str(p)[:6000]}
                )
            except Exception:
                pass

        ws.on("framereceived", lambda fd: _add("recv", fd))
        ws.on("framesent", lambda fd: _add("send", fd))

    print(f"[sniff] table_substr='{table_substr}' seconds={args.seconds} out={out_dir}")
    print("[sniff] IMPORTANT: place exactly ONE $1 bet manually during betting phase, then hands off.")
    print("[sniff] NOTE: ws_messages.jsonl may contain session tokens; do NOT share it. Share only redacted analyzer output.")
    print("[sniff] NOTE: do not run Pragmatic Watcher concurrently (duplicate sessions can get you kicked).")

    try:
        with Camoufox(
            headless=bool(args.headless),
            persistent_context=True,
            user_data_dir=str(Path(args.profile_dir)),
        ) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("websocket", on_ws)

            if args.cookies_file:
                try:
                    cookies = json.loads(Path(args.cookies_file).read_text(encoding="utf-8"))
                    if isinstance(cookies, list) and cookies:
                        ctx.add_cookies(cookies)
                        print(f"[sniff] restored cookies: {len(cookies)} from {args.cookies_file}")
                except Exception as e:
                    print(f"[sniff] cookie restore failed: {e}")

            print("[Stage 1] goto stake pragmatic lobby ...")
            page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(10_000)
            dump_frames(out_dir, page, "s1_lobby")

            print("[Stage 2] wait pragmatic shell ...")
            gf = find_game_frame(page)
            if not gf:
                print("[FAIL] pragmatic shell not found")
                dump_frames(out_dir, page, "s2_fail")
                return 1

            print("[Stage 3] find internal lobby (shell-app) ...")
            shell = find_shell_app_frame(page)
            if not shell:
                print("[FAIL] shell-app not found")
                dump_frames(out_dir, page, "s3_fail")
                return 1

            clicked = False
            if not args.no_auto_click:
                print(
                    f"[Stage 4] wait (<= {args.auto_click_wait_sec}s) for table '{table_substr}' to render, then click ..."
                )
                deadline = time.time() + float(max(args.auto_click_wait_sec, 1))
                while time.time() < deadline and not clicked:
                    try:
                        # shell-app can be present while still showing a loading screen; poll until text appears.
                        loc = shell.get_by_text(table_substr, exact=False).first
                        if loc.count() > 0:
                            loc.scroll_into_view_if_needed(timeout=3000)
                            loc.click(timeout=3000)
                            clicked = True
                            break
                    except Exception:
                        # frame might have reloaded/detached; reacquire
                        new_shell = find_shell_app_frame(page, attempts=2)
                        if new_shell:
                            shell = new_shell

                    # fallback: if any tiles/cards appear, try clicking the first one
                    if not clicked:
                        for sel in ['div[class*="tile"]', 'div[class*="card"]', 'button', '[role="button"]']:
                            try:
                                if shell.locator(sel).count() > 0:
                                    shell.locator(sel).first.click(timeout=2000)
                                    clicked = True
                                    break
                            except Exception:
                                continue

                    if not clicked:
                        page.wait_for_timeout(1000)

            if not clicked:
                print("[WARN] auto-click did not succeed. Continue in MANUAL mode.")
                print("       1) Wait for table cards to render")
                print("       2) Click 'Speed Baccarat 1' manually")
                print("       3) During betting phase, place exactly ONE $1 bet on PLAYER")
                dump_frames(out_dir, page, "s4_manual_needed")
            else:
                page.wait_for_timeout(12_000)
                dump_frames(out_dir, page, "s4_after_click")

            print("[Stage 5] capturing...")
            t0 = time.time()
            last_report = 0.0
            last_flush = 0.0
            flush_every = float(max(args.flush_every_sec, 1))

            while time.time() - t0 < float(args.seconds):
                page.wait_for_timeout(500)
                elapsed = time.time() - t0

                if elapsed - last_flush >= flush_every:
                    last_flush = elapsed
                    nonlocal_last = len(ws_msgs)
                    if nonlocal_last > last_flushed:
                        _append_jsonl(partial_path, ws_msgs[last_flushed:nonlocal_last])
                        last_flushed = nonlocal_last

                if elapsed - last_report >= 5:
                    last_report = elapsed
                    sent_pp = sum(
                        1
                        for m in ws_msgs
                        if m.get("dir") == "send"
                        and any(h in str(m.get("url", "")) for h in ("pragmaticplaylive.net", "qpidreoxcc.net"))
                    )
                    sent_stake = sum(
                        1
                        for m in ws_msgs
                        if m.get("dir") == "send" and "stake.com/_api/websockets" in str(m.get("url", ""))
                    )
                    print(
                        f"  t={int(elapsed)}s  ws_total={len(ws_msgs)}  ws_send_pp={sent_pp}  ws_send_stake={sent_stake}",
                        flush=True,
                    )

            # final flush
            if len(ws_msgs) > last_flushed:
                _append_jsonl(partial_path, ws_msgs[last_flushed:])
                last_flushed = len(ws_msgs)

            _atomic_write_jsonl(out_dir / "ws_messages.jsonl", ws_msgs)
            dump_frames(out_dir, page, "s5_end")
    except KeyboardInterrupt:
        print("[sniff] KeyboardInterrupt: saving partial logs...", flush=True)
    finally:
        # Ensure we have *something* even if interrupted mid-run.
        try:
            if len(ws_msgs) > last_flushed:
                _append_jsonl(partial_path, ws_msgs[last_flushed:])
                last_flushed = len(ws_msgs)
        except Exception:
            pass
        try:
            if ws_msgs:
                _atomic_write_jsonl(out_dir / "ws_messages.jsonl", ws_msgs)
        except Exception:
            pass

    # Quick summary for operator
    sends = [m for m in ws_msgs if m.get("dir") == "send"]
    (out_dir / "send_pragmatic_samples.txt").write_text(
        "\n\n".join(f"{m.get('url')}\n{m.get('payload')}" for m in sends[:60]),
        encoding="utf-8",
    )
    print(f"[sniff] done. ws_total={len(ws_msgs)} ws_send_total={len(sends)} out={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
