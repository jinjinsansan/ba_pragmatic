$ErrorActionPreference = "Stop"

# ミラー版 (田辺チーム) の engine をビルドする。
#
#   出力: copytrade_gui/build_staging/engine/bacopy_engine_mirror.exe
#
# full 版 (scripts/build_bacopy_engine.ps1) との違いは 1 点だけ:
#   梶原ロジック (dual_line_*) を **一切同梱しない**。
#
# 理由:
#   田辺チームはマスター画面の人間が side を決めるので、6/10パターンの判定は
#   要らない。資金管理 (ダランベール / マーチンゲール / グランドマーチンゲール) は
#   bacopy_executor_pragmatic_ws_live.py に内蔵されているため dual_line_money も不要。
#   使わない物を配らなければ、梶原ロジック (869行) のコピーが外に出る先は
#   梶原チームだけになる。
#
# ★camoufox は除外できない。
#   bacopy_executor_pragmatic_ws_live.py の main() が無条件に
#   `from camoufox.sync_api import Camoufox` するため、
#   除外すると起動直後に SystemExit する。
#
# ★検証は文字列検索でなく「実行」で行うこと (SEQ_CUSTOM_START_2026-08-05.md の教訓)。
#   ビルド後に `bacopy_engine_mirror.exe dual-line --help` が失敗することを確かめる。

# ★出力先は build_staging/engine ではなく engine_variants。
#   electron-builder は build_staging/engine を「丸ごと」同梱するため、
#   そこに full 版と mirror 版を並べると、田辺チームのインストーラに
#   梶原ロジック入りの exe まで入ってしまう。
#   梱包直前に scripts/stage_engine.ps1 で 1 本だけ engine/ へ置く運用にする。
$ROOT = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OUTDIR = Join-Path $ROOT "copytrade_gui/build_staging/engine_variants"
New-Item -ItemType Directory -Force -Path $OUTDIR | Out-Null

Push-Location $ROOT
try {
  $dist = Join-Path $ROOT "dist_mirror"
  $build = Join-Path $ROOT "build_mirror"
  if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
  if (Test-Path $build) { Remove-Item -Recurse -Force $build }

  python -m PyInstaller --noconfirm --clean --onefile `
    --name bacopy_engine_mirror `
    --collect-all camoufox `
    --collect-all browserforge `
    --collect-all apify_fingerprint_datapoints `
    --collect-all language_tags `
    --collect-submodules playwright `
    --collect-all encodings `
    --hidden-import tzdata `
    --hidden-import bacopy_executor_pragmatic_ws_live `
    --hidden-import bacopy_watch_pragmatic `
    --hidden-import bacopy_watch_evolution `
    --hidden-import marubatsu_strategy `
    --exclude-module dual_line_pragmatic_bot `
    --exclude-module dual_line_live_executor `
    --exclude-module dual_line_logic `
    --exclude-module dual_line_match `
    --exclude-module dual_line_money `
    --distpath $dist `
    --workpath $build `
    --specpath $build `
    (Join-Path $ROOT "bacopy_engine.py")

  Copy-Item (Join-Path $dist "bacopy_engine_mirror.exe") $OUTDIR -Force
  Write-Host "[ok] built: $OUTDIR\bacopy_engine_mirror.exe"
} finally {
  Pop-Location
}
