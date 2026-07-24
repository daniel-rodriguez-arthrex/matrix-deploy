# Matrix Deploy

A modular GUI tool for deploying SWU firmware updates and configuration files to
Matrix operating rooms over SSH/SCP.

## Project Structure

```
matrix-deploy/
├── run_gui.py                  # Entry point
├── requirements.txt
├── config/
│   └── deploy_config.json      # Rooms, connection, Artifactory settings (edit me)
└── matrix_deploy/
    ├── config.py               # Config loading + data models (Qt-free)
    ├── ssh_client.py           # SSH/SCP helpers + wait-for-reboot (Qt-free)
    ├── artifactory.py          # Download latest SWU build (Qt-free)
    ├── config_builder.py       # Per-room JSON generation (Qt-free)
    ├── deployer.py             # SWU + config deployment logic (Qt-free)
    ├── workers.py              # Qt threads wrapping the logic
    └── gui.py                  # Qt UI only
```

**Design principle:** All deployment logic is Qt-free and lives in the service
modules. Only `workers.py` and `gui.py` depend on PyQt5, so the core is testable
and reusable from a future CLI.

## Installation

```powershell
pip install -r requirements.txt
```

## Usage

```powershell
python run_gui.py
```

### Workflow

1. Fill in **Connection Settings** (router IP, SSH user, sudo password).
2. For downloads, enter your **Artifactory email + token**.
3. Either **Browse** for an SWU file or click **Download Latest**.
4. For config deployment, select a base **Config Template** JSON.
5. Check the **Operating Rooms** to target.
6. Choose the **Operation** (SWU / Config / Both).
7. Keep **Deploy sequentially** checked (see note below) and click **Start Deployment**.
8. Use **Cancel** to abort cleanly between steps.

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

Similarly, `matrix_deploy/template/*.json` (your real per-room "Config
Template" files, which may contain live credentials) are gitignored. A
sanitized `matrix_deploy/template/example.config.json` is committed to show
the expected structure - copy and fill it in for your own environment.

### Optional `.env` prefill

To avoid re-typing fields each launch, copy `.env.example` to `.env` and set
values. On startup these prefill the matching GUI fields and take precedence
over the saved settings file. `.env` is gitignored.

Non-secret keys:

- `ROUTER_IP`
- `SSH_USERNAME`
- `ARTIFACTORY_EMAIL`
- `SWU_FILE` (optional default path)
- `CONFIG_TEMPLATE` (optional default path)

Secret keys (optional, **plaintext on disk** - leave blank to opt out):

- `SSH_PASSWORD`
- `SUDO_PASSWORD`
- `ARTIFACTORY_TOKEN`

If you set the secret keys, they prefill the GUI password fields each launch.
The app still never writes them to its settings file, and `.env` is gitignored
- but they do live in plaintext in `.env`, so only use this on a trusted
machine.

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
| `matrix_deploy/template/*.json` (except `example.config.json`) | Real per-room config files, which embed the live NMS admin password and any RUM/Datadog tokens for your environment |
| `build/`, `dist/` | PyInstaller output |

Before pushing to a public remote, always run `git status` and confirm none
of the above show up as tracked/staged.

## Notes on the SWU process

- Updates install via `swupdate-client` (more reliable than the HTTP upload).
- Success is detected from the `SWUPDATE successful` message, not the exit code.
- After a successful update the tool waits for the host to reboot and accept SSH
  again before deploying config.
- The verbose `Keeping file ...` overlay-cleanup output is filtered from the log.
