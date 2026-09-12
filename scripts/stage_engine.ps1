$ErrorActionPreference = "Stop"

<#
.SYNOPSIS
  インストーラへ同梱する engine を 1 本だけ build_staging/engine に置く。

.DESCRIPTION
  electron-builder は build_staging/engine を「丸ごと」extraResources に入れる
  (copytrade_gui/package.json)。したがって、そこに複数の exe が残っていると
  意図しない版まで配布物に混入する。

  ★特に危険なのは、田辺チーム向けのインストーラに full 版 (梶原ロジック入り) が
    残ること。使わせないだけでは意味がなく、ファイルとして渡った時点で流出になる。

  GUI (copytrade_gui/src/main.js) は engine のファイル名を
  "bacopy_engine.exe" で固定している (プロセス列挙・起動・kill の4箇所)。
  そのため、どの版を使う場合でもこの名前で置く必要がある。

.PARAMETER Variant
  full   … 梶原チーム向け (dual_line_* を同梱。パターン判定とSEQを含む)
  mirror … 田辺チーム向け (dual_line_* を非同梱。資金管理はエンジン内蔵)

.EXAMPLE
  powershell -File scripts/stage_engine.ps1 -Variant mirror
  cd copytrade_gui; npm run build:installer
#>
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("full", "mirror")]
  [string]$Variant
)

$ROOT = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ENGINE_DIR = Join-Path $ROOT "copytrade_gui/build_staging/engine"
$VARIANTS = Join-Path $ROOT "copytrade_gui/build_staging/engine_variants"
$TARGET = Join-Path $ENGINE_DIR "bacopy_engine.exe"

$src = switch ($Variant) {
  "full"   { Join-Path $VARIANTS "bacopy_engine_full.exe" }
  "mirror" { Join-Path $VARIANTS "bacopy_engine_mirror.exe" }
}

if (-not (Test-Path $src)) {
  throw "$Variant の engine が見つかりません: $src`n  先に scripts/build_bacopy_engine$(if($Variant -eq 'mirror'){'_mirror'}).ps1 を実行してください。"
}

New-Item -ItemType Directory -Force -Path $ENGINE_DIR | Out-Null

# ★engine/ に残っている他の exe を必ず退かす。
#   ここを飛ばすと「前回の版」が同梱され続ける。
Get-ChildItem $ENGINE_DIR -Filter *.exe -ErrorAction SilentlyContinue | ForEach-Object {
  Write-Host "[clean] 既存を削除: $($_.Name)"
  Remove-Item $_.FullName -Force
}

Copy-Item $src $TARGET -Force
$h = (Get-FileHash $TARGET -Algorithm SHA256).Hash.Substring(0, 12).ToLower()
$mb = [math]::Round((Get-Item $TARGET).Length / 1MB, 1)
Write-Host "[ok] staged: $Variant -> bacopy_engine.exe  ($mb MB / sha256 $h)"
Write-Host ""
Write-Host "次: cd copytrade_gui; npm run build:installer"
Write-Host "★配布前に必ず exe を『実行して』検証すること (文字列検索では確かめない)。"
if ($Variant -eq "mirror") {
  Write-Host "   mirror 版は dual-line サブコマンドが ImportError で失敗するのが正常。"
}
