from __future__ import annotations

import argparse
import sys
from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="bacopy_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("executor-pragmatic", help="Pragmatic live WS executor")
    sub.add_parser("watch-pragmatic", help="Pragmatic watcher (snapshot)")
    sub.add_parser("watch-evolution", help="Evolution watcher (snapshot)")

    ns, rest = ap.parse_known_args(argv)

    if ns.cmd == "executor-pragmatic":
        from bacopy_executor_pragmatic_ws_live import main as _m

        return int(_m(rest) or 0)

    if ns.cmd == "watch-pragmatic":
        from bacopy_watch_pragmatic import main as _m

        return int(_m(rest) or 0)

    if ns.cmd == "watch-evolution":
        from bacopy_watch_evolution import main as _m

        return int(_m(rest) or 0)

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
