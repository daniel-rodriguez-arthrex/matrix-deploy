"""FAQ / reference content for the Matrix Deploy GUI.

Kept Qt-free and as plain data so the reference page can be rendered by the GUI
(or reused elsewhere) and edited without touching UI code. Each section is a
(title, list-of-(question, answer_html)) pair. Answers may contain a small,
safe subset of HTML (``<code>``, ``<b>``, ``<ul>``/``<li>``, ``<br>``).

To grow the FAQ, just add entries here - the GUI renders and makes them
searchable automatically.
"""

from __future__ import annotations

from typing import List, Tuple

# (section title, [(question, answer_html), ...])
FAQ_SECTIONS: List[Tuple[str, List[Tuple[str, str]]]] = [
    (
        "Getting Started",
        [
            (
                "What do I need to configure before I can run anything?",
                "Open the <b>Settings</b> tab and fill in at least the "
                "<b>Router IP</b> and <b>SSH Username</b> - these are required "
                "for any room operation. For config deployments and most "
                "system actions you also need the <b>Sudo Password</b>. "
                "Downloads need your <b>Artifactory email + token</b>; builds "
                "need your <b>Jenkins username + token</b>.",
            ),
            (
                "What's the normal deployment workflow?",
                "<ul>"
                "<li>Fill in <b>Settings</b> (router IP, SSH user, sudo password).</li>"
                "<li>On <b>Deploy</b>, either <b>Browse</b> for an SWU file or "
                "click <b>Download Latest</b> (or <b>Build New</b> to trigger a Jenkins build).</li>"
                "<li>For config, select a base <b>Config Template</b> JSON.</li>"
                "<li>Check the <b>Operating Rooms</b> to target.</li>"
                "<li>Pick the <b>Operation</b> (SWU / Config / Both) and <b>Concurrency</b>.</li>"
                "<li>Click <b>Start Deployment</b> and confirm the prompt.</li>"
                "</ul>",
            ),
            (
                "Are my passwords and tokens saved to disk?",
                "No. The app never writes the SSH password, sudo password, "
                "Artifactory token, or Jenkins token to disk. Only non-secret "
                "fields (router IP, username, last file paths, Artifactory "
                "email) are saved to <code>~/.matrix_deploy_settings.json</code>. "
                "You can opt in to prefilling secrets from a gitignored "
                "<code>.env</code> file (plaintext - trusted machines only).",
            ),
        ],
    ),
    (
        "Concurrency & the Shared Host",
        [
            (
                "How does Concurrency work?",
                "It controls how many selected rooms run at once for "
                "deployments and system actions. Rooms beyond the limit queue "
                "and start as earlier ones finish. <b>3 at a time</b> is the "
                "default - it balances throughput against router/uplink "
                "bandwidth and per-room SSH/SCP overhead.",
            ),
            (
                "Why is concurrency sometimes forced to sequential?",
                "If <code>same_physical_host</code> is set in the config (every "
                "\"room\" is a different SSH port on the same box), simultaneous "
                "SWU installs would compete for <code>/tmp</code> extraction "
                "space and trigger overlapping reboots, so deployment is forced "
                "to 1-at-a-time. Actions touching local shared state (e.g. "
                "<b>Remove Fingerprint</b>) are also forced sequential.",
            ),
        ],
    ),
    (
        "Services & System Actions",
        [
            (
                "What do the Service buttons do?",
                "<ul>"
                "<li><b>Restart matrix-api</b> / <b>Restart NMS</b> - restart the "
                "<code>matrix-api</code> / <code>barco-nms</code> service.</li>"
                "<li><b>Status: matrix-api</b> / <b>Status: NMS</b> - show "
                "<code>systemctl status</code> (read-only).</li>"
                "<li><b>Stop matrix-api</b> / <b>Stop NMS</b> - stop the service.</li>"
                "<li><b>Reboot</b> / <b>Shutdown</b> - reboot or power off the room.</li>"
                "</ul>",
            ),
            (
                "What does matrix-api-certs do?",
                "Disables the cert-init/unseal units, regenerates the "
                "self-signed matrix-api server cert/key, fixes ownership and "
                "permissions, and restarts <code>matrix-api</code> on the "
                "selected rooms.",
            ),
            (
                "What does Fix IP Race Condition do?",
                "Patches <code>matrix-room-config-generator.service</code> so it "
                "waits on <code>barco-nms-network-init.service</code> before "
                "starting, fixing a race where it could grab the wrong/stale IP "
                "address. Takes effect on next reboot; safe to re-run.",
            ),
            (
                "What does Log Level: Debug do?",
                "Sets <code>logConfig.streams[].level</code> to <code>debug</code> "
                "in <code>matrix.api.config.json</code> and restarts "
                "<code>matrix-api</code> on the selected rooms.",
            ),
        ],
    ),
    (
        "Bandwidth & Overlay",
        [
            (
                "Bandwidth: MAX vs LIMITED?",
                "Pushes the golden <code>nms-config.json</code> with "
                "<code>videoSourceSharing.bandwidth</code> set to <b>MAX</b> or "
                "<b>LIMITED</b>.",
            ),
            (
                "Interop BW: 50000 vs 500000?",
                "Pushes <code>application-user.yml</code> with "
                "<code>interor.bandwidth</code> upload/download set to the given "
                "value (kbps) and restarts <code>barco-nms</code>.",
            ),
            (
                "What does Remove Overlay do?",
                "Pushes <code>application-user.yml</code> with "
                "<code>nexxis.overlay.noVideoOverlayId = matrixEmptyOverlay</code> "
                "and restarts <code>barco-nms</code>.",
            ),
        ],
    ),
    (
        "Diagnostics & Logs",
        [
            (
                "Get Logs / View matrix.api.config?",
                "<b>Get Logs</b> downloads matrix-api and barco-nms logs for the "
                "selected rooms to your Downloads folder. <b>View "
                "matrix.api.config</b> prints each room's "
                "<code>matrix.api.config.json</code> in the Output terminal and "
                "saves a raw copy to Downloads.",
            ),
            (
                "How do I watch logs live?",
                "Use <b>Watch matrix-api Live</b> or <b>Watch NMS Live</b>. Each "
                "opens one tab per selected room streaming the journal "
                "(<code>journalctl -f</code>). It's read-only and safe to run "
                "alongside other jobs on the same room - click <b>Stop</b> on the "
                "tab to end it.",
            ),
            (
                "How do I get a room's NMS admin password / GUI?",
                "Click <b>Open GUI</b> on a room row to open its NMS demonstrator "
                "GUI in the browser and copy the admin password to your "
                "clipboard (requires the sudo password). <b>Get NMS Password</b> "
                "runs <code>sudo act-mfg-eeprom display</code> for the selected "
                "rooms.",
            ),
        ],
    ),
    (
        "Troubleshooting",
        [
            (
                "\"REMOTE HOST IDENTIFICATION HAS CHANGED\" / host key errors",
                "Click <b>Remove Fingerprint</b> for the affected rooms. It "
                "removes the cached SSH host key from your local "
                "<code>known_hosts</code> file. It's local-only and does not "
                "connect to the room.",
            ),
            (
                "Build New fails or asks for credentials",
                "The Jenkins field wants your <b>Jenkins login ID</b> (e.g. "
                "\"Daniel Rodriguez\"), <b>not</b> your email. Check "
                "<code>https://jenkins-embedded.dev.actsw.net</code> under your "
                "account/profile if unsure, and make sure the Jenkins token is set.",
            ),
            (
                "A room says it's busy",
                "A room can only have one active (locking) job at a time. Wait "
                "for the running job to finish or cancel it. Read-only live log "
                "tails do not lock the room.",
            ),
            (
                "SWU update seems stuck after install",
                "After a successful SWU update the tool waits for the host to "
                "reboot and accept SSH again before deploying config. Success is "
                "detected from the <code>SWUPDATE successful</code> message, not "
                "the exit code.",
            ),
        ],
    ),
]
