"""LAPLACE Client Distribution Builder

Reads `.dist_excludes` at the repo root and produces a sanitised
`dist_client/` directory containing only the files that are safe to ship.

The build runs 3 automated checks plus (optionally) a watermark injection:

    1. Pattern matcher. Every path in the source tree is tested against
       `.dist_excludes` patterns (filename globs, directory prefixes,
       full path globs). Matches are excluded.

    2. Canary file check. A hard-coded list of sensitive filenames
       (marubatsu_strategy.py etc.) must NOT appear in the output.
       If any slip through, the build fails.

    3. Canary string check. The output is scanned for a small set of
       forbidden strings (SEQ = [, compute_score, _compute_regularity,
       PLAYERS_PRIMARY =, etc.). If any are found, the build fails.

    4. Import smoke test. laplace_client and agent_api are imported
       from the output directory with nothing else on PYTHONPATH. Any
       ImportError for a server-only module means we forgot to make
       something lazy.

    5. (L.7) Fingerprint injection. When --user-id is provided, the
       build stamps a unique build_id into laplace_client.py's
       _BUILD_INFO dict, writes .build_manifest.json, records the build
       in scripts/.build_registry.json, and optionally issues a per-user
       API key via the admin endpoint.

Usage:
    # Unbranded dev build
    python scripts/build_client_dist.py

    # Per-user branded build
    python scripts/build_client_dist.py --user-id alice --channel beta

    # Per-user build + auto-issue API key (requires LAPLACE_ADMIN_KEY env)
    python scripts/build_client_dist.py --user-id alice --issue-key

    # Custom output dir + verbose
    python scripts/build_client_dist.py --out custom_dir --verbose
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# =============================================================================
# Canaries — files and strings that MUST NOT appear in the output distribution
# =============================================================================

CANARY_FILES = [
    "marubatsu_strategy.py",
    "marubatsu_bet.py",
    "table_selector.py",
    "shoe.py",
    "strategy.py",
    "laplace_api.py",
    "laplace_bet_runner.py",
    "bot_manager.py",
]

CANARY_STRINGS = [
    # MaruBatsu core sequence (literal array start)
    "SEQ = [",
    # Regularity scoring
    "_compute_regularity",
    "def _compute_regularity",
    # Table selector scoring formula
    "def compute_score",
    "PLAYERS_PRIMARY =",
    "PLAYERS_RELAXED =",
    "DRAGON_LIMIT =",
    "EXCLUDE_TITLE_KEYWORDS",
    # MaruBatsu finalization
    "def finalize_set",
    "def calc_slashed",
    "def calc_next_unit_idx",
    # 1-2-3 strategy
    "class BetStrategy",
]

# Directories that are always pruned, even if not in .dist_excludes
HARD_PRUNE_DIRS = {
    ".git",
    ".claude",
    ".factory",
    ".pytest_cache",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "dist_client",
    "dist_client_test",
    "build",
}


# =============================================================================
# Manifest parser
# =============================================================================


@dataclass
class ExcludePattern:
    raw: str
    is_dir: bool
    has_path_sep: bool

    @classmethod
    def from_line(cls, line: str) -> "ExcludePattern":
        is_dir = line.endswith("/")
        clean = line.rstrip("/")
        return cls(raw=clean, is_dir=is_dir, has_path_sep="/" in clean)


def parse_manifest(path: Path) -> list[ExcludePattern]:
    patterns: list[ExcludePattern] = []
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(ExcludePattern.from_line(line))
    return patterns


# =============================================================================
# Matcher
# =============================================================================


def match_exclude(rel_path: str, patterns: list[ExcludePattern]) -> ExcludePattern | None:
    """Return the first matching ExcludePattern, or None.

    `rel_path` is POSIX-style (forward slashes), relative to the repo root.
    Directory paths should NOT end with a trailing slash.
    """
    basename = rel_path.rsplit("/", 1)[-1]
    dir_head = rel_path.split("/", 1)[0]
    for p in patterns:
        if p.is_dir:
            # Directory pattern. Supports both literal names and globs.
            if p.has_path_sep:
                # Full path directory, e.g. "gui/node_modules"
                prefix = p.raw + "/"
                if rel_path == p.raw or rel_path.startswith(prefix):
                    return p
            else:
                # Bare directory name, e.g. "data" or "*_dumps".
                # Match if ANY path component matches the glob.
                for part in rel_path.split("/")[:-1] or [dir_head]:
                    if fnmatch.fnmatch(part, p.raw):
                        return p
                # Also handle the case where rel_path IS the directory itself
                if fnmatch.fnmatch(dir_head, p.raw):
                    return p
            continue
        if p.has_path_sep:
            # Full path file pattern
            if fnmatch.fnmatch(rel_path, p.raw):
                return p
            continue
        # Simple basename pattern (exact or glob)
        if fnmatch.fnmatch(basename, p.raw):
            return p
    return None


# =============================================================================
# Builder
# =============================================================================


@dataclass
class BuildFingerprint:
    user_id: str
    build_id: str
    built_at: str
    channel: str
    key_prefix: Optional[str] = None  # set if --issue-key succeeded
    issued_key: Optional[str] = None  # full secret, returned ONCE

    def to_manifest_dict(self) -> dict:
        d = {
            "user_id": self.user_id,
            "build_id": self.build_id,
            "built_at": self.built_at,
            "channel": self.channel,
        }
        if self.key_prefix:
            d["api_key_prefix"] = self.key_prefix
        return d


@dataclass
class BuildReport:
    copied: list[str] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)  # (path, pattern)
    canary_files_found: list[str] = field(default_factory=list)
    canary_strings_found: list[tuple[str, str]] = field(default_factory=list)  # (file, string)
    import_errors: list[str] = field(default_factory=list)
    fingerprint: Optional[BuildFingerprint] = None
    fingerprint_errors: list[str] = field(default_factory=list)
    zip_path: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return (
            not self.canary_files_found
            and not self.canary_strings_found
            and not self.import_errors
            and not self.fingerprint_errors
        )


def _make_build_id(user_id: str) -> str:
    """Deterministic-ish short ID: sha256 over user + random salt + timestamp."""
    salt = secrets.token_hex(8)
    ts = datetime.now(tz=timezone.utc).isoformat()
    raw = f"{user_id}|{ts}|{salt}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


FINGERPRINT_MARKER_START = "# === BUILD_FINGERPRINT_START ==="
FINGERPRINT_MARKER_END = "# === BUILD_FINGERPRINT_END ==="


def _inject_fingerprint(file_path: Path, fp: BuildFingerprint) -> None:
    """Rewrite the _BUILD_INFO block between the fingerprint markers."""
    text = file_path.read_text(encoding="utf-8")
    if FINGERPRINT_MARKER_START not in text or FINGERPRINT_MARKER_END not in text:
        raise ValueError(
            f"{file_path.name}: missing fingerprint markers "
            f"({FINGERPRINT_MARKER_START} / {FINGERPRINT_MARKER_END})"
        )
    replacement = (
        FINGERPRINT_MARKER_START
        + "\n"
        + "# Injected by build_client_dist.py. Do not edit.\n"
        + "_BUILD_INFO: dict = {\n"
        + f'    "user_id": "{fp.user_id}",\n'
        + f'    "build_id": "{fp.build_id}",\n'
        + f'    "built_at": "{fp.built_at}",\n'
        + f'    "channel": "{fp.channel}",\n'
        + "}\n"
        + FINGERPRINT_MARKER_END
    )
    new_text = re.sub(
        re.escape(FINGERPRINT_MARKER_START)
        + r".*?"
        + re.escape(FINGERPRINT_MARKER_END),
        replacement,
        text,
        count=1,
        flags=re.DOTALL,
    )
    file_path.write_text(new_text, encoding="utf-8")


def _issue_key_via_admin(user_id: str, fp: BuildFingerprint) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Call /api/admin/keys. Returns (full_key, key_prefix, error)."""
    try:
        import requests
    except ImportError:
        return None, None, "requests library not available"
    url = os.getenv("LAPLACE_API_URL", "").rstrip("/")
    admin = os.getenv("LAPLACE_ADMIN_KEY", "").strip()
    if not url or not admin:
        return None, None, "LAPLACE_API_URL or LAPLACE_ADMIN_KEY not set"
    try:
        r = requests.post(
            f"{url}/api/admin/keys",
            headers={"Authorization": f"Bearer {admin}"},
            json={
                "user_id": user_id,
                "name": f"build {fp.build_id}",
                "rate_limit_per_hour": 3600,
                "ip_allowlist": [],
            },
            timeout=10,
        )
    except Exception as e:
        return None, None, f"admin API call failed: {e}"
    if r.status_code >= 400:
        return None, None, f"admin API error {r.status_code}: {r.text}"
    data = r.json()
    return data["key"], data["key"][: len("lpk_live_") + 8], None


def _write_build_manifest(out: Path, fp: BuildFingerprint, copied: list[str]) -> None:
    """Write .build_manifest.json summarising what's in the bundle."""
    sha = hashlib.sha256()
    for rel in sorted(copied):
        p = out / rel
        if p.is_file():
            sha.update(rel.encode("utf-8"))
            sha.update(b"\0")
            sha.update(p.read_bytes())
    manifest = {
        **fp.to_manifest_dict(),
        "file_count": len(copied),
        "content_sha256": sha.hexdigest(),
    }
    (out / ".build_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _append_build_registry(scripts_dir: Path, fp: BuildFingerprint) -> None:
    """Maintain a local log of every build ever made."""
    registry = scripts_dir / ".build_registry.json"
    data = {"builds": []}
    if registry.exists():
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
        except Exception:
            pass
    data.setdefault("builds", []).append(fp.to_manifest_dict())
    registry.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _make_zip(out: Path, fp: BuildFingerprint, src_root: Path) -> Path:
    builds_dir = src_root / "builds"
    builds_dir.mkdir(exist_ok=True)
    zip_name = f"laplace_client_{fp.user_id}_{fp.build_id}.zip"
    zip_path = builds_dir / zip_name
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in out.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(out))
    return zip_path


def build(
    src: Path,
    out: Path,
    verbose: bool = False,
    fp: Optional[BuildFingerprint] = None,
    issue_key: bool = False,
    make_zip: bool = False,
) -> BuildReport:
    patterns = parse_manifest(src / ".dist_excludes")
    report = BuildReport()
    report.fingerprint = fp

    # Clean output
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        # Prune directories in-place
        pruned: list[str] = []
        kept: list[str] = []
        for d in sorted(dirs):
            if d in HARD_PRUNE_DIRS:
                pruned.append(d)
                continue
            rel_d = (rel_root / d).as_posix()
            if rel_d.startswith("./"):
                rel_d = rel_d[2:]
            if rel_d == ".":
                rel_d = d
            m = match_exclude(rel_d, patterns)
            if m:
                pruned.append(d)
                report.excluded.append((rel_d + "/", m.raw + ("/" if m.is_dir else "")))
            else:
                kept.append(d)
        dirs[:] = kept
        if verbose:
            for d in pruned:
                print(f"  prune dir: {rel_root / d}")

        for f in files:
            rel_f = (rel_root / f).as_posix()
            if rel_f.startswith("./"):
                rel_f = rel_f[2:]
            m = match_exclude(rel_f, patterns)
            if m:
                report.excluded.append((rel_f, m.raw + ("/" if m.is_dir else "")))
                if verbose:
                    print(f"  exclude: {rel_f} <- {m.raw}")
                continue
            dst = out / rel_root / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(root) / f, dst)
            report.copied.append(rel_f)

    # --- Canary file check ---
    for canary in CANARY_FILES:
        for copied in report.copied:
            if copied == canary or copied.endswith("/" + canary):
                report.canary_files_found.append(copied)

    # --- Canary string check ---
    for copied in report.copied:
        if not copied.endswith(".py"):
            continue
        path = out / copied
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for needle in CANARY_STRINGS:
            if needle in text:
                report.canary_strings_found.append((copied, needle))

    # --- Fingerprint injection (L.7) ---
    if fp is not None:
        target = out / "laplace_client.py"
        if not target.exists():
            report.fingerprint_errors.append("laplace_client.py missing from output")
        else:
            try:
                _inject_fingerprint(target, fp)
            except Exception as e:
                report.fingerprint_errors.append(f"inject failed: {e}")

        if issue_key:
            full_key, prefix, err = _issue_key_via_admin(fp.user_id, fp)
            if err:
                report.fingerprint_errors.append(f"key issuance failed: {err}")
            else:
                fp.issued_key = full_key
                fp.key_prefix = prefix

        if not report.fingerprint_errors:
            _write_build_manifest(out, fp, report.copied)
            _append_build_registry(Path(__file__).resolve().parent, fp)

    # --- Import smoke test ---
    report.import_errors.extend(_import_smoke_test(out))

    # --- Optional zip packaging ---
    if make_zip and fp is not None and report.ok:
        try:
            report.zip_path = _make_zip(out, fp, Path(src))
        except Exception as e:
            report.fingerprint_errors.append(f"zip packaging failed: {e}")

    return report


def _import_smoke_test(out: Path) -> list[str]:
    """Run full import of every shipped Python module from a COPY of the
    output dir.

    We copy to a throwaway tempdir first because agent_api.py's module-level
    logging.FileHandler writes agent.log at import time, and the Python
    interpreter writes __pycache__/*.pyc, both of which would pollute the
    pristine build output.

    Testing every .py forces us to catch transitive deps that the earlier
    laplace_client + agent_api pair misses (agent_api imports scraper
    lazily, so scraper's own top-level imports are only validated here).
    """
    import tempfile

    errors: list[str] = []
    py_modules = sorted(
        p.stem for p in out.glob("*.py") if p.stem != "__init__"
    )
    with tempfile.TemporaryDirectory(prefix="laplace_smoke_") as tmp:
        probe_dir = Path(tmp) / "dist"
        shutil.copytree(out, probe_dir)
        probe_str = str(probe_dir).replace("\\", "\\\\")
        import_lines = "\n".join(
            f"import {m}; print('imported:', {m!r})" for m in py_modules
        )
        probe = (
            "import sys\n"
            f"sys.path.insert(0, '{probe_str}')\n"
            f"{import_lines}\n"
            "import laplace_client\n"
            "assert hasattr(laplace_client, 'RemoteLaplaceSession')\n"
            "assert hasattr(laplace_client, 'RemoteTableSelector')\n"
            "assert hasattr(laplace_client, 'ClientSetData')\n"
            "print('IMPORT_SMOKE_OK')\n"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            errors.append("Import smoke test timed out")
            return errors
        if result.returncode != 0 or "IMPORT_SMOKE_OK" not in result.stdout:
            errors.append(
                f"Import smoke test failed:\n"
                f"  returncode: {result.returncode}\n"
                f"  stdout: {result.stdout.strip()}\n"
                f"  stderr: {result.stderr.strip()}"
            )
    return errors


def print_summary(report: BuildReport, out: Path) -> None:
    print("")
    print("=" * 72)
    print("LAPLACE Client Distribution Build -- Summary")
    print("=" * 72)
    print(f"Output directory: {out}")
    print(f"Files copied:     {len(report.copied)}")
    print(f"Files excluded:   {len(report.excluded)}")
    print("")
    if report.canary_files_found:
        print(f"[FAIL] Canary files leaked into output:")
        for f in report.canary_files_found:
            print(f"  - {f}")
    else:
        print(f"[PASS] Canary file audit ({len(CANARY_FILES)} entries checked)")
    print("")
    if report.canary_strings_found:
        print(f"[FAIL] Canary strings leaked into output:")
        for f, s in report.canary_strings_found[:20]:
            print(f"  - {f}: {s!r}")
        if len(report.canary_strings_found) > 20:
            print(f"  ... and {len(report.canary_strings_found) - 20} more")
    else:
        print(f"[PASS] Canary string audit ({len(CANARY_STRINGS)} entries checked)")
    print("")
    if report.import_errors:
        print("[FAIL] Import smoke test:")
        for e in report.import_errors:
            print(f"  {e}")
    else:
        print("[PASS] Import smoke test (laplace_client + agent_api)")
    print("")
    if report.fingerprint is not None:
        fp = report.fingerprint
        if report.fingerprint_errors:
            print("[FAIL] Fingerprint injection:")
            for e in report.fingerprint_errors:
                print(f"  {e}")
        else:
            print("[PASS] Fingerprint injected")
            print(f"  user_id:  {fp.user_id}")
            print(f"  build_id: {fp.build_id}")
            print(f"  channel:  {fp.channel}")
            print(f"  built_at: {fp.built_at}")
            if fp.key_prefix:
                print(f"  api_key_prefix: {fp.key_prefix}")
            if fp.issued_key:
                print("")
                print("  " + "!" * 60)
                print("  NEW API KEY ISSUED -- SAVE NOW, WILL NEVER BE SHOWN AGAIN:")
                print(f"    {fp.issued_key}")
                print("  " + "!" * 60)
        print("")
    else:
        print("[SKIP] Fingerprint injection (--user-id not provided)")
        print("")
    if report.zip_path:
        size_kb = report.zip_path.stat().st_size / 1024
        print(f"[ZIP]  {report.zip_path} ({size_kb:.1f} KB)")
        print("")
    print("=" * 72)
    if report.ok:
        print("BUILD OK -- distribution is ready to ship")
    else:
        print("BUILD FAILED -- fix the above errors before shipping")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build sanitised LAPLACE client distribution"
    )
    parser.add_argument(
        "--out",
        default="dist_client",
        help="Output directory (relative to repo root)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Print every excluded file"
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="Embed a per-user fingerprint (user_id, build_id, timestamp)",
    )
    parser.add_argument(
        "--channel",
        default="beta",
        help="Distribution channel tag (e.g. dev, beta, stable)",
    )
    parser.add_argument(
        "--issue-key",
        action="store_true",
        help="Auto-issue an API key via LAPLACE_API_URL admin endpoint",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help="Produce builds/laplace_client_<user>_<id>.zip after build",
    )
    args = parser.parse_args()

    src = Path(__file__).resolve().parent.parent
    out = src / args.out

    fp: Optional[BuildFingerprint] = None
    if args.user_id:
        fp = BuildFingerprint(
            user_id=args.user_id,
            build_id=_make_build_id(args.user_id),
            built_at=datetime.now(tz=timezone.utc).isoformat(),
            channel=args.channel,
        )

    print(f"Source:      {src}")
    print(f"Destination: {out}")
    if fp:
        print(f"User:        {fp.user_id}")
        print(f"Build ID:    {fp.build_id}")
        print(f"Channel:     {fp.channel}")
    print("")

    report = build(
        src,
        out,
        verbose=args.verbose,
        fp=fp,
        issue_key=args.issue_key,
        make_zip=args.zip,
    )
    print_summary(report, out)

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
