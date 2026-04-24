# =============================================================
# BACOPY 初回セットアップ (統合版)
# =============================================================
#
# GUI の "INSTALL ON THIS PC" ボタンから起動される。
# このスクリプト1つで以下を全て自動実行:
#
#   1. 必須ランタイムのインストール (winget 優先 / 直接DL フォールバック)
#        - Visual C++ Redistributable (Camoufox/エンジン動作に必要)
#        - Edge WebView2 Runtime     (Electron GUI に必要)
#   2. OpenSSH Server のインストール + サービス起動 + 自動起動
#   3. 管理者公開鍵を administrators_authorized_keys に配置 + ACL
#   4. sshd_config を公開鍵認証のみに強化 (opt-in)
#   5. Windows Firewall に 22 番ポート受信規則を追加
#
# 管理者権限必須 (UAC 承認後、GUI から自動起動される)。
#
# 引数:
#   -AdminPubKeyPath  管理者公開鍵ファイルパス (任意)
#   -DryRun           実際には変更せずログのみ出力
#   -HardenSshdConfig パスワード認証を無効化 (強化モード)
#   -LogPath          ログ保存先 (既定: C:\ProgramData\BACOPY\setup-all.log)
# =============================================================

param(
    [string]$AdminPubKeyPath = "",
    [switch]$DryRun,
    [switch]$HardenSshdConfig,
    [string]$LogPath = "$env:ProgramData\BACOPY\setup-all.log"
)

$ErrorActionPreference = "Continue"

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
Write-Log "BACOPY Full Setup Start"
Write-Log "DryRun=$DryRun AdminPubKey=$AdminPubKeyPath"
Write-Log "OS: $([System.Environment]::OSVersion.VersionString)"
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

# --- 1. ランタイムのインストール ---
Write-Log "--- Phase 1: Runtime packages ---"

# winget の有無を確認
$wingetCmd = Get-Command winget -ErrorAction SilentlyContinue

function Install-WithWinget {
    param([string]$PackageId, [string]$DisplayName)
    if ($DryRun) { Write-Log "[DryRun] winget install $PackageId"; return $true }
    try {
        $result = winget install -e --id $PackageId --silent --accept-package-agreements --accept-source-agreements 2>&1
        if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq -1978335189) {
            # -1978335189 = already installed
            Write-Log "${DisplayName}: OK (winget)"
            return $true
        }
        Write-Log "${DisplayName}: winget exit=$LASTEXITCODE" "WARN"
        return $false
    } catch {
        Write-Log "${DisplayName}: winget error $($_.Exception.Message)" "WARN"
        return $false
    }
}

function Install-VCRedist {
    # VCRedist: winget 優先、失敗時は Microsoft 直接 DL
    if ($wingetCmd) {
        $ok = Install-WithWinget "Microsoft.VCRedist.2015+.x64" "VCRedist 2015+x64"
        if ($ok) { return }
    }
    if ($DryRun) { Write-Log "[DryRun] VCRedist 直接DL"; return }
    Write-Log "VCRedist: 直接ダウンロードを試みます..."
    $url = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    $dest = "$env:TEMP\vc_redist.x64.exe"
    try {
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 60
        Start-Process -FilePath $dest -ArgumentList "/install", "/quiet", "/norestart" -Wait -NoNewWindow
        Write-Log "VCRedist: 直接DL インストール完了"
    } catch {
        Write-Log "VCRedist 直接DL 失敗: $($_.Exception.Message)" "WARN"
    } finally {
        if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
    }
}

function Install-EdgeWebView2 {
    # Edge WebView2: winget 優先、失敗時は Microsoft 直接 DL
    if ($wingetCmd) {
        $ok = Install-WithWinget "Microsoft.EdgeWebView2Runtime" "Edge WebView2 Runtime"
        if ($ok) { return }
    }
    if ($DryRun) { Write-Log "[DryRun] WebView2 直接DL"; return }
    # 既にインストール済みか確認
    $wv2 = Get-ItemProperty "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" `
        -ErrorAction SilentlyContinue
    if ($wv2) { Write-Log "Edge WebView2: 既にインストール済み"; return }
    Write-Log "Edge WebView2: 直接ダウンロードを試みます..."
    $url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
    $dest = "$env:TEMP\MicrosoftEdgeWebview2Setup.exe"
    try {
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 60
        Start-Process -FilePath $dest -ArgumentList "/silent", "/install" -Wait -NoNewWindow
        Write-Log "Edge WebView2: 直接DL インストール完了"
    } catch {
        Write-Log "Edge WebView2 直接DL 失敗: $($_.Exception.Message)" "WARN"
    } finally {
        if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
    }
}

if (-not $wingetCmd) {
    Write-Log "winget が見つかりません。直接ダウンロードで代替します。" "WARN"
    Write-Log "(winget は Windows 10 バージョン 1709 以降の App Installer で利用可能)" "WARN"
}

Install-VCRedist
Install-EdgeWebView2

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
        Write-Log "sshd: 起動 + 自動起動 ON"
    } catch {
        Write-Log "sshd 起動失敗: $($_.Exception.Message)" "WARN"
    }
}

# --- 3. 管理者公開鍵 ---
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
            if (-not (Test-Path $sshDir)) { New-Item -Path $sshDir -ItemType Directory -Force | Out-Null }

            $isNewFile = -not (Test-Path $authKeysPath)
            if ($isNewFile) {
                Set-Content -Path $authKeysPath -Value $adminPubKey -Encoding ASCII
                Write-Log "admin 公開鍵: ファイル作成"
                icacls $authKeysPath /inheritance:r 2>&1 | Out-Null
                icacls $authKeysPath /grant "Administrators:F" /grant "SYSTEM:F" 2>&1 | Out-Null
                Write-Log "ACL 設定完了"
            } else {
                $existing = Get-Content $authKeysPath -Raw -ErrorAction SilentlyContinue
                if ($existing -and $existing.Contains($adminPubKey)) {
                    Write-Log "admin 公開鍵: 登録済み (スキップ)"
                } else {
                    Add-Content -Path $authKeysPath -Value "`n$adminPubKey" -Encoding ASCII
                    Write-Log "admin 公開鍵: 追記完了"
                }
            }
        }
    }
} else {
    Write-Log "--- Phase 3: 管理者公開鍵なし (スキップ) ---"
}

# --- 4. sshd_config 強化 (opt-in) ---
Write-Log "--- Phase 4: sshd_config (harden=$HardenSshdConfig) ---"
$sshConfig = "$env:ProgramData\ssh\sshd_config"
if (-not $HardenSshdConfig) {
    Write-Log "sshd_config 強化: スキップ (既存設定を尊重)"
} elseif ($DryRun) {
    Write-Log "[DryRun] sshd_config 編集"
} else {
    if (Test-Path $sshConfig) {
        try {
            $cfg = Get-Content $sshConfig -Raw
            $backup = "$sshConfig.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
            Copy-Item $sshConfig $backup -Force
            Write-Log "sshd_config バックアップ: $backup"
            if ($cfg -match "(?m)^#?PasswordAuthentication\s+\w+") {
                $cfg = $cfg -replace "(?m)^#?PasswordAuthentication\s+\w+", "PasswordAuthentication no"
            } else { $cfg += "`r`nPasswordAuthentication no`r`n" }
            if ($cfg -match "(?m)^#?PubkeyAuthentication\s+\w+") {
                $cfg = $cfg -replace "(?m)^#?PubkeyAuthentication\s+\w+", "PubkeyAuthentication yes"
            }
            Set-Content -Path $sshConfig -Value $cfg -Encoding ASCII
            Restart-Service sshd -ErrorAction SilentlyContinue
            Write-Log "sshd_config: 公開鍵認証のみ設定完了"
        } catch {
            Write-Log "sshd_config 編集失敗: $($_.Exception.Message)" "WARN"
        }
    } else {
        Write-Log "sshd_config 未作成 — スキップ" "WARN"
    }
}

# --- 5. Windows Firewall ---
Write-Log "--- Phase 5: Windows Firewall ---"
if ($DryRun) {
    Write-Log "[DryRun] New-NetFirewallRule BACOPY-sshd"
} else {
    # 旧LAPLACE用ルールがあれば削除して置き換え
    $oldRule = Get-NetFirewallRule -Name "LAPLACE-sshd" -ErrorAction SilentlyContinue
    if ($oldRule) {
        Remove-NetFirewallRule -Name "LAPLACE-sshd" -ErrorAction SilentlyContinue
        Write-Log "旧 LAPLACE-sshd ルール削除"
    }
    $existingRule = Get-NetFirewallRule -Name "BACOPY-sshd" -ErrorAction SilentlyContinue
    if ($existingRule) {
        Write-Log "Firewall rule BACOPY-sshd: 既に存在"
    } else {
        try {
            New-NetFirewallRule -Name "BACOPY-sshd" `
                -DisplayName "BACOPY Copytrade - SSH Remote Support" `
                -Enabled True -Direction Inbound -Protocol TCP -LocalPort 22 `
                -Action Allow | Out-Null
            Write-Log "Firewall rule BACOPY-sshd: 作成完了"
        } catch {
            Write-Log "Firewall rule 作成失敗: $($_.Exception.Message)" "WARN"
        }
    }
}

# --- 6. SSH リバーストンネル をタスクスケジューラに登録 (GUI から独立) ---
Write-Log "--- Phase 6: SSH Support Tunnel (Task Scheduler) ---"

$resDir = Split-Path -Parent $PSCommandPath
$envPath = Join-Path $resDir ".env"

function Read-BacopyEnv {
    param([string]$Path)
    $result = @{}
    if (-not (Test-Path $Path)) { return $result }
    foreach ($line in (Get-Content $Path -Encoding UTF8 -ErrorAction SilentlyContinue)) {
        $line = $line.Trim()
        if ($line -eq '' -or $line.StartsWith('#')) { continue }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        $k = $line.Substring(0, $idx).Trim()
        $v = $line.Substring($idx + 1).Trim()
        $result[$k] = $v
    }
    return $result
}

$env = Read-BacopyEnv $envPath
$tunnelEnabled  = ($env['BACOPY_SUPPORT_ENABLED'] -eq '1')
$sshHost        = $env['BACOPY_SUPPORT_SSH_HOST']
$sshKeyRaw      = $env['BACOPY_SUPPORT_SSH_KEY']
$remotePort     = $env['BACOPY_SUPPORT_REMOTE_PORT']
$localPort      = if ($env['BACOPY_SUPPORT_LOCAL_PORT']) { $env['BACOPY_SUPPORT_LOCAL_PORT'] } else { '22' }
$isEncrypted    = ($env['BACOPY_SUPPORT_SSH_KEY_ENCRYPTED'] -eq '1')
$userEmail      = $env['BACOPY_SUPPORT_USER_EMAIL']

if (-not $tunnelEnabled -or -not $sshHost -or -not $remotePort) {
    Write-Log "SSH トンネル: 設定不足のためスキップ (BACOPY_SUPPORT_ENABLED/SSH_HOST/REMOTE_PORT を確認)"
} else {
    # 鍵ファイルのパス解決
    $sshKeyPath = if ([System.IO.Path]::IsPathRooted($sshKeyRaw)) {
        $sshKeyRaw
    } else {
        Join-Path $resDir $sshKeyRaw
    }

    # 鍵を復号して永続パスに書き出す
    $plainKeyPath = "$env:ProgramData\BACOPY\support_key_plain"
    $keyOk = $false

    if ($DryRun) {
        Write-Log "[DryRun] SSH鍵処理スキップ"
        $keyOk = $true
    } elseif ($isEncrypted -and $userEmail -and (Test-Path $sshKeyPath)) {
        try {
            $encB64 = (Get-Content $sshKeyPath -Raw -Encoding ASCII).Trim()
            $emailBytes = [System.Text.Encoding]::UTF8.GetBytes($userEmail.ToLower())
            $saltBytes  = [System.Text.Encoding]::UTF8.GetBytes('bacopy-support-v1-2026')
            $pbkdf2 = New-Object System.Security.Cryptography.Rfc2898DeriveBytes(
                $emailBytes, $saltBytes, 100000,
                [System.Security.Cryptography.HashAlgorithmName]::SHA256
            )
            $aesKey = $pbkdf2.GetBytes(32)
            $data        = [Convert]::FromBase64String($encB64)
            $iv          = $data[0..15]
            $ciphertext  = $data[16..($data.Length - 1)]
            $aes         = [System.Security.Cryptography.Aes]::Create()
            $aes.Key     = $aesKey; $aes.IV = $iv; $aes.Mode = 'CBC'; $aes.Padding = 'PKCS7'
            $dec         = $aes.CreateDecryptor()
            $plain       = $dec.TransformFinalBlock($ciphertext, 0, $ciphertext.Length)
            New-Item -Path (Split-Path $plainKeyPath) -ItemType Directory -Force | Out-Null
            [System.IO.File]::WriteAllBytes($plainKeyPath, $plain)
            icacls $plainKeyPath /inheritance:r /grant "SYSTEM:F" /grant "Administrators:F" 2>&1 | Out-Null
            Write-Log "SSH鍵: 復号完了 -> $plainKeyPath"
            $keyOk = $true
        } catch {
            Write-Log "SSH鍵 復号失敗: $($_.Exception.Message)" "WARN"
        }
    } elseif (-not $isEncrypted -and (Test-Path $sshKeyPath)) {
        New-Item -Path (Split-Path $plainKeyPath) -ItemType Directory -Force | Out-Null
        Copy-Item $sshKeyPath $plainKeyPath -Force
        icacls $plainKeyPath /inheritance:r /grant "SYSTEM:F" /grant "Administrators:F" 2>&1 | Out-Null
        Write-Log "SSH鍵: コピー完了 -> $plainKeyPath"
        $keyOk = $true
    } else {
        Write-Log "SSH鍵ファイルが見つかりません: $sshKeyPath" "WARN"
    }

    if ($keyOk -and -not $DryRun) {
        # トンネル維持スクリプトを書き出す
        $tunnelScriptPath = "$env:ProgramData\BACOPY\run_tunnel.ps1"
        $tunnelScript = @"
# BACOPY SSH Support Tunnel — GUI から独立して動作
while (`$true) {
    try {
        & ssh.exe -i "$plainKeyPath" ``
            -o StrictHostKeyChecking=no ``
            -o BatchMode=yes ``
            -o ExitOnForwardFailure=yes ``
            -o ServerAliveInterval=30 ``
            -o ServerAliveCountMax=3 ``
            -N -R 127.0.0.1:${remotePort}:127.0.0.1:${localPort} ``
            $sshHost
    } catch {}
    Start-Sleep -Seconds 10
}
"@
        Set-Content -Path $tunnelScriptPath -Value $tunnelScript -Encoding UTF8
        Write-Log "トンネルスクリプト: $tunnelScriptPath"

        # タスクスケジューラに登録
        $taskName = "BACOPY-SupportTunnel"
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
            Write-Log "既存タスク削除: $taskName"
        }
        $action  = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$tunnelScriptPath`""
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet `
            -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -MultipleInstances IgnoreNew
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force | Out-Null
        # 即時起動
        Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Write-Log "タスクスケジューラ登録完了: $taskName (SYSTEM, ログオン時 + 即時起動)"
    }
}

# --- 完了 ---
Write-Log "================================"
Write-Log "BACOPY Setup Complete"
Write-Log "Log: $LogPath"
Write-Log "================================"

if (-not $DryRun) {
    Write-Host ""
    Write-Host "==============================="
    Write-Host " BACOPY セットアップ完了"
    Write-Host "==============================="
    Write-Host "ログ: $LogPath"
    Write-Host ""
    Write-Host "次のステップ:"
    Write-Host "  1. GUI を再起動してください"
    Write-Host "  2. SSH サポートトンネルはシステム起動時に自動接続されます (GUI 不要)"
    Write-Host ""
    Start-Sleep -Seconds 5
}
exit 0
