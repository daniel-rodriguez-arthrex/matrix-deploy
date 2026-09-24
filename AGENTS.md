# Matrix Deploy - agent notes

- Run web UI (dev): `python run_server.py` (binds 127.0.0.1:8420). Setup Check only: `python run_server.py --check`.
- Build distributable: `powershell -ExecutionPolicy Bypass -File .\build_exe.ps1` -> `dist\MatrixDeploy\` + `dist\MatrixDeploy.zip`.
  - Builds in `%TEMP%\matrixdeploy-pyinstaller` because OneDrive locks files in the repo (PyInstaller/zip "Access is denied").
  - Only SSH/SUDO password keys from `config\<lab>.env` ship; the root `.env` (personal Artifactory/Jenkins tokens) must never ship.
- Verify: `node --check matrix_deploy/web/static/app.js`, `python -m py_compile` on changed files, then extract the zip to a temp folder and run `MatrixDeploy.exe --check` (the exe is windowed; from a terminal the report prints there, otherwise it's in a message box and `matrixdeploy.log`).
- The packaged exe auto-quits when its last window closes (presence heartbeats in app.js + `Presence` in server.py; running jobs keep it alive). `python run_server.py` does not auto-quit unless frozen.
- Don't print `.env` / `config/*.env` values. They contain plaintext secrets.
- The web UI is the only UI. The PyQt5 app was removed; it's preserved at tag `v2-last-pyqt`. When adding or changing a button, update the FAQ in `matrix_deploy/web/static/faq.js`.
- The GitHub remote was public. Don't push until it's private or moved to an internal host.
