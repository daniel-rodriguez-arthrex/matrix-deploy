# Matrix Deploy

A modular tool for deploying SWU firmware updates to Matrix operating rooms over
SSH/SCP, running system/service actions, and editing each room's live
`matrix.api.config.json` directly. Available as a PyQt5 desktop app and a
localhost web UI (see `ROADMAP_3.0.md`).

## Project Structure

```
matrix-deploy/
├── run_gui.py                  # Desktop GUI entry point
├── run_server.py               # Localhost web UI entry point
├── requirements.txt
├── config/
│   └── deploy_config.json      # Rooms, connection, Artifactory settings (edit me)
└── matrix_deploy/
    ├── config.py               # Config loading + data models (Qt-free)
    ├── ssh_client.py           # SSH/SCP helpers + wait-for-reboot (Qt-free)
    ├── artifactory.py          # Download latest SWU build (Qt-free)
    ├── deployer.py             # SWU deploy + live config + actions (Qt-free)
    ├── workers.py              # Qt threads wrapping the logic
    ├── gui.py                  # Qt UI only
    └── web/                    # FastAPI localhost web UI (Qt-free)
```

**Design principle:** All deployment logic is Qt-free and lives in the service
modules. Only `workers.py` and `gui.py` depend on PyQt5, so the core is testable
and reusable from the CLI and the web UI.

**Config workflow:** there are no golden templates. Each room's
`matrix.api.config.json` is edited live — load it from the room, edit it, then
push it back and restart matrix-api (Config tab in the web UI).

## Installation

```powershell
pip install -r requirements.txt
```

## Usage

Desktop GUI:

```powershell
python run_gui.py
```

Localhost web UI (binds to 127.0.0.1 only, opens your browser):

```powershell
python run_server.py
```

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
| `MatrixDeploy.exe`, `_internal\` | The app. Double-click to start. The console window must stay open while the app runs. |
| `config\<lab>.json` | Every site profile in your `config\` folder. |
| `config\<lab>.env` | **Only** `SSH_PASSWORD`/`SUDO_PASSWORD` (and `MATRIX_` aliases). All other keys in your lab `.env` are dropped. |
| `START HERE.txt` | Quick start for coworkers. |

Your root `.env` (personal Artifactory/Jenkins tokens) is **never** copied.
The build fails if a token shows up anywhere in the output. Each coworker enters
their own tokens in the app. They're saved to `.env` next to the exe on that
coworker's machine.

On every launch a **Setup Check** runs, both in the console and in the web UI
under **Settings > Setup Check**, with a red banner when something is blocking.
It checks:

- The app folder is writable and isn't running from inside a zip.
- The bundled UI/golden files are present.
- At least one site profile loads, with no placeholder values left.
- Lab SSH/sudo passwords are set.
- Artifactory/Jenkins credentials are set (optional).
- git/npm are installed, if build-from-source repos are configured (optional).
- The lab router is reachable.

Blocking items show a one-click fix button where one applies. Launching the exe
a second time reopens the running instance instead of starting a second one.

When frozen, `config\` and `.env` are always read from the folder that holds
`MatrixDeploy.exe`, never from inside `_internal\`.

### Workflow

1. Fill in **Connection Settings** / **Credentials** (SSH user, sudo password).
2. For downloads, enter your **Artifactory email + token**.
3. Either **Browse**/enter an SWU file path or click **Download Latest**.
4. Check the **Operating Rooms** to target.
5. Click **Start Deployment** (SWU firmware). Use the **Config** tab/editor to
   change a room's live config.
6. Use **Cancel** to abort cleanly between steps.

## Configuration

All environment-specific values live in `config/deploy_config.json`, which is
**gitignored** since it typically contains internal network addresses. Copy
the committed example to get started:

```powershell
Copy-Item config\deploy_config.example.json config\deploy_config.json
```

Then edit `config/deploy_config.json`:

- `connection` - router IP, SSH user, port base, service name, SWU port,
  and `same_physical_host` (see below).
- `artifactory` - URL, repo, build path, build name, branch filter.
- `rooms` - the room registry (number, room_id, display name).

No code changes are needed to retarget a different environment.

### Optional `.env` prefill

To avoid re-typing fields each launch, copy `.env.example` to `.env` and set
values. On startup these prefill the matching GUI fields and take precedence
over the saved settings file. `.env` is gitignored.

Non-secret keys:

- `ROUTER_IP`
- `SSH_USERNAME`
- `ARTIFACTORY_EMAIL`
- `SWU_FILE` (optional default path)

Secret keys (optional, **plaintext on disk** - leave blank to opt out):

- `SSH_PASSWORD`
- `SUDO_PASSWORD`
- `ARTIFACTORY_TOKEN`

If you set the secret keys, they prefill the GUI password fields each launch.
The app still never writes them to its settings file, and `.env` is gitignored
- but they do live in plaintext in `.env`, so only use this on a trusted
machine.

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

In this environment every "room" is a different **SSH port on the same box**
(`10.101.44.150`). Two implications, both handled by the tool:

1. **Unique remote filenames** - each room's SWU uploads to
   `update-or{N}-<name>.swu` so parallel runs never clobber each other.
2. **Sequential deployment is the default** - simultaneous SWU installs would
   compete for `/tmp` extraction space and trigger overlapping reboots.

## Security

- The app itself never writes SSH password, sudo password, or the Artifactory
  token to disk. The settings file (`~/.matrix_deploy_settings.json`) stores
  only non-secret fields (router IP, username, last file paths, Artifactory
  email).
- By default secrets stay **in memory only** (typed into the GUI each session).
- **Opt-in exception:** if you put them in `.env` (see *Optional `.env`
  prefill*), they live in plaintext in that gitignored file and prefill the GUI
  on launch. Only do this on a trusted machine.

### What's gitignored and why

The following are excluded from version control because they can contain
live secrets or site-specific network details - **never force-add or commit
these**:

| Path | Reason |
|---|---|
| `.env` | Plaintext SSH/sudo password + Artifactory token, if you opt in |
| `*.key`, `*.pem` | Private keys / certs (e.g. NMS root CA material) |
| `config/deploy_config.json` | Real router IP + per-room network addresses |
| `build/`, `dist/` | PyInstaller output |

Before pushing to a public remote, always run `git status` and confirm none
of the above show up as tracked/staged.

## Notes on the SWU process

- Updates install via `swupdate-client` (more reliable than the HTTP upload).
- Success is detected from the `SWUPDATE successful` message, not the exit code.
- After a successful update the tool waits for the host to reboot and accept SSH
  again before finishing.
- The verbose `Keeping file ...` overlay-cleanup output is filtered from the log.
