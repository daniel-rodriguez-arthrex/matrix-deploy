# Matrix Deploy

A localhost web tool for deploying SWU firmware updates to Matrix operating
rooms over SSH/SCP, running system/service actions, deploying the Matrix web
app, and editing each room's live `matrix.api.config.json` directly.

The PyQt5 desktop app was retired in favor of the web UI. It is preserved at
the git tag `v2-last-pyqt`.

## Project Structure

```
matrix-deploy/
├── run_server.py               # Entry point (also the MatrixDeploy.exe entry point)
├── build_exe.ps1               # Builds the distributable folder + zip
├── requirements.txt
├── config/
│   ├── deploy_config.example.json
│   ├── <lab>.json              # Site profiles: rooms, connection, Artifactory (gitignored)
│   └── <lab>.env               # Per-lab SSH/sudo passwords (gitignored)
└── matrix_deploy/
    ├── config.py               # Profile loading + data models
    ├── env_settings.py         # .env loading/saving
    ├── preflight.py            # Setup Check
    ├── ssh_client.py           # SSH/SCP helpers + wait-for-reboot
    ├── artifactory.py          # Download latest SWU build
    ├── jenkins.py              # Trigger Embedded Builder builds
    ├── deployer.py             # SWU deploy + live config + actions
    ├── webapp_builder.py       # Build the web app/backend from source
    └── web/                    # FastAPI server + static UI (FAQ content in static/faq.js)
```

All deployment logic lives in the core modules. `web/` is a thin HTTP/WebSocket
layer over them.

**Config workflow:** there are no golden templates. Each room's
`matrix.api.config.json` is edited live — load it from the room, edit it, then
push it back and restart matrix-api (Config tab in the web UI).

## Installation

```powershell
pip install -r requirements.txt
```

## Usage

Start it. It binds to 127.0.0.1 only and opens a Chrome/Edge app window:

```powershell
python run_server.py
```

When run from source it keeps running until Ctrl+C. The packaged exe quits by
itself once its last window is closed and no job is running (running jobs
finish first; live log streams are stopped).

Setup Check only (prints a report, exits 1 if something must be fixed):

```powershell
python run_server.py --check
```

## Distributing to coworkers (no Python needed)

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

This builds the web UI into `dist\MatrixDeploy\` and zips it to
`dist\MatrixDeploy.zip`. Share the zip. The folder contains:

| Item | Notes |
|---|---|
| `MatrixDeploy.exe`, `_internal\` | The app. Double-click to start. It opens in its own Chrome/Edge app window, with no console. |
| `config\<lab>.json` | Every site profile in your `config\` folder. |
| `config\<lab>.env` | **Only** `SSH_PASSWORD`/`SUDO_PASSWORD` (and `MATRIX_` aliases). All other keys in your lab `.env` are dropped. |
| `START HERE.txt` | Quick start for coworkers. |

Your root `.env` (personal Artifactory/Jenkins tokens) is **never** copied.
The build fails if a token shows up anywhere in the output. Each coworker enters
their own tokens in the app. They're saved to `.env` next to the exe on that
coworker's machine.

On every launch a **Setup Check** runs and shows under **Settings > Setup
Check**, with a red banner when something is blocking. If the app can't start
at all (e.g. no site profile), a message box explains why. The exe logs to
`matrixdeploy.log` next to itself. It checks:

- The app folder is writable and isn't running from inside a zip.
- The bundled UI/golden files are present.
- At least one site profile loads, with no placeholder values left.
- Lab SSH/sudo passwords are set.
- Artifactory/Jenkins credentials are set (optional).
- git/npm are installed, if build-from-source repos are configured (optional).
- The lab router is reachable.

Blocking items show a one-click fix button where one applies. Launching the exe
a second time opens another window onto the running instance instead of
starting a second one.

When frozen, `config\` and `.env` are always read from the folder that holds
`MatrixDeploy.exe`, never from inside `_internal\`.

### Workflow

1. Pick the lab in the **Site** dropdown and check **Settings > Setup Check**.
2. Tick the target rooms in the sidebar.
3. On **Deploy**, click **Download Latest SWU** or **Browse** to an SWU file.
4. Choose a concurrency and click **Start Deployment**.
5. Use **Config** to edit a room's live config. **Actions**, **Logs**,
   **Web App** and **Tunnels** cover everything else. The **FAQ** tab explains
   every button.
6. **Cancel** aborts cleanly between steps.

## Configuration

Each lab is a **site profile** in `config/`: any `*.json` with `connection` and
`rooms` sections (e.g. `qa1lab.json`). The display name comes from `site.name`.
Profiles are **gitignored** because they contain internal network addresses.
Start from the example:

```powershell
Copy-Item config\deploy_config.example.json config\mylab.json
```

- `connection`: router IP, SSH user, port base, service names, SWU port,
  `same_physical_host` (see below), and the remote web-app paths.
- `artifactory`: URL, repo, build path/name, branch filter, optional `branches`.
- `rooms`: the room registry (number, room_id, display name, optional overrides).

No code changes are needed to retarget a different environment.

### Credentials (`.env` files)

| File | Holds | Written by |
|---|---|---|
| `config/<lab>.env` | That lab's `SSH_PASSWORD` / `SUDO_PASSWORD` (`MATRIX_*` aliases accepted) | The **Edit saved credentials** dialog |
| `.env` (next to `run_server.py` / `MatrixDeploy.exe`) | `ARTIFACTORY_EMAIL` / `ARTIFACTORY_TOKEN`, `JENKINS_USERNAME` / `JENKINS_TOKEN` | The **Edit saved credentials** dialog |
| same `.env` | This computer's folders: `SWU_DOWNLOAD_DIR`, `SWU_FILE`, `BACKEND_REPO`, `WEB_REPO`, `WEBAPP_DIST`, `WEBAPP_WEB` | **Settings → Local folders** |

See `.env.example` for all keys. Values in these files are **plaintext on
disk** and prefill the UI on launch. Leave them blank to type credentials into
the UI each session, where they stay in memory only.

## Web App Build & Deploy

The **Web App** tab (web UI) deploys the Matrix Electron web app + `matrix.api`
backend to each selected room over SSH, restarting `matrix-api` when done -
ported from the standalone `matrix-electron-web-deployer` tool.

- **Build from source** (optional): runs `git pull` + `npm install` + `npm run
  build` in your local checkouts of the internal `matrix-api-linux` and
  `matrix-app-linux` repos. Requires Node.js/npm + git on this machine and
  those repos as siblings (access-restricted; not included here). Prefill the
  paths via `BACKEND_REPO`/`WEB_REPO` in `.env`.
- **Deploy already-built artifacts**: point **Backend dist folder** /
  **Web assets folder** at existing build output and skip the build step.
- A backend/web major-version mismatch is logged as a warning, not a hard
  error, since deploying a known-good combination may be intentional.
- **Reset Web App** undoes a previous deploy (removes staged/deployed files,
  restores the original systemd entrypoint). **Diagnose Web App** dumps web
  asset listings, config values, service status, and AppArmor denials.
- Remote paths (`/opt/matrix-api-app`, `/usr/lib/node_modules/matrix.api`,
  the systemd unit) are configurable per profile in `deploy_config.json`
  under `connection` if your OR image differs from the default layout.

## Important: Shared Physical Host

In some labs every "room" is a different **SSH port on the same box**
(`same_physical_host: true`). Two implications, both handled by the tool:

1. **Unique remote filenames** - each room's SWU uploads to
   `update-or{N}-<name>.swu` so parallel runs never clobber each other.
2. **Sequential deployment is the default** - simultaneous SWU installs would
   compete for `/tmp` extraction space and trigger overlapping reboots.

## Security

- The server binds to `127.0.0.1` only and is meant for a single local user.
- Credentials typed into the UI stay **in memory only**. Credentials saved via
  **Edit saved credentials** go to the `.env` files above in plaintext, so only
  do that on a trusted machine.
- The distributable build never includes the root `.env`, and strips lab `.env`
  files down to SSH/sudo passwords (see *Distributing to coworkers*).

### What's gitignored and why

The following are excluded from version control because they can contain
live secrets or site-specific network details - **never force-add or commit
these**:

| Path | Reason |
|---|---|
| `.env` | Plaintext Artifactory/Jenkins tokens, if saved |
| `config/*.env` | Plaintext per-lab SSH/sudo passwords |
| `*.key`, `*.pem` | Private keys / certs (e.g. NMS root CA material) |
| `config/*.json` (except the example) | Real router IP + per-room network addresses |
| `build/`, `dist/`, `*.spec` | PyInstaller output |

Before pushing, always run `git status` and confirm none of the above show up
as tracked/staged. Keep the remote **private**: the code itself describes
internal hosts and device procedures.

## Notes on the SWU process

- Updates install via `swupdate-client` (more reliable than the HTTP upload).
- Success is detected from the `SWUPDATE successful` message, not the exit code.
- After a successful update the tool waits for the host to reboot and accept SSH
  again before finishing.
- The verbose `Keeping file ...` overlay-cleanup output is filtered from the log.
