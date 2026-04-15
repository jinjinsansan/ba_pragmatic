"""LAPLACE Beta Release Builder

One-command pipeline to produce a ready-to-distribute zip file for a
specific beta user. Orchestrates the entire L.4-L.7 + PyInstaller +
Electron + camoufox bundling flow.

Usage:
    python scripts/build_beta_release.py --user-id alice
    python scripts/build_beta_release.py --user-id bob --live  # DRY_RUN=0
    python scripts/build_beta_release.py --user-id alice --skip-engine  # reuse existing Engine

Steps (all automated):
    1. build_client_dist.py --user-id <X> --issue-key --channel beta
       => Sanitised dist_client/ with fingerprint + API key issued
    2. build_client_exe.py --user-id <X>
       => Per-user branded Engine .exe in builds/exe/
    3. electron-builder --dir
       => gui/dist/win-unpacked/LAPLACE.exe with Engine in resources/
    4. Copy camoufox browser cache into resources/engine/camoufox_cache/
    5. Write per-user .env (from .env.template with API key + username)
    6. Zip everything => builds/LAPLACE_<user>_<date>.zip
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _run(cmd: list[str], label: str, timeout: int = 900) -> int:
    print(f"\n{'='*72}")
    print(f"[{label}] {' '.join(cmd)}")
    print(f"{'='*72}")
    rc = subprocess.call(cmd, timeout=timeout)
    if rc != 0:
        print(f"[{label}] FAILED (exit {rc})")
    return rc


def _find_camoufox_cache() -> Path | None:
    """Locate the camoufox browser cache on this build machine."""
    # Microsoft Store Python redirect
    redirect = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages" / \
        "PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0" / \
        "LocalCache" / "Local" / "camoufox" / "camoufox" / "Cache"
    if redirect.exists() and (redirect / "version.json").exists():
        return redirect
    # Standard path
    standard = Path(os.environ.get("LOCALAPPDATA", "")) / "camoufox" / "camoufox" / "Cache"
    if standard.exists() and (standard / "version.json").exists():
        return standard
    return None


def _read_build_registry(repo_root: Path, user_id: str) -> dict | None:
    """Read the most recent build registry entry for this user."""
    reg_path = repo_root / "scripts" / ".build_registry.json"
    if not reg_path.exists():
        return None
    try:
        with open(reg_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        for entry in reversed(entries):
            if entry.get("user_id") == user_id:
                return entry
    except Exception:
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Build LAPLACE beta release for a user")
    parser.add_argument("--user-id", required=True, help="Target user identifier")
    parser.add_argument("--channel", default="beta")
    parser.add_argument("--live", action="store_true", help="Set LAPLACE_FORCE_DRYRUN=0")
    parser.add_argument("--skip-engine", action="store_true",
                        help="Reuse existing Engine build (skip PyInstaller)")
    parser.add_argument("--skip-electron", action="store_true",
                        help="Reuse existing Electron build")
    parser.add_argument("--no-zip", action="store_true", help="Skip zip creation")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    user_id = args.user_id
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    zip_name = f"LAPLACE_{user_id}_{ts}"

    print(f"Building beta release for user: {user_id}")
    print(f"Channel: {args.channel}")
    print(f"Mode: {'LIVE' if args.live else 'DRY RUN'}")
    print()

    # ---- Step 1: Sanitised dist + fingerprint + API key ----
    step1_cmd = [
        sys.executable, "-X", "utf8",
        str(repo_root / "scripts" / "build_client_dist.py"),
        "--user-id", user_id,
        "--channel", args.channel,
        "--issue-key",
    ]
    rc = _run(step1_cmd, "Step 1: dist + fingerprint + key")
    if rc != 0:
        return rc

    # Read the issued key from build registry
    reg_entry = _read_build_registry(repo_root, user_id)
    api_key = ""
    if reg_entry:
        api_key = reg_entry.get("issued_key", "") or ""
        print(f"[info] API key for {user_id}: {api_key[:20]}..." if api_key else "[warn] No API key found in registry")

    # ---- Step 2: PyInstaller Engine ----
    if not args.skip_engine:
        step2_cmd = [
            sys.executable, "-X", "utf8",
            str(repo_root / "scripts" / "build_client_exe.py"),
            "--user-id", user_id,
            "--channel", args.channel,
            "--no-smoke-test",
        ]
        rc = _run(step2_cmd, "Step 2: PyInstaller Engine")
        if rc != 0:
            return rc

    engine_name = f"laplace_client_{user_id}_{ts}"
    engine_dir = repo_root / "builds" / "exe" / engine_name
    if not engine_dir.exists():
        # Try unbranded fallback
        engine_dir = repo_root / "builds" / "exe" / f"laplace_client_{user_id}"
        if not engine_dir.exists():
            # Glob for any matching dir
            candidates = sorted(
                (repo_root / "builds" / "exe").glob(f"laplace_client_{user_id}*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                engine_dir = candidates[0]
            else:
                print(f"[fatal] Engine build not found for {user_id}")
                return 2
    print(f"[info] Engine dir: {engine_dir}")

    # ---- Step 3: Electron packaging ----
    # Temporarily update package.json to point at the user-specific Engine
    gui_dir = repo_root / "gui"
    pkg_json = gui_dir / "package.json"
    pkg_backup = gui_dir / "package.json.bak"

    with open(pkg_json, "r", encoding="utf-8") as f:
        pkg_data = json.load(f)

    # Rewrite extraResources to point at this user's Engine
    original_extra = pkg_data.get("build", {}).get("extraResources", [])
    new_extra = [
        {"from": str(engine_dir).replace("\\", "/"), "to": "engine"},
        {"from": "../.env.beta", "to": ".env"},
    ]
    pkg_data["build"]["extraResources"] = new_extra

    # Save backup and write modified
    shutil.copy2(pkg_json, pkg_backup)
    with open(pkg_json, "w", encoding="utf-8") as f:
        json.dump(pkg_data, f, indent=2, ensure_ascii=False)

    # Write per-user .env from template
    env_template = repo_root / ".env.template"
    env_beta = repo_root / ".env.beta"
    if env_template.exists():
        content = env_template.read_text(encoding="utf-8")
        content = content.replace("__API_KEY__", api_key)
        content = content.replace("__USERNAME__", user_id)
        if args.live:
            content = content.replace("LAPLACE_FORCE_DRYRUN=1", "LAPLACE_FORCE_DRYRUN=0")
        env_beta.write_text(content, encoding="utf-8")
        print(f"[info] .env.beta written for {user_id}")
    else:
        print("[warn] .env.template not found, skipping .env generation")

    if not args.skip_electron:
        # Clean old dist
        dist_dir = gui_dir / "dist"
        if dist_dir.exists():
            subprocess.run(["cmd", "/c", f"rmdir /s /q {dist_dir}"],
                           capture_output=True, timeout=120)

        electron_cmd = [
            "npx", "electron-builder", "--dir",
            "--config.win.signAndEditExecutable=false",
        ]
        env = os.environ.copy()
        env["CSC_IDENTITY_AUTO_DISCOVERY"] = "false"
        print(f"\n{'='*72}")
        print(f"[Step 3: Electron] {' '.join(electron_cmd)}")
        print(f"{'='*72}")
        rc = subprocess.call(electron_cmd, cwd=str(gui_dir), env=env, timeout=600)

        # Restore package.json
        shutil.move(str(pkg_backup), str(pkg_json))
        # Clean temp .env.beta
        env_beta.unlink(missing_ok=True)

        if rc != 0:
            print("[Step 3: Electron] FAILED")
            return rc
    else:
        shutil.move(str(pkg_backup), str(pkg_json))
        env_beta.unlink(missing_ok=True)

    unpacked = gui_dir / "dist" / "win-unpacked"
    if not unpacked.exists():
        print(f"[fatal] Electron output not found at {unpacked}")
        return 2

    # ---- Step 4: Bundle camoufox browser cache ----
    camo_cache = _find_camoufox_cache()
    camo_dest = unpacked / "resources" / "engine" / "camoufox_cache"
    if camo_cache:
        print(f"[Step 4] Copying camoufox browser cache ({camo_cache})")
        if camo_dest.exists():
            shutil.rmtree(camo_dest)
        shutil.copytree(str(camo_cache), str(camo_dest))
        camo_files = sum(1 for _ in camo_dest.rglob("*") if _.is_file())
        camo_size = sum(p.stat().st_size for p in camo_dest.rglob("*") if p.is_file())
        print(f"[Step 4] Copied {camo_files} files ({camo_size / (1024*1024):.0f} MB)")
    else:
        print("[Step 4] WARNING: camoufox browser cache not found on this machine")
        print("         User will need to run 'camoufox fetch' on first launch")

    # ---- Step 5: Write per-user .env into the bundle ----
    env_dest = unpacked / "resources" / ".env"
    if env_template.exists():
        content = env_template.read_text(encoding="utf-8")
        content = content.replace("__API_KEY__", api_key)
        content = content.replace("__USERNAME__", user_id)
        if args.live:
            content = content.replace("LAPLACE_FORCE_DRYRUN=1", "LAPLACE_FORCE_DRYRUN=0")
        env_dest.write_text(content, encoding="utf-8")
        print(f"[Step 5] .env written to {env_dest}")

    # ---- Step 6: Create zip ----
    if not args.no_zip:
        zip_dir = repo_root / "builds"
        zip_dir.mkdir(exist_ok=True)
        zip_path = zip_dir / zip_name
        print(f"\n[Step 6] Creating zip: {zip_path}.zip")
        shutil.make_archive(str(zip_path), "zip", str(unpacked.parent), unpacked.name)
        zip_file = zip_dir / f"{zip_name}.zip"
        if zip_file.exists():
            size_mb = zip_file.stat().st_size / (1024 * 1024)
            print(f"[Step 6] {zip_file} ({size_mb:.0f} MB)")

    # ---- Summary ----
    print()
    print("=" * 72)
    print("BETA RELEASE BUILD COMPLETE")
    print("=" * 72)
    print(f"User:        {user_id}")
    print(f"Channel:     {args.channel}")
    print(f"Mode:        {'LIVE' if args.live else 'DRY RUN'}")
    print(f"API Key:     {api_key[:20]}..." if api_key else "API Key:     (none)")
    print(f"Engine:      {engine_dir.name}")
    print(f"Electron:    {unpacked}")
    if camo_cache:
        print(f"Camoufox:    bundled ({camo_dest})")
    if not args.no_zip:
        print(f"ZIP:         {zip_file}")
    print(f"Build ID:    {reg_entry.get('build_id', 'N/A') if reg_entry else 'N/A'}")
    print()
    print("To distribute:")
    print(f"  1. Send {zip_file.name} to {user_id}")
    print(f"  2. User extracts the zip and double-clicks LAPLACE.exe")
    print(f"  3. User presses START and logs in to Stake.com manually")
    print(f"  4. LAPLACE handles the rest automatically")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
