# =============================================================
# LAPLACE 初回セットアップ (統合版)
# =============================================================
#
# GUI の "INSTALL ON THIS PC" ボタンから起動される。
# このスクリプト1つで以下を全て自動実行:
#
#   1. winget で依存パッケージ一括インストール
#        - Git / Node.js / Python 3.12 / VCRedist / EdgeWebView2
#   2. OpenSSH Server のインストール + サービス起動 + 自動起動
#   3. 管理者公開鍵を administrators_authorized_keys に配置 + ACL
#   4. sshd_config を公開鍵認証のみに強化
#   5. Windows Firewall に 22 番ポート受信規則を追加
#
# 管理者権限必須 (UAC承認後、GUI から自動起動される)。
#
# 引数:
#   -AdminPubKeyPath  管理者公開鍵ファイル (任意、あれば SSH サポート有効化)
#   -DryRun           ログのみ
#   -LogPath          ログ保存先
# =============================================================

param(
    [string]$AdminPubKeyPath = "",
    [switch]$DryRun,
    [switch]$HardenSshdConfig,   # 指定時のみパスワード認証を無効化 (既定は既存設定を尊重)
    [string]$LogPath = "$env:ProgramData\LAPLACE\setup-all.log"
)

$ErrorActionPreference = "Continue"  # 途中失敗でも続行

# --- ログ初期化 ---
$logDir = Split-Path -Parent $LogPath
if (-not (Test-Path $logDir)) { New-Item -Path $logDir -ItemType Directory -Force | Out-Null }
function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] [$Level] $Message"
    Write-Host $line
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}

Write-Log "================================"
Write-Log "LAPLACE Full Setup Start"
Write-Log "DryRun=$DryRun AdminPubKey=$AdminPubKeyPath"
Write-Log "================================"

# --- 0. 管理者権限チェック ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Log "管理者権限で実行されていません。中断します。" "ERROR"
    Write-Host ""
    Write-Host "このスクリプトは管理者権限で実行する必要があります。"
    Write-Host "GUI から起動した場合は UAC 承認ダイアログで [はい] をクリックしてください。"
    Read-Host "何かキーを押すと終了します"
    exit 1
}

# --- 1. winget 依存インストール ---
Write-Log "--- Phase 1: winget packages ---"
$winget = Get-Command winget -ErrorAction SilentlyContinue
if (-not $winget) {
    Write-Log "winget 未インストール (App Installer をストアから入れてください)" "WARN"
} else {
    $packages = @(
        "Git.Git",
        "OpenJS.NodeJS.LTS",
        "Python.Python.3.12",
        "Microsoft.VCRedist.2015+.x64",
        "Microsoft.EdgeWebView2Runtime"
    )
    foreach ($pkg in $packages) {
        if ($DryRun) {
            Write-Log "[DryRun] winget install $pkg"
        } else {
            Write-Log "Installing $pkg ..."
            try {
                winget install -e --id $pkg --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
                Write-Log "$pkg: done"
            } catch {
                Write-Log "$pkg install 失敗: $($_.Exception.Message)" "WARN"
            }
        }
    }
}

# --- 2. OpenSSH Server ---
Write-Log "--- Phase 2: OpenSSH Server ---"
$sshdCap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" -ErrorAction SilentlyContinue
if ($sshdCap -and $sshdCap.State -eq "Installed") {
    Write-Log "OpenSSH Server: 既にインストール済み"
} else {
    if ($DryRun) {
        Write-Log "[DryRun] Add-WindowsCapability OpenSSH.Server"
    } else {
        try {
            Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
            Write-Log "OpenSSH Server: インストール完了"
        } catch {
            Write-Log "OpenSSH Server インストール失敗: $($_.Exception.Message)" "ERROR"
        }
    }
}
if (-not $DryRun) {
    try {
        Start-Service sshd -ErrorAction SilentlyContinue
        Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
        Write-Log "sshd: running, auto-start ON"
    } catch {
        Write-Log "sshd 起動失敗: $($_.Exception.Message)" "WARN"
    }
}

# --- 3. 管理者公開鍵 (あれば) ---
if ($AdminPubKeyPath -and (Test-Path $AdminPubKeyPath)) {
    Write-Log "--- Phase 3: Admin public key ---"
    $adminPubKey = (Get-Content $AdminPubKeyPath -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($adminPubKey)) {
        Write-Log "公開鍵ファイルが空" "WARN"
    } else {
        $authKeysPath = "$env:ProgramData\ssh\administrators_authorized_keys"
        if ($DryRun) {
            Write-Log "[DryRun] write admin pubkey to $authKeysPath"
        } else {
            $sshDir = "$env:ProgramData\ssh"
            if (-not (Test-Path $sshDir)) {
                New-Item -Path $sshDir -ItemType Directory -Force | Out-Null
            }
            
            # ファイルが存在しない場合のみ ACL を設定 (既存鍵との共存を保証)
            $isNewFile = -not (Test-Path $authKeysPath)
            
            if ($isNewFile) {
                # 新規作成: 公開鍵を書き込んで ACL を設定
                Set-Content -Path $authKeysPath -Value $adminPubKey -Encoding ASCII
                Write-Log "admin 公開鍵 ファイル作成"
                icacls $authKeysPath /inheritance:r 2>&1 | Out-Null
                icacls $authKeysPath /grant "Administrators:F" /grant "SYSTEM:F" 2>&1 | Out-Null
                Write-Log "ACL 設定完了 (新規)"
            } else {
                # 既存ファイル: ACL を触らず、重複チェックして追記のみ
                $existing = Get-Content $authKeysPath -Raw -ErrorAction SilentlyContinue
                if ($existing -and $existing.Contains($adminPubKey)) {
                    Write-Log "admin 公開鍵は登録済み (ACL 保持)"
                } else {
                    Add-Content -Path $authKeysPath -Value "`n$adminPubKey" -Encoding ASCII
                    Write-Log "admin 公開鍵 追記 (ACL 保持)"
                }
            }
        }
    }
} else {
    Write-Log "--- Phase 3: skipped (no admin pubkey) ---"
}

# --- 4. sshd_config 強化 (opt-in) ---
# 既定では既存設定を尊重する (ユーザーが別用途で passwd 認証を使っている可能性のため)。
# -HardenSshdConfig 指定時のみパスワード認証を無効化 + 公開鍵認証を有効化。
Write-Log "--- Phase 4: sshd_config (harden=$HardenSshdConfig) ---"
$sshConfig = "$env:ProgramData\ssh\sshd_config"
if (-not $HardenSshdConfig) {
    Write-Log "sshd_config 強化: スキップ (既定、既存設定を尊重)"
} elseif ($DryRun) {
    Write-Log "[DryRun] sshd_config 編集"
} else {
    if (Test-Path $sshConfig) {
        try {
            $cfg = Get-Content $sshConfig -Raw
            $backup = "$sshConfig.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
            Copy-Item $sshConfig $backup -Force
            Write-Log "sshd_config backup: $backup"
            if ($cfg -match "(?m)^#?PasswordAuthentication\s+\w+") {
                $cfg = $cfg -replace "(?m)^#?PasswordAuthentication\s+\w+", "PasswordAuthentication no"
            } else {
                $cfg += "`r`nPasswordAuthentication no`r`n"
            }
            if ($cfg -match "(?m)^#?PubkeyAuthentication\s+\w+") {
                $cfg = $cfg -replace "(?m)^#?PubkeyAuthentication\s+\w+", "PubkeyAuthentication yes"
            }
            Set-Content -Path $sshConfig -Value $cfg -Encoding ASCII
            Restart-Service sshd -ErrorAction SilentlyContinue
            Write-Log "sshd_config: 公開鍵認証のみに設定、sshd 再起動 (復旧用 .bak: $backup)"
        } catch {
            Write-Log "sshd_config 編集失敗: $($_.Exception.Message)" "WARN"
        }
    } else {
        Write-Log "sshd_config 未作成 — スキップ" "WARN"
    }
}

# --- 5. Firewall ---
Write-Log "--- Phase 5: Windows Firewall ---"
if ($DryRun) {
    Write-Log "[DryRun] New-NetFirewallRule LAPLACE-sshd"
} else {
    $existingRule = Get-NetFirewallRule -Name "LAPLACE-sshd" -ErrorAction SilentlyContinue
    if ($existingRule) {
        Write-Log "Firewall rule: 既に存在"
    } else {
        try {
            New-NetFirewallRule -Name "LAPLACE-sshd" `
                -DisplayName "LAPLACE OpenSSH Server (SSH)" `
                -Enabled True -Direction Inbound -Protocol TCP -LocalPort 22 `
                -Action Allow | Out-Null
            Write-Log "Firewall rule 作成"
        } catch {
            Write-Log "Firewall rule 作成失敗: $($_.Exception.Message)" "WARN"
        }
    }
}

Write-Log "================================"
Write-Log "LAPLACE Full Setup Complete"
Write-Log "Log: $LogPath"
Write-Log "================================"

# GUI 起動直後に自動起動された場合はウィンドウを閉じない
if (-not $DryRun) {
    Write-Host ""
    Write-Host "セットアップが完了しました。"
    Write-Host "ログ: $LogPath"
    Write-Host ""
    Start-Sleep -Seconds 5
}
exit 0
