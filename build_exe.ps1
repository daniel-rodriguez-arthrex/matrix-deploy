# Matrix Deploy - build folder app (PyInstaller)

# Usage:
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1

$ErrorActionPreference = "Stop"

# Clean old build artifacts to avoid permission errors
$distDir = Join-Path $PSScriptRoot "dist\MatrixDeploy"
if (Test-Path $distDir) {
  Write-Host "Removing old build..."
  Remove-Item $distDir -Recurse -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 500
}

Write-Host "Installing PyInstaller..."
python -m pip install --upgrade pyinstaller

Write-Host "Building folder app..."
python -m PyInstaller --noconsole --name MatrixDeploy `
  --add-data "config\deploy_config.json;config" `
  --add-data "matrix_deploy\golden_files;matrix_deploy\golden_files" `
  --hidden-import "cryptography.hazmat.bindings._rust" `
  --hidden-import "cryptography.hazmat.bindings._openssl" `
  --collect-all cryptography `
  run_gui.py

$distDir = Join-Path $PSScriptRoot "dist\MatrixDeploy"
$envExample = Join-Path $PSScriptRoot ".env.example"
if (Test-Path $envExample) {
  $envTarget = Join-Path $distDir ".env"
  $envExampleTarget = Join-Path $distDir ".env.example"
  if (!(Test-Path $envTarget)) {
    Copy-Item $envExample $envTarget
  }
  Copy-Item $envExample $envExampleTarget -Force
  Write-Host "Copied .env.example to $envExampleTarget"
}

Write-Host "Done. App located at: .\dist\MatrixDeploy\MatrixDeploy.exe"
