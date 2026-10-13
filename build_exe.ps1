# Matrix Facility Manager - build the distributable folder app (PyInstaller)
#
# Builds the localhost web UI (run_server.py) into dist\MatrixDeploy\ and zips
# it to dist\MatrixDeploy.zip, ready to hand to coworkers. The folder contains:
#   MatrixDeploy.exe, _internal\     the app (no Python install needed)
#   config\deploy_config.example.json
#   tools\MatrixHealthCheck.ps1, .env.example, START HERE.txt
# No labs and no passwords ship: no config\<lab>.json, no config\<lab>.env, no
# root .env. Each coworker imports the facility planner zip of the site they
# work on (Settings > Facility config), which brings the lab layout and the
# room SSH/sudo login; the app then asks for the router/switch passwords.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$outRoot = Join-Path $PSScriptRoot "dist"

# Build, assemble and zip in %TEMP%, then copy the results to dist\. The repo
# lives under OneDrive, whose sync locks freshly written files and makes
# PyInstaller cleanup / Compress-Archive fail with "Access is denied".
$running = Get-Process MatrixDeploy -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -like "$outRoot\*" }
if ($running) {
  throw "MatrixDeploy.exe is running from $outRoot (PID $($running.Id -join ', ')). Close its console window(s), then re-run the build."
}

$work = Join-Path $env:TEMP "matrixdeploy-pyinstaller"
$stage = Join-Path $work "dist"
$distDir = Join-Path $stage "MatrixDeploy"
$zipPath = Join-Path $stage "MatrixDeploy.zip"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }

# Native tools (pip, PyInstaller) log to stderr; under Windows PowerShell 5
# with "Stop" that aborts the script even on success. Judge by exit code only.
function Invoke-Native([string]$what, [scriptblock]$cmd) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  & $cmd 2>&1 | ForEach-Object { "$_" } | Out-Host
  $code = $LASTEXITCODE
  $ErrorActionPreference = $prev
  if ($code -ne 0) { throw "$what failed (exit $code)" }
}

# QA lock (hash only, never the password), baked into _internal so deleting
# a file from the app's config folder can't unlock the QA tabs. See
# matrix_deploy\qa_lock.py. Required: every shared build has the QA tabs
# locked, so nobody can hand out an unlocked copy by accident.
$qaLockFile = "config\qa_lock.cfg"
$qaLock = $null
if (Test-Path $qaLockFile) {
  try { $qaLock = Get-Content $qaLockFile -Raw | ConvertFrom-Json } catch { $qaLock = $null }
}
if (-not $qaLock -or -not $qaLock.salt -or -not $qaLock.verify) {
  throw ("No valid QA lock in $qaLockFile, so this build would leave the QA-only tabs open to everyone. " +
    "Set the QA password first: run 'python run_server.py', open Settings > Your team > QA password, then re-run this build.")
}
$qaLockArgs = @("--add-data", "$PSScriptRoot\$qaLockFile;matrix_deploy")
Write-Host "QA lock: built in ($qaLockFile)"

Write-Host "Installing build dependencies..."
# Pinned: an unpinned --upgrade pulls whatever was published minutes ago into the shipped exe.
Invoke-Native "pip install" { python -m pip install --disable-pip-version-check "pyinstaller==6.22.3" -r requirements.txt }

Write-Host "Building folder app..."
# Plain --console build. SentinelOne deleted a --windowed build on sight, and
# flagged a build that hid its own console. Don't add either back; get the
# exe signed/allowlisted by IT instead.
Invoke-Native "PyInstaller" { python -m PyInstaller --noconfirm --clean --console --name MatrixDeploy `
  --workpath "$work\build" --specpath "$work" --distpath "$stage" `
  --icon "$PSScriptRoot\matrix_deploy\web\static\app-icon.ico" `
  --add-data "$PSScriptRoot\matrix_deploy\web\static;matrix_deploy\web\static" `
  --add-data "$PSScriptRoot\matrix_deploy\golden_files;matrix_deploy\golden_files" `
  --hidden-import "cryptography.hazmat.bindings._rust" `
  --collect-all cryptography `
  --collect-submodules uvicorn `
  --collect-submodules websockets `
  --exclude-module PyQt5 `
  @qaLockArgs `
  run_server.py }

# --- Config folder: the example only ----------------------------------------
# Labs come from each coworker's own facility zip import, never from the build:
# your lab profiles (router IPs, room layout) and passwords stay on this PC.
$cfgOut = Join-Path $distDir "config"
New-Item -ItemType Directory -Force $cfgOut | Out-Null
Copy-Item "config\deploy_config.example.json" $cfgOut
Copy-Item ".env.example" (Join-Path $distDir ".env.example")
# Windows device health check: copied on its own to field devices (never the
# rest of this folder - once used it holds that person's saved passwords).
$toolsOut = Join-Path $distDir "tools"
New-Item -ItemType Directory -Force $toolsOut | Out-Null
Copy-Item "tools\health-check\MatrixHealthCheck.ps1" $toolsOut

@"
MATRIX FACILITY MANAGER - QUICK START
=====================================

1. Extract this whole folder somewhere you own (e.g. Documents or Desktop).
   Don't run it from inside the .zip, and don't put it in Program Files.

2. Double-click MatrixDeploy.exe.
   A small console window opens (leave it alone, you can minimize it), and
   Matrix Facility Manager opens in its own window (it uses Chrome or Edge
   behind the scenes). Close the Matrix Facility Manager window to quit; the console closes by
   itself. If a deploy or other job is still running, it finishes first.
   Double-clicking again while it's open just brings up another window.

3. In the app, open Settings > Setup Check.
     Red   = must fix. Use the button next to the item.
     Amber = optional features only.
   To use "Download Latest" or "Trigger Jenkins Build", click
   "Enter my Artifactory/Jenkins details" and use YOUR OWN account.
   Generate the tokens from your profile page on each site. Your Jenkins
   username is your login ID (shown on your Jenkins account page), not
   your email.

4. Add your lab: Settings > Facility config > Import facility config (.zip),
   and pick the facility planner export for your site
   (<SITE>-version<N>.zip). Get it from your lead if you don't have it.
   The app then asks for the router and switch passwords (not in the zip).
   Repeat for each site you work on; switch between them with the "Site"
   dropdown at the top-left. Until a lab is imported, only Health Check
   (any device by address) works.

5. Optional: set your own SWU download folder (and repo/dist folders if
   you use the Web App tab) under Settings > Local folders > Save folders.

This app zip contains no labs and no passwords. The facility zip does
(room login, site passwords): don't forward it, and delete it once imported.
The app never keeps a copy of it.

Passwords and tokens are saved encrypted (Windows DPAPI) in this folder,
readable only by your Windows account on this computer. Still don't share
the folder once you've imported a lab or entered your own tokens.

Troubleshooting
  - "Lab network" warning: connect to the lab network/VPN.
  - Windows SmartScreen "protected your PC": click More info > Run anyway.
  - It doesn't start: the console window shows why and stays open until
    you press Enter.
  - The exe disappears or gets blocked by security software: don't
    restore it yourself. Contact IT.
"@ | Set-Content -Path (Join-Path $distDir "START HERE.txt") -Encoding UTF8

# --- Safety net: no labs, passwords or tokens may ship ----------------------
$labFiles = Get-ChildItem $distDir -Recurse -File -Force |
  Where-Object { $_.FullName -notlike "*\_internal\*" -and ($_.Extension -eq ".env" -or
    ($_.DirectoryName -eq $cfgOut -and $_.Name -ne "deploy_config.example.json")) }
if ($labFiles) { throw "Refusing to package: $($labFiles[0].FullName) is a lab or password file. Labs come from each person's facility zip." }
$leaks = Get-ChildItem $distDir -Recurse -File -Force |
  Where-Object { $_.FullName -notlike "*\_internal\*" -and $_.Name -ne ".env.example" } |
  Select-String -Pattern '^\s*(export\s+)?[A-Z0-9_]*(PASSWORD|TOKEN|API_KEY)\s*=\s*\S'
if ($leaks) { throw "Refusing to package: a password or token was found in $($leaks[0].Path)" }
if (-not (Test-Path (Join-Path $distDir "_internal\matrix_deploy\qa_lock.cfg"))) {
  throw "Refusing to package: the QA lock didn't make it into _internal\matrix_deploy\qa_lock.cfg."
}

Start-Sleep -Seconds 5
if (-not (Test-Path (Join-Path $distDir "MatrixDeploy.exe"))) {
  throw "MatrixDeploy.exe disappeared after the build - security software (e.g. SentinelOne) likely quarantined it. Ask IT to allow it."
}

Write-Host "Zipping..."
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($distDir, $zipPath, [IO.Compression.CompressionLevel]::Optimal, $true)

Write-Host "Copying to $outRoot ..."
foreach ($old in @((Join-Path $outRoot "MatrixDeploy"), (Join-Path $outRoot "MatrixDeploy.zip"))) {
  if (Test-Path $old) { Remove-Item $old -Recurse -Force }
}
New-Item -ItemType Directory -Force $outRoot | Out-Null
Copy-Item $distDir $outRoot -Recurse
Copy-Item $zipPath $outRoot

Write-Host ""
Write-Host "Done."
Write-Host "  Folder: $(Join-Path $outRoot 'MatrixDeploy')"
Write-Host "  Zip:    $(Join-Path $outRoot 'MatrixDeploy.zip')   <- share this"
