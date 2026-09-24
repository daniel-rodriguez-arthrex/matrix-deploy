/* FAQ content for the Matrix Deploy web UI (FAQ tab).
 *
 * Plain data so it can be edited without touching UI code: each section is
 * { title, items: [{ q, a }] }. Answers are trusted, hand-written HTML (a small
 * subset: <b>, <code>, <ul>/<li>, <br>). The FAQ tab renders and searches
 * these automatically - just add entries.
 */
window.FAQ_SECTIONS = [
  {
    title: "Getting Started",
    items: [
      {
        q: "I just got the MatrixDeploy folder. What do I do first?",
        a: "<ul>" +
          "<li>Extract the whole zip somewhere you own (Documents or Desktop), not Program Files, and don't run it from inside the zip.</li>" +
          "<li>Double-click <code>MatrixDeploy.exe</code>. A console window runs a <b>Setup Check</b> and then opens this page in your browser.</li>" +
          "<li>Keep the console window open while you use the app. Closing it stops the app.</li>" +
          "<li>Go to <b>Settings &rarr; Setup Check</b>. Fix anything red with the button next to it. Amber items only affect optional features.</li>" +
          "</ul>",
      },
      {
        q: "What does the Setup Check look at?",
        a: "Whether the app folder is writable, the bundled app files are present, a site profile loads (and has no example values left), " +
          "the lab SSH/sudo passwords are saved, your Artifactory and Jenkins credentials are set (optional), git/npm exist if you set " +
          "build-from-source repos (optional), and whether the lab router answers. A red banner appears at the top of every tab while " +
          "something blocking is wrong. Click <b>Re-run check</b> after fixing things. You can also run <code>MatrixDeploy.exe --check</code> in a terminal.",
      },
      {
        q: "How do I switch between labs?",
        a: "Use the <b>Site</b> dropdown at the top of the sidebar. Each lab is a profile in the <code>config</code> folder " +
          "(e.g. <code>qa1lab.json</code>) with its own passwords in the matching <code>qa1lab.env</code>. Switching reloads the rooms, " +
          "connection details and saved passwords for that lab.",
      },
      {
        q: "Where do I get my Artifactory and Jenkins tokens?",
        a: "Use <b>your own</b> account. Never borrow someone else's token, because everything you do is logged under that name." +
          "<ul>" +
          "<li><b>Artifactory:</b> log into <code>https://artifactory.dev.actsw.net</code> with Arthrex SSO, open your profile " +
          "(top-right avatar &rarr; <b>Edit Profile</b>) and generate an <b>Identity Token</b>. Your username is your Arthrex email.</li>" +
          "<li><b>Jenkins:</b> log into <code>https://jenkins-embedded.dev.actsw.net</code>, click your name (top-right) &rarr; " +
          "<b>Configure</b> / <b>Security</b> &rarr; <b>Add new Token</b>. Your <b>Jenkins username</b> is the login ID on that " +
          "profile page, usually your full name and <b>not</b> your email.</li>" +
          "</ul>" +
          "Enter them under <b>Settings &rarr; Edit saved credentials</b> (open the <i>Artifactory &amp; Jenkins</i> section).",
      },
      {
        q: "Where are my passwords and tokens stored?",
        a: "Values you enter in the <b>Edit saved credentials</b> dialog are saved <b>in plain text</b> on this computer only: lab " +
          "SSH/sudo passwords in <code>config\\&lt;lab&gt;.env</code>, and Artifactory/Jenkins in <code>.env</code> next to " +
          "<code>MatrixDeploy.exe</code>. Anything you type directly into the sidebar or tab fields stays in memory and is gone when you " +
          "close the app. Once you've added your own tokens, <b>don't pass your folder on</b>. Share the original zip instead.",
      },
      {
        q: "I double-clicked MatrixDeploy.exe again and no new window appeared.",
        a: "The app is already running, so a second launch just reopens the browser tab for the running copy. To restart it, " +
          "close the existing console window first.",
      },
    ],
  },
  {
    title: "Deploying Firmware (SWU)",
    items: [
      {
        q: "What's the normal SWU deployment workflow?",
        a: "<ul>" +
          "<li>Pick your lab in <b>Site</b> and tick the target rooms in the sidebar.</li>" +
          "<li>On <b>Deploy</b>, click <b>Download Latest SWU</b> and choose the build source, or <b>Browse</b> to an SWU file you already have.</li>" +
          "<li>Choose a concurrency (<b>Sequential</b>, <b>2&times;</b>, <b>3&times;</b>, <b>All</b>) and click <b>Start Deployment</b>.</li>" +
          "<li>Each room gets the file uploaded, installs it with <code>swupdate-client</code>, reboots, and is marked done once SSH answers again.</li>" +
          "</ul>" +
          "Progress for each job shows in its own console tab at the bottom. <b>Cancel</b> stops cleanly between steps.",
      },
      {
        q: "How does concurrency work? Why does it sometimes run one room at a time anyway?",
        a: "Concurrency is how many rooms work at once. Extra rooms wait in a queue. SWU <b>uploads</b> are always one at a time, because " +
          "every room shares the router's uplink. If the site profile sets <code>same_physical_host</code> (every room is a different SSH " +
          "port on one box), everything runs sequentially so installs don't fight over <code>/tmp</code> or reboot on top of each other. " +
          "<b>Remove SSH Fingerprint</b> is always sequential because it edits one local file.",
      },
      {
        q: "How do I kick off a new build?",
        a: "Use <b>Deploy &rarr; Jenkins Build &rarr; Trigger Build</b>. It starts a new Embedded Builder build (matrix / wrynose) " +
          "using your Jenkins username and token. When it finishes in Jenkins, use <b>Download Latest SWU</b> to pull it.",
      },
      {
        q: "The SWU install looks stuck after it finished installing.",
        a: "After a successful install the room reboots, and the tool waits until SSH comes back before marking that room done. " +
          "Success is detected from the <code>SWUPDATE successful</code> message, not the exit code. A reboot taking a few minutes is normal.",
      },
    ],
  },
  {
    title: "Editing Room Config",
    items: [
      {
        q: "How do I change a room's matrix.api.config.json?",
        a: "Open <b>Config</b>, pick a room and click <b>Load</b> to pull its live file into the editor. Then:" +
          "<ul>" +
          "<li><b>Deploy &amp; Restart</b> validates the JSON, pushes the whole file back to <i>that</i> room and restarts <code>matrix-api</code>.</li>" +
          "<li><b>Apply Changes to Selected Rooms</b> sends <b>only the fields you changed</b> to every room ticked in the sidebar. " +
          "Each room keeps its own room-specific values, and list edits such as <code>trustedEndPoints</code> are added or removed item by item.</li>" +
          "</ul>" +
          "If the room picker no longer matches the loaded room, Deploy is disabled until you Load again, so you can't push one room's file to another.",
      },
    ],
  },
  {
    title: "Web App",
    items: [
      {
        q: "Which Web App mode should I use?",
        a: "<ul>" +
          "<li><b>Deploy existing build</b>: point <b>Backend dist folder</b> and <b>Web assets folder</b> at build output you already have.</li>" +
          "<li><b>Build + deploy</b>: runs <code>git pull</code>, <code>npm install</code> and <code>npm run build</code> in your local " +
          "<code>matrix-api-linux</code> / <code>matrix-app-linux</code> checkouts, then deploys. Needs Git and Node.js on this computer.</li>" +
          "<li><b>Build only</b>: the build step without touching any room.</li>" +
          "</ul>" +
          "A backend/web major-version mismatch is logged as a warning, not an error.",
      },
      {
        q: "What do Configure / Reset / Diagnose Web App do?",
        a: "<b>Configure Web App</b> points <code>apiServer</code> at the installed web app folders, sets the pairing key, and trusts every " +
          "room's external API origin, all in one edit plus a <code>matrix-api</code> restart. <b>Reset Web App</b> undoes a previous deploy " +
          "(removes staged/deployed files and restores the original systemd entrypoint). <b>Diagnose Web App</b> dumps web asset listings, " +
          "config values, service status and AppArmor denials.",
      },
      {
        q: "A room's web app or module scripts return 403 through the router.",
        a: "The browser's <code>Origin</code> (<code>https://&lt;router ip&gt;:1000N</code>) isn't in <code>apiServer.trustedEndPoints</code>. " +
          "Run <b>Configure Web App</b> on the affected rooms. It adds every room's origin and restarts <code>matrix-api</code>. " +
          "For a one-off host, use <b>Actions &rarr; Config Fixes &rarr; Add Endpoint</b>. If it still fails, check AppArmor denials with <b>Diagnose Web App</b>.",
      },
      {
        q: "How do I open a room's web app or NMS Demonstrator?",
        a: "Use the <b>App</b> and <b>NMS</b> buttons on the room's row in the sidebar. <b>NMS</b> copies that room's NMS login password " +
          "to your clipboard first (needs the sudo password), then opens the demonstrator login page.",
      },
    ],
  },
  {
    title: "Actions",
    items: [
      {
        q: "What do the Services buttons do?",
        a: "Pick <b>Matrix API</b> or <b>Barco NMS</b>, then <b>Status</b> (<code>systemctl status</code>, read-only), " +
          "<b>Stream Live</b> (follows the journal with <code>journalctl -f</code> until you cancel), <b>Restart</b> or <b>Stop</b>.",
      },
      {
        q: "What do the NMS Configuration buttons do?",
        a: "<ul>" +
          "<li><b>Max / Limited Bandwidth</b>: pushes the golden <code>nms-config.json</code> with <code>videoSourceSharing.bandwidth</code> set to MAX or LIMITED.</li>" +
          "<li><b>Apply Link Bandwidth</b>: sets inter-OR upload/download bandwidth (kbps) in <code>application-user.yml</code> and restarts <code>barco-nms</code>.</li>" +
          "<li><b>Remove Video Overlay</b>: sets <code>nexxis.overlay.noVideoOverlayId = matrixEmptyOverlay</code> and restarts <code>barco-nms</code>.</li>" +
          "</ul>",
      },
      {
        q: "What do the Config Fixes do?",
        a: "<ul>" +
          "<li><b>Set Log Level: Debug</b>: sets the <code>logConfig</code> stream levels to <code>debug</code> and restarts <code>matrix-api</code>.</li>" +
          "<li><b>Fix Room Config Race</b>: makes <code>matrix-room-config-generator</code> wait for <code>barco-nms-network-init</code>, " +
          "so the room stops grabbing the wrong IP at boot. Safe to re-run, and takes effect on the next reboot.</li>" +
          "<li><b>Regenerate API Certs</b>: regenerates the <code>matrix-api</code> TLS certificates.</li>" +
          "<li><b>Add Endpoint</b>: adds one host to <code>apiServer.trustedEndPoints</code>. Entries already present are skipped.</li>" +
          "</ul>",
      },
      {
        q: "What is Custom Command for?",
        a: "Runs one shell command on every selected room and prints the output per room. Tick <b>sudo</b> to run it as root. " +
          "Be careful: it runs exactly what you type, on every ticked room.",
      },
    ],
  },
  {
    title: "Logs & Diagnostics",
    items: [
      {
        q: "How do I collect logs?",
        a: "In <b>Logs</b>, pick a scope (Matrix services only, or the entire system journal) and a time range, then click " +
          "<b>Download Logs</b>. Files appear under <b>Downloads</b> on that tab. <b>Show Errors</b> prints just the error-level messages to " +
          "the console. <b>Support Bundle</b> produces the official get-support zip. It's large, needs sudo, and ignores the time range.",
      },
      {
        q: "How do I see or debug the Matrix App on a room's screen?",
        a: "In <b>Tunnels</b>, pick the room and click <b>Enable Remote Debugging</b> (relaunches the kiosk app with DevTools and needs sudo). " +
          "Then <b>View Matrix App (DevTools)</b> opens the inspector in a Chromium browser, and <b>Screenshot</b> saves a PNG of the screen. " +
          "Click <b>Disable Remote Debugging</b> when you're done. Open tunnels are listed under <b>Active Tunnels</b> and close when the app stops.",
      },
      {
        q: "What do Uptime / Disk Space / Specs show?",
        a: "<b>Uptime</b> is how long the room has been up. <b>Disk Space</b> is free space, including <code>/tmp</code> where SWUs are " +
          "extracted. <b>Specs</b> covers OS/kernel version, CPU, memory and root filesystem usage.",
      },
    ],
  },
  {
    title: "Troubleshooting",
    items: [
      {
        q: "Setup Check says \"Lab network\" can't reach the router.",
        a: "You're not on the lab network. Connect to the lab network or VPN, then click <b>Re-run check</b>. If only one room is unreachable, " +
          "it may just be powered off.",
      },
      {
        q: "\"REMOTE HOST IDENTIFICATION HAS CHANGED\" / host key errors",
        a: "Usually happens after a room is reflashed. Click <b>Actions &rarr; Power &amp; Maintenance &rarr; Remove SSH Fingerprint</b> for those rooms. " +
          "It only removes the cached key from your local <code>known_hosts</code> and never connects to the room. It needs the Windows OpenSSH client.",
      },
      {
        q: "Trigger Build fails or asks me to log in",
        a: "Check that the Jenkins username is your <b>login ID</b>, not your email, and that the token is set. If the error says Jenkins " +
          "redirected to a login page, open <code>https://jenkins-embedded.dev.actsw.net</code> in your browser, complete the Okta/MFA login, " +
          "then click <b>Trigger Build</b> again. Jenkins sometimes needs an active browser session on top of the token.",
      },
      {
        q: "Saving credentials fails, or my saved passwords disappeared.",
        a: "The app folder isn't writable, or you ran it from inside the zip (Windows extracts it to a temp folder that gets wiped). " +
          "Extract the zip to Documents or Desktop and run it from there. The Setup Check flags both cases.",
      },
      {
        q: "Windows says \"Windows protected your PC\" when I start it.",
        a: "The exe isn't code-signed. Click <b>More info &rarr; Run anyway</b>. If antivirus quarantines it, ask IT to allow it.",
      },
    ],
  },
];
