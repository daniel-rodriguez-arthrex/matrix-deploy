# Matrix Facility Manager - build the distributable folder app (PyInstaller)
#
# Builds the localhost web UI (run_server.py) into dist\MatrixDeploy\ and zips
# it to dist\MatrixDeploy.zip, ready to hand to coworkers. The folder contains:
#   MatrixDeploy.exe, _internal\     the app (no Python install needed)
#   config\<lab>.json                site profiles from config\
#   config\<lab>.env                 shared lab SSH/sudo passwords ONLY (every
#                                    other key in your lab .env is dropped)
#   .env.example, START HERE.txt
# Your root .env (personal Artifactory/Jenkins tokens) is NEVER copied; each
# coworker enters their own in the app's Setup Check.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$outRoot = Join-Path $PSScriptRoot "dist"
# Lab .env files (from the Matrix Lab extension) also hold switch/router/
# streaming passwords etc. Ship only the keys Matrix Deploy actually reads.
$labKeysAllowed = '^\s*(export\s+)?(MATRIX_)?(SSH|SUDO)_PASSWORD\s*='

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

# --- Site profiles + scrubbed lab credential files --------------------------
# Saved passwords are DPAPI-encrypted for *your* Windows account
# (matrix_deploy\secret_store.py), which no coworker can decrypt. Shipped lab
# SSH/sudo passwords are decrypted here; each copy re-encrypts them for its
# own user on first launch.
Add-Type -AssemblyName System.Security
$dpapiEntropy = [Text.Encoding]::UTF8.GetBytes("matrix-facility-manager")
function Unprotect-EnvLine([string]$line) {
  if ($line -notmatch '^(\s*(?:export\s+)?[^=]+=)\s*["'']?dpapi:([A-Za-z0-9+/=]+)["'']?\s*$') { return $line }
  $keyPart = $Matches[1]
  $bytes = [Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($Matches[2]), $dpapiEntropy, 'CurrentUser')
  $value = [Text.Encoding]::UTF8.GetString($bytes)
  if ($value -ne $value.Trim() -or $value -match '^(["'']).*\1$') { $value = if ($value.Contains('"')) { "'$value'" } else { "`"$value`"" } }
  return $keyPart + $value
}
$cfgOut = Join-Path $distDir "config"
New-Item -ItemType Directory -Force $cfgOut | Out-Null
$profiles = Get-ChildItem "config\*.json" | Where-Object { $_.Name -ne "deploy_config.example.json" }
if (-not $profiles) { throw "No site profiles in config\ - nothing for coworkers to connect to." }
foreach ($p in $profiles) {
  Copy-Item $p.FullName $cfgOut
  $labEnv = [IO.Path]::ChangeExtension($p.FullName, ".env")
  if (Test-Path $labEnv) {
    $kept = @("# Lab SSH/sudo passwords for $($p.BaseName) (shared lab credentials).") +
      @(Get-Content $labEnv | Where-Object { $_ -match $labKeysAllowed } | ForEach-Object { Unprotect-EnvLine $_ })
    Set-Content -Path (Join-Path $cfgOut (Split-Path $labEnv -Leaf)) -Value $kept -Encoding UTF8
    Write-Host "  profile $($p.Name) + $(Split-Path $labEnv -Leaf) (SSH/sudo keys only)"
  } else {
    Write-Host "  profile $($p.Name) (no lab .env - coworkers will be asked for passwords)"
  }
}
Copy-Item "config\deploy_config.example.json" $cfgOut
Copy-Item ".env.example" (Join-Path $distDir ".env.example")
# Windows device health check: copied on its own to field devices (never the
# rest of this folder - it holds lab passwords).
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

4. Pick your lab from the "Site" dropdown at the top-left.

5. Optional: set your own SWU download folder (and repo/dist folders if
   you use the Web App tab) under Settings > Local folders > Save folders.

This zip contains the shared lab SSH/sudo passwords in plain text
(config\<lab>.env). Only share it with people who should have that access,
and delete the .zip once you've extracted it. On first launch the app
encrypts them for your Windows account.

Passwords and tokens you enter are saved encrypted (Windows DPAPI) in this
folder, readable only by your Windows account on this computer. Still don't
share the folder after you've entered your own tokens.

Troubleshooting
  - "Lab network" warning: connect to the lab network/VPN.
  - Windows SmartScreen "protected your PC": click More info > Run anyway.
  - It doesn't start: the console window shows why and stays open until
    you press Enter.
  - The exe disappears or gets blocked by security software: don't
    restore it yourself. Contact IT.
"@ | Set-Content -Path (Join-Path $distDir "START HERE.txt") -Encoding UTF8

# --- Safety net: no personal tokens may ship --------------------------------
$leaks = Get-ChildItem $distDir -Recurse -File -Force |
  Where-Object { $_.FullName -notlike "*\_internal\*" -and $_.Name -ne ".env.example" } |
  Select-String -Pattern '^\s*(export\s+)?(ARTIFACTORY_TOKEN|ARTIFACTORY_API_KEY|JENKINS_TOKEN)\s*=\s*\S'
if ($leaks) { throw "Refusing to package: personal token found in $($leaks[0].Path)" }
if (Test-Path (Join-Path $distDir ".env")) { throw "Refusing to package: a root .env ended up in the dist folder." }
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
