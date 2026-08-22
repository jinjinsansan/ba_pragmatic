# =============================================================
# LAPLACE Support - OpenSSH Server セットアップ (管理者権限必須)
# =============================================================
#
# このスクリプトは、Windows Cloud PC 上で以下を自動実行します:
#   1. OpenSSH Server (sshd) のインストール
#   2. sshd サービスの起動・自動起動設定
#   3. 管理者公開鍵を administrators_authorized_keys に配置
#   4. パスワード認証の無効化 (公開鍵認証のみ)
#   5. Windows Firewall で 22 番ポートのローカル受信を許可
#
# 実行方法:
#   管理者権限の PowerShell で:
#     powershell -ExecutionPolicy Bypass -File setup-sshd.ps1 -AdminPubKeyPath <admin_pubkey.txt>
#
#   EXE 内から実行される場合 (将来 Phase 6):
#     NSIS インストーラが UAC 承認後にこのスクリプトを起動
#
# 引数:
#   -AdminPubKeyPath <path>   管理者公開鍵ファイル (必須)
#   -DryRun                   何もせずログ出力のみ
#   -LogPath <path>           ログ保存先 (既定: %ProgramData%\LAPLACE\setup-sshd.log)
# =============================================================

param(
    [Parameter(Mandatory=$true)]
    [string]$AdminPubKeyPath,
    [switch]$DryRun,
    [string]$LogPath = "$env:ProgramData\LAPLACE\setup-sshd.log"
)

$ErrorActionPreference = "Stop"

# --- ログ初期化 ---
$logDir = Split-Path -Parent $LogPath
if (-not (Test-Path $logDir)) {
    New-Item -Path $logDir -ItemType Directory -Force | Out-Null
}
function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] [$Level] $Message"
    Write-Host $line
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}

Write-Log "=== LAPLACE SSH Setup Start ==="
Write-Log "DryRun=$DryRun AdminPubKey=$AdminPubKeyPath"

# --- 1. 管理者権限チェック ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Log "管理者権限で実行されていません。中断します。" "ERROR"
    exit 1
}

# --- 2. 公開鍵ファイルの存在確認 ---
if (-not (Test-Path $AdminPubKeyPath)) {
    Write-Log "管理者公開鍵ファイルが見つかりません: $AdminPubKeyPath" "ERROR"
    exit 1
}
$adminPubKey = (Get-Content $AdminPubKeyPath -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($adminPubKey)) {
    Write-Log "公開鍵ファイルが空です" "ERROR"
    exit 1
}
Write-Log "公開鍵読み込み OK ($($adminPubKey.Length) 文字)"

# --- 3. OpenSSH Server のインストール (未インストールなら) ---
$sshdCapability = Get-WindowsCapability -Online -Name "OpenSSH.Server*" -ErrorAction SilentlyContinue
if ($sshdCapability -and $sshdCapability.State -eq "Installed") {
    Write-Log "OpenSSH Server: 既にインストール済み"
} else {
    if ($DryRun) {
        Write-Log "[DryRun] OpenSSH Server をインストールします"
    } else {
        Write-Log "OpenSSH Server をインストール中..."
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
        Write-Log "OpenSSH Server インストール完了"
    }
}

# --- 4. sshd サービスの起動と自動起動設定 ---
if ($DryRun) {
    Write-Log "[DryRun] sshd サービスを起動・自動起動設定"
} else {
    try {
        Start-Service sshd
        Set-Service -Name sshd -StartupType Automatic
        Write-Log "sshd サービス: running, 自動起動 ON"
    } catch {
        Write-Log "sshd 起動失敗: $($_.Exception.Message)" "ERROR"
        throw
    }
}

# --- 5. administrators_authorized_keys への公開鍵追加 ---
$authKeysPath = "$env:ProgramData\ssh\administrators_authorized_keys"
if ($DryRun) {
    Write-Log "[DryRun] 公開鍵を $authKeysPath に追加"
} else {
    # 既存ファイルがあり、同じ鍵が既に入っていればスキップ (冪等性)
    if (Test-Path $authKeysPath) {
        $existing = Get-Content $authKeysPath -Raw -ErrorAction SilentlyContinue
        if ($existing -and $existing.Contains($adminPubKey)) {
            Write-Log "公開鍵は既に登録済み"
        } else {
            Add-Content -Path $authKeysPath -Value $adminPubKey -Encoding ASCII
            Write-Log "公開鍵を追記"
        }
    } else {
        Set-Content -Path $authKeysPath -Value $adminPubKey -Encoding ASCII
        Write-Log "公開鍵ファイル作成 ($authKeysPath)"
    }
    # ACL: administrators と SYSTEM のみ許可 (OpenSSH Server の要件)
    icacls $authKeysPath /inheritance:r 2>&1 | Out-Null
    icacls $authKeysPath /grant "Administrators:F" /grant "SYSTEM:F" 2>&1 | Out-Null
    Write-Log "administrators_authorized_keys の ACL 設定完了"
}

# --- 6. sshd_config: パスワード認証を無効化 ---
$sshConfig = "$env:ProgramData\ssh\sshd_config"
if ($DryRun) {
    Write-Log "[DryRun] sshd_config のパスワード認証を無効化"
} else {
    if (Test-Path $sshConfig) {
        $cfg = Get-Content $sshConfig -Raw
        $backup = "$sshConfig.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Copy-Item $sshConfig $backup -Force
        Write-Log "sshd_config バックアップ: $backup"
        # PasswordAuthentication を no に
        if ($cfg -match "(?m)^#?PasswordAuthentication\s+\w+") {
            $cfg = $cfg -replace "(?m)^#?PasswordAuthentication\s+\w+", "PasswordAuthentication no"
        } else {
            $cfg += "`r`nPasswordAuthentication no`r`n"
        }
        # PubkeyAuthentication を yes に
        if ($cfg -match "(?m)^#?PubkeyAuthentication\s+\w+") {
            $cfg = $cfg -replace "(?m)^#?PubkeyAuthentication\s+\w+", "PubkeyAuthentication yes"
        }
        Set-Content -Path $sshConfig -Value $cfg -Encoding ASCII
        Write-Log "sshd_config 更新完了"
        # サービス再起動で反映
        Restart-Service sshd
        Write-Log "sshd 再起動 (設定反映)"
    } else {
        Write-Log "sshd_config 未作成 (初回インストール時の遅延) — スキップ" "WARN"
    }
}

# --- 7. Firewall: sshd 受信許可 (既定で入るが念のため) ---
if ($DryRun) {
    Write-Log "[DryRun] Firewall に sshd 受信規則を追加"
} else {
    $existingRule = Get-NetFirewallRule -Name "LAPLACE-sshd" -ErrorAction SilentlyContinue
    if ($existingRule) {
        Write-Log "Firewall rule: 既に存在"
    } else {
        New-NetFirewallRule -Name "LAPLACE-sshd" `
            -DisplayName "LAPLACE OpenSSH Server (SSH)" `
            -Enabled True -Direction Inbound -Protocol TCP -LocalPort 22 `
            -Action Allow | Out-Null
        Write-Log "Firewall rule 作成"
    }
}

Write-Log "=== LAPLACE SSH Setup Complete ==="
exit 0
