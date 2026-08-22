$ErrorActionPreference = "Stop"

Write-Host "================================"
Write-Host "LAPLACE Dependency Installer"
Write-Host "================================"

function Ensure-Winget {
  $winget = Get-Command winget -ErrorAction SilentlyContinue
  if (-not $winget) {
    Write-Host "winget not found. Please install App Installer from Microsoft Store."
    exit 1
  }
}

function Install-Package($id) {
  Write-Host "Installing $id ..."
  winget install -e --id $id --silent --accept-package-agreements --accept-source-agreements
}

Ensure-Winget

Install-Package "Git.Git"
Install-Package "OpenJS.NodeJS.LTS"
Install-Package "Python.Python.3.12"
Install-Package "Microsoft.VCRedist.2015+.x64"
Install-Package "Microsoft.EdgeWebView2Runtime"

Write-Host "Installing OpenSSH Server ..."
try {
  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
  Start-Service sshd
  Set-Service -Name sshd -StartupType Automatic
} catch {
  Write-Host "OpenSSH Server install failed: $($_.Exception.Message)"
}

Write-Host "Done."
