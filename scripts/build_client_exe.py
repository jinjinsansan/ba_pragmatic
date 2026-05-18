"""LAPLACE Client .exe Packager (PyInstaller frontend)

Runs the L.4 distribution builder first to produce a sanitised
`dist_client/`, then invokes PyInstaller to bundle it into a
Windows executable suitable for end-user distribution.

Design decisions:

  * One-dir mode (`--onedir`). Easier to debug than `--onefile`, and
    playwright/camoufox behave better when their native browser
    binaries live alongside the .exe rather than being extracted on
    every launch.

  * Static entry point: agent_api.py. All other client modules are
    lazy-imported from inside agent_api, so we declare them as
    --hidden-import to make PyInstaller pull them in.

  * Runtime assets (browser binaries for playwright/camoufox) are
    NOT bundled. The end user runs `camoufox fetch` once on first
    install to download them. Bundling would blow the output to
    ~1.5 GB per copy.

  * Optional per-user watermarking: pass --user-id to run the L.7
    fingerprint injection inside the bundled dist_client before
    PyInstaller runs. This way the fingerprint lives inside the
    final .exe's embedded Python bytecode.

Usage:
    # Unbranded build
    python scripts/build_client_exe.py

    # Per-user branded build
    python scripts/build_client_exe.py --user-id alice --channel beta

    # Clean PyInstaller caches first
    python scripts/build_client_exe.py --clean

    # Skip dist rebuild (reuse existing dist_client/)
    python scripts/build_client_exe.py --skip-dist-build
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Modules agent_api.py lazy-imports at runtime. PyInstaller's static
# analysis never sees these, so we list them explicitly.
HIDDEN_IMPORTS = [
    # Core client modules
    "scraper",
    "executor",
    "laplace_client",
    "humanizer",
    "notify",
    "game_ws",
    "config",
    "telegram_auth",
    "login_step1",
    # dual-line modules (bacopy_engine dual-line subcommand)
    "dual_line_pragmatic_bot",
    "dual_line_match",
    "dual_line_live_executor",
    "collector_pragmatic",
    # Third-party runtime deps
    "numpy",
    "camoufox.sync_api",
    "camoufox",
    "playwright.sync_api",
    "playwright._impl",
    "playwright._impl._api_types",
    "requests",
    "dotenv",
    # stdlib submodules that PyInstaller sometimes misses
    "encodings.idna",
    "logging.handlers",
]

# Runtime packages whose entire contents we want bundled. `--collect-all`
# copies the package dir, data files, and dynamic deps together.
COLLECT_ALL = [
    "camoufox",
    "playwright",
    # Data packages camoufox reads at runtime via relative paths
    # (PyInstaller cannot discover these via static import analysis).
    "apify_fingerprint_datapoints",
    "browserforge",
    "language_tags",
]


def run_dist_build(
    repo_root: Path,
    user_id: Optional[str],
    channel: str,
    verbose: bool,
) -> int:
    """Delegate to build_client_dist.py so the L.4 audits + L.7 fingerprint
    are applied before we hand the tree to PyInstaller."""
    cmd = [
        sys.executable,
        "-X",
        "utf8",
        str(repo_root / "scripts" / "build_client_dist.py"),
    ]
    if user_id:
        cmd.extend(["--user-id", user_id, "--channel", channel])
    if verbose:
        cmd.append("--verbose")
    print("[dist-build]", " ".join(cmd))
    return subprocess.call(cmd)


def run_pyinstaller(
    repo_root: Path,
    dist_dir: Path,
    output_name: str,
    clean: bool,
) -> int:
    """Invoke PyInstaller against dist_client/agent_api.py."""
    builds_dir = repo_root / "builds"
    builds_dir.mkdir(exist_ok=True)
    workpath = builds_dir / "_pyinstaller_work"
    specpath = builds_dir / "_pyinstaller_spec"
    distpath = builds_dir / "exe"
    workpath.mkdir(parents=True, exist_ok=True)
    specpath.mkdir(parents=True, exist_ok=True)
    distpath.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--name",
        output_name,
        "--distpath",
        str(distpath),
        "--workpath",
        str(workpath),
        "--specpath",
        str(specpath),
        # Include every .py file from dist_client as a data source so
        # the lazy imports can find them.
        "--paths",
        str(dist_dir),
    ]
    if clean:
        cmd.append("--clean")
    for mod in HIDDEN_IMPORTS:
        cmd.extend(["--hidden-import", mod])
    for pkg in COLLECT_ALL:
        cmd.extend(["--collect-all", pkg])
    # Entry point
    cmd.append(str(dist_dir / "agent_api.py"))

    print("[pyinstaller] running (this may take 1-5 minutes)...")
    start = time.time()
    rc = subprocess.call(cmd)
    elapsed = time.time() - start
    print(f"[pyinstaller] finished in {elapsed:.1f}s (exit {rc})")
    return rc


def smoke_test_exe(exe_path: Path) -> bool:
    """Launch the bundled .exe with --help-ish invocation to verify it
    actually boots Python and our module graph. We spawn it with a bogus
    stdin so agent_api's IPC loop exits quickly."""
    if not exe_path.exists():
        print(f"[smoke] missing: {exe_path}")
        return False
    print(f"[smoke] spawning {exe_path.name} ...")
    proc = subprocess.Popen(
        [str(exe_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # NOTE:
        # The engine runs an infinite loop waiting for IPC messages from Electron,
        # so a successful boot may *not* exit on its own. We just need to ensure
        # it launches without immediate ImportError/DLL failures.
        out, err = proc.communicate(input=b"\n", timeout=30)
    except subprocess.TimeoutExpired:
        # Consider this a PASS: the process stayed alive long enough to be running.
        proc.kill()
        out, err = proc.communicate()
        print("[smoke] TIMEOUT -- process stayed alive (treated as success)")
        return True
    rc = proc.returncode
    print(f"[smoke] exit={rc}")
    if out:
        print(f"[smoke] stdout: {out[:500].decode('utf-8', 'replace')}")
    if err:
        print(f"[smoke] stderr: {err[:500].decode('utf-8', 'replace')}")
    # Any clean exit (even non-zero, as long as it launched Python and
    # loaded agent_api) is considered success. Crash-on-startup (ImportError,
    # DLL missing) returns distinctive errors.
    if err and b"ModuleNotFoundError" in err:
        print("[smoke] FAIL: hidden import missing")
        return False
    if err and b"ImportError" in err:
        print("[smoke] FAIL: import error")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Package LAPLACE client as a Windows .exe via PyInstaller"
    )
    parser.add_argument("--user-id", default=None, help="Embed fingerprint")
    parser.add_argument("--channel", default="beta")
    parser.add_argument("--clean", action="store_true", help="Clean PyInstaller caches")
    parser.add_argument(
        "--skip-dist-build",
        action="store_true",
        help="Reuse existing dist_client/ without rebuilding",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--no-smoke-test",
        action="store_true",
        help="Skip launching the built .exe afterwards",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    dist_dir = repo_root / "dist_client"

    if not args.skip_dist_build:
        rc = run_dist_build(repo_root, args.user_id, args.channel, args.verbose)
        if rc != 0:
            print(f"[dist-build] FAILED (exit {rc})")
            return rc
    else:
        # Staleness guard: if the user passed --skip-dist-build but the
        # root source is newer than the sanitised copy, we would silently
        # freeze stale bytecode into the bundled Engine. This has burned
        # us once already (Japanese cp932 hotfix rebuild didn't take);
        # refuse to proceed unless the user really knows what they're
        # doing.
        stale: list[str] = []
        check_files = ("agent_api.py", "scraper.py", "laplace_client.py")
        for name in check_files:
            root_f = repo_root / name
            dist_f = dist_dir / name
            if not root_f.exists() or not dist_f.exists():
                continue
            if root_f.stat().st_mtime > dist_f.stat().st_mtime + 1:
                stale.append(name)
        if stale:
            print("")
            print("=" * 72)
            print("[fatal] dist_client/ is STALE relative to repo root:")
            for s in stale:
                print(f"        - {s}")
            print(
                "\nPyInstaller would compile outdated source. Drop "
                "--skip-dist-build\nor run 'python scripts/build_client_dist.py' "
                "first."
            )
            print("=" * 72)
            return 3

    if not dist_dir.exists():
        print(f"[fatal] dist_client/ does not exist at {dist_dir}")
        return 2

    # Compose output name
    if args.user_id:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
        output_name = f"laplace_client_{args.user_id}_{ts}"
    else:
        output_name = "laplace_client_unbranded"

    rc = run_pyinstaller(repo_root, dist_dir, output_name, clean=args.clean)
    if rc != 0:
        print(f"[pyinstaller] FAILED (exit {rc})")
        return rc

    exe_path = (
        repo_root / "builds" / "exe" / output_name / f"{output_name}.exe"
    )
    print("")
    print("=" * 72)
    print("BUILD ARTIFACTS")
    print("=" * 72)
    print(f"EXE: {exe_path}")
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"Size: {size_mb:.1f} MB")
    bundle_dir = exe_path.parent
    if bundle_dir.exists():
        total = sum(p.stat().st_size for p in bundle_dir.rglob("*") if p.is_file())
        print(f"Bundle size (total): {total / (1024*1024):.1f} MB")
    print("")

    if args.no_smoke_test:
        print("[smoke] skipped (--no-smoke-test)")
        return 0

    ok = smoke_test_exe(exe_path)
    print("=" * 72)
    if ok:
        print("BUILD OK -- .exe packaged and smoke-tested")
    else:
        print("BUILD FAILED -- .exe did not pass smoke test")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
