/* FAQ content for the Matrix Facility Manager web UI (FAQ tab).
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
          "<li>Double-click <code>MatrixDeploy.exe</code>. A small console window opens (just leave it or minimize it) and Matrix Facility Manager " +
          "opens in its own window. Closing the Matrix Facility Manager window quits the app, and the console closes by itself. " +
          "If a job is still running, it finishes first.</li>" +
          "<li>Go to <b>Settings &rarr; Setup Check</b>. Fix anything red with the button next to it. Amber items only affect optional features.</li>" +
          "</ul>",
      },
      {
        q: "What does the Setup Check look at?",
        a: "Whether the app folder is writable, the bundled app files are present, a site profile loads (and has no example values left), " +
          "the lab SSH/sudo passwords are saved, your Artifactory and Jenkins credentials are set (optional), git/npm exist if you set " +
          "build-from-source repos (optional), and whether the lab router answers. A red banner appears at the top of every tab while " +
          "something blocking is wrong. Click <b>Re-run check</b> after fixing things. If the app can't start at all, its console window explains why " +
          "and stays open until you press Enter.",
      },
      {
        q: "Why was I asked which team I'm on? Where are the Tunnels, Config, Software Update or Web App tabs?",
        a: "The first time the app opens on a computer it asks whether you're on <b>QA</b> or <b>Integration</b>. " +
          "It only changes which tabs you see: <b>Integration</b> hides the QA-only <b>Tunnels</b>, <b>Config</b>, <b>Software Update</b> " +
          "and <b>Web App</b> tabs, and the <b>NMS Configuration</b> card on <b>Actions</b>. Everything else is the same for both. Change it any time under <b>Settings &rarr; Your team</b>. " +
          "If a <b>QA password</b> is set, choosing QA asks for it (tick <i>Remember this computer</i> to not be asked again), and the app " +
          "refuses those features without it even if someone calls them directly. A QA team member sets or changes the password under " +
          "<b>Settings &rarr; Your team &rarr; QA password</b> on the computer that builds the app, then rebuilds it: the password check is built " +
          "into the app, so coworkers' copies can't change or remove it (and deleting files from the app folder doesn't unlock it). Every shared build " +
          "has it: the build refuses to run without a QA password, so the QA features are locked by default. Choosing Integration locks " +
          "the QA features on that computer again. " +
          "The choice is saved on this computer only.",
      },
      {
        q: "How do I switch between labs?",
        a: "Use the <b>Site</b> dropdown at the top of the sidebar. Each lab is a profile in the <code>config</code> folder " +
          "(e.g. <code>qa1lab.json</code>) with its own passwords in the matching <code>qa1lab.env</code>. Switching reloads the rooms, " +
          "connection details and saved passwords for that lab.",
      },
      {
        q: "How do I add a new lab (or update one) from the facility planner?",
        a: "Go to <b>Settings &rarr; Facility config</b>, click <b>Import facility config (.zip)</b> and pick the planner export " +
          "(<code>&lt;SITE&gt;-version&lt;N&gt;.zip</code>). A preview shows the site, version, router, rooms, switches and which passwords it " +
          "contains. Each site becomes its <b>own lab</b> in the <b>Site</b> list (<code>config\\&lt;site&gt;.json</code> + <code>&lt;site&gt;.env</code>). " +
          "Importing a newer version of the same site updates that lab and lists what changed. If another lab already uses the same router you " +
          "can update that one instead. Untick <b>QA test forwards</b> for a real facility whose router only has the planner's forwards." +
          "<ul>" +
          "<li>Passwords (Windows, NMS, Extron, site API key) go to the lab's <code>.env</code> on this computer only and never ship in the app zip. " +
          "The planner's Windows password also becomes the lab's SSH/sudo password unless one is already saved.</li>" +
          "<li>The router and switch passwords aren't in the export. Add them once under <b>Edit saved credentials &rarr; Router, switches &amp; Windows</b>.</li>" +
          "<li>The uploaded zip (incl. the NMS CA key) isn't kept.</li>" +
          "</ul>",
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
        q: "How do I set my own download folder and repo paths?",
        a: "Go to <b>Settings &rarr; Local folders</b>, paste your paths and click <b>Save folders</b>. They're saved to your " +
          "<code>.env</code> on this computer only and fill in the Software Update and Web App tabs from then on. Tip: in File Explorer, " +
          "Shift+right-click a folder &rarr; <b>Copy as path</b>. The SWU download folder defaults to " +
          "<code>&lt;your home&gt;\\Desktop\\latest-matrix-wrynose</code> and is created on first download. The repo and dist " +
          "folders are only needed for the Web App tab. The Setup Check warns if a saved folder no longer exists.",
      },
      {
        q: "Where are my passwords and tokens stored?",
        a: "Values you enter in the <b>Edit saved credentials</b> dialog are saved on this computer only, <b>encrypted for your Windows account</b>: lab " +
          "SSH/sudo passwords in <code>config\\&lt;lab&gt;.env</code>, and Artifactory/Jenkins in <code>.env</code> next to " +
          "<code>MatrixDeploy.exe</code>. Another Windows user or computer can't read them; if you copy the folder elsewhere, enter them again there. " +
          "Passwords that are still plain text (the lab passwords that come with the zip, or ones typed into a <code>.env</code> by hand) are " +
          "encrypted the next time the app starts. Saved passwords are never sent to the app's window: a box with a saved one just says " +
          "<i>Saved - leave blank to use it</i>, and the app uses the saved value when you leave it empty. Type into it to use a different one " +
          "for now. Anything you type directly into the sidebar or tab fields stays in memory and is gone when you " +
          "close the app. Share the original zip, not your own folder.",
      },
      {
        q: "It says \"This window isn't signed in to the running app\".",
        a: "Each start of the app makes a new sign-in key, so only the window it opens (and other programs can't) uses it. That page means " +
          "the window was opened some other way, e.g. a bookmark or an old window from before a restart. Double-click <b>MatrixDeploy.exe</b> " +
          "again: it opens a signed-in window of the app that's already running.",
      },
      {
        q: "How do I quit or restart Matrix Facility Manager?",
        a: "Close its window. It quits a few seconds later, or right after any running deploy/action finishes. It asks you first " +
          "if a job is running. Live log streams are simply stopped. Double-clicking <code>MatrixDeploy.exe</code> while it's already " +
          "open just opens another window onto the same copy. It never starts a second one.",
      },
    ],
  },
  {
    title: "Facility Status",
    items: [
      {
        q: "What does the Facility tab show?",
        a: "One card per room with:" +
          "<ul>" +
          "<li>Whether the <b>Matrix API</b> (<code>matrix-api</code>) and <b>Barco NMS</b> (<code>barco-nms</code>) services are running, plus uptime.</li>" +
          "<li>The installed <b>software</b> (the <b>SW</b> badge, e.g. <i>2.0.0 &middot; MatrixG2-2.0-15</i>; hover it for the full build). " +
          "It turns amber when a room runs a different build from most rooms, and the <b>Software</b> box in the summary shows how many builds " +
          "the site has (hover it to see which rooms run which).</li>" +
          "<li>The <b>encoders</b> and <b>decoders</b> the room's NMS sees, and how many are online.</li>" +
          "<li><b>Displays</b>: decoder outputs with a screen plugged in. Open <b>Devices</b> on a card to see each device's model, ID, firmware, " +
          "and which sources and displays are connected to it.</li>" +
          "<li>Whether the room's <b>NMS license</b> is active. Hover the license badge to see each licensed feature.</li>" +
          "<li><b>Temperatures and fans</b>: each encoder and decoder's CPU/FPGA temperature and fan speed (RPM and fan duty %) are in the " +
          "<b>Temp / Fans</b> column under <b>Devices</b>. A device with a problem keeps its <b>online</b> state and gets a " +
          "<b>needs attention</b> badge (hover it for the reason), and the flagged reading turns red. The <b>Hottest</b> badge on the card shows the room's highest reading and turns red " +
          "when NMS raises a hardware alert (e.g. <i>CPU temp too high</i>, <i>Fan speed too low</i>). Hover it for details.</li>" +
          "</ul>" +
          "<b>Check All Rooms</b> checks every room in the site. <b>Check Selected Rooms</b> checks only the rooms ticked in the sidebar. " +
          "Every room is also checked automatically when the app opens or you switch site, as long as the SSH password is filled in. The checks are read-only and nothing on the rooms changes. A summary also goes to the <b>General</b> console tab.",
      },
      {
        q: "What does \"Changed since\" on a room card mean?",
        a: "Each check is compared with the previous one for that room (even from an earlier session), and the card lists what's different: " +
          "health changes (e.g. <i>Healthy &rarr; Needs attention</i>), a <b>reboot</b>, a new <b>software</b> build, a service or license that " +
          "changed state, devices that appeared, disappeared, went offline/online or got new firmware, and issues that are new or resolved. " +
          "The <b>N changes</b> badge shows the count, and the list also goes to the <b>General</b> console tab. With nothing new, the card says " +
          "<i>no changes since &hellip;</i>. The previous results are kept in this browser only (per site, no passwords), so a different " +
          "computer or browser starts fresh.",
      },
      {
        q: "How do I share or save the Facility results?",
        a: "Use the buttons next to <b>Check Selected Rooms</b>. <b>Export report</b> downloads a Markdown file (paste it into a ticket, Teams or a wiki): " +
          "the summary, a table of rooms (state, software, uptime, services, license, device counts), each room's issues and changes, " +
          "and every device with its state, firmware and temperatures. <b>Export CSV</b> downloads the same rooms and devices as a spreadsheet, " +
          "one row per device. Both use the last check, and also appear under <b>Logs &rarr; Downloads</b> for this session.",
      },
      {
        q: "What makes a room Healthy, Needs attention, Error or Unreachable?",
        a: "<ul>" +
          "<li><b>Healthy</b>: both services are active, NMS answered, every device is online, licensed, needs no firmware upgrade and has no NMS hardware alerts (temperature, fan), and the NMS license is active.</li>" +
          "<li><b>Needs attention</b>: SSH works but something on that list doesn't. The card lists each problem.</li>" +
          "<li><b>Error</b>: SSH connected but the checks couldn't run.</li>" +
          "<li><b>Unreachable</b>: the room's SSH port (200 + room number on the router) didn't answer or the SSH password was rejected.</li>" +
          "</ul>",
      },
      {
        q: "The Facility tab shows services but no devices or license.",
        a: "Devices and license come from the room's Barco NMS. To log in, the tool reads the NMS password with " +
          "<code>sudo act-mfg-eeprom display</code>, so the <b>sudo password</b> must be filled in in the sidebar. NMS is reached through " +
          "the router on port <code>11000 + room number</code>. If the card says that port isn't reachable, check the <b>Network</b> tab. " +
          "Secondary rooms have no local NMS, so they only show services.",
      },
    ],
  },
  {
    title: "Network",
    items: [
      {
        q: "What does the Network tab show?",
        a: "<ul>" +
          "<li><b>Router</b>: how each room is reached from outside, through a port on the site router (outside port = base + room number). " +
          "One row per service, one square per room: <b>API / Cert</b> (10000+n) is the facility forward every site gets; <b>SSH</b> (200+n), " +
          "<b>SWU</b> (300+n) and <b>NMS</b> (11000+n) are the extra QA test access. Hover a square to see exactly where it goes. " +
          "<b>All forwards</b> (folded) has the same plan as a table.</li>" +
          "<li><b>Check live router</b> logs in to the router (read-only, it never enters config mode) and colours each square " +
          "<b>green</b> (forwarded), <b>red</b> (missing) or <b>amber</b> (the port is used by something else). The line above says how many " +
          "forwards are OK, and every problem is spelled out with what it breaks (e.g. <i>OR 4 SSH: port 204 isn't forwarded - this app can't SSH in</i>) " +
          "or where the port goes instead. Every other forward on the router is listed under <b>Other ports in use</b>, labelled with what " +
          "it points at (room DevTools, Extron, switch SSH...). It runs automatically the first time you open the tab when a router password is saved for the lab.</li>" +
          "<li><b>Switches</b>: each switch's ports coloured by room VLAN, uplink and backbone, like the facility planner, plus each VLAN's tag, " +
          "gateway IP and port range. This needs the lab's facility config (Settings &rarr; Facility config).</li>" +
          "<li><b>Check switches</b> logs in to every switch (read-only, through the router's switch SSH forwards) and shows which ports " +
          "have a link: lit ports are in use, faded ones are free, and an amber outline means something is plugged into an unassigned port. " +
          "A line under each switch counts the devices per room and its problems. <b>Click a port</b> for its details (VLAN, link speed, " +
          "the device on it and which other port the same device uses), or open <b>Devices</b> for the full list. " +
          "Switch to <b>By room</b> to see every room's PC, Extron, encoders and decoders in one table. " +
          "It runs right after the router check when you open the tab, and takes a few seconds. Needs the lab's switch password " +
          "(<b>Edit saved credentials &rarr; Router, switches &amp; Windows</b>).</li>" +
          "<li><b>Device names</b>: room PCs (.12), secondary PCs (.13), Extrons and the router come from the lab profile. Encoders and decoders " +
          "come from each room's Barco NMS: the name you gave the encoder's source or the decoder's display (e.g. <i>OR1 Display 1 - mna-240</i>), " +
          "with model and serial next to it, or just <i>Decoder MNA240</i> if nothing is named. Run the <b>Facility</b> check first; the list " +
          "updates when it finishes. " +
          "A device with two network cables is shown once with both ports (e.g. <code>11 + 12</code>). Barco gear that no room's NMS lists is " +
          "marked <i>Not registered in any room's NMS</i> (not adopted, or a spare).</li>" +
          "<li><b>Planned vs seen</b> (By room): each room's PC (from the lab profile) and Extrons (from the facility config) get a " +
          "&check; when a switch sees them at their planned IP, and encoders/decoders show how many of the room's NMS devices are on a switch port. " +
          "A planned PC that isn't seen is a problem (red). A planned Extron that isn't seen is only a note (amber): Extrons are optional and " +
          "the planner's list can be out of date, so check whether the room really has one.</li>" +
          "<li><b>Problems</b> (red dot on the port, red text in the list): a planned room PC that isn't seen; an NMS device that isn't on any switch port; a two-cable device " +
          "with only one cable linked; an encoder/decoder running below 10G; a device whose IP doesn't belong to the room of the port it's " +
          "plugged into; something plugged into an unassigned port; and a router uplink or switch backbone port with no link.</li>" +
          "<li><b>Fix router config</b> (folded away): the Cisco <code>ip nat inside source static</code> commands for the forwards you pick. " +
          "Click <b>Copy NAT Commands</b> to paste them yourself, or use <b>Apply to Router</b>.</li>" +
          "</ul>",
      },
      {
        q: "How do I add only some of the router forwards, or change the commands?",
        a: "Open <b>Network &rarr; Fix router config</b>. Under <b>Rules to add</b> there's one square per room and service, like the Router card:" +
          "<ul>" +
          "<li>Click a square to add or leave out that rule, or click a service name (SSH, SWU, API / Cert, NMS) to toggle the whole row. " +
          "<b>Only missing</b> picks just the forwards the live router doesn't have, <b>All planned</b> picks every one, <b>None</b> clears the picks.</li>" +
          "<li>After <b>Check live router</b>, the missing forwards are picked for you, and squares get a red outline (missing) or amber outline " +
          "(the port is used by something else). Amber ones need a look by hand: adding the rule won't replace the other one.</li>" +
          "<li>The text box below always shows the commands for your picks, and you can edit it (e.g. add a one-off rule or a " +
          "<code>no ip nat inside source static &hellip;</code> line to remove one). <b>Undo my edits</b> goes back to the generated commands; " +
          "picking rules again asks before replacing your edits.</li>" +
          "</ul>" +
          "<b>Copy NAT Commands</b> and <b>Apply to Router</b> both use exactly what's in the box. For safety, Apply only accepts " +
          "<code>ip nat inside source static &hellip;</code> and <code>no ip nat inside source static &hellip;</code> lines (plus the " +
          "<code>configure terminal</code> / <code>end</code> / <code>write memory</code> wrappers and <code>!</code> comments); anything else is refused before the router is touched.",
      },
      {
        q: "How does \"Apply to Router\" actually change the router?",
        a: "It opens a real SSH session to the router (needs a privileged admin account, not a read-only one), sends " +
          "<code>enable</code> (with the enable password if the router asks for one), then <code>configure terminal</code>, each " +
          "NAT line in the box, <code>end</code> and <code>write memory</code> - the same steps you'd type by hand. Enter the router " +
          "SSH username/password (and enable password if needed) under <b>Network &rarr; Fix router config</b>, pick the rules (or edit the commands), " +
          "tick the confirm box after reviewing them, then click <b>Apply to Router</b>. The console tab shows the router's live output. Save these once under " +
          "<b>Settings &rarr; Edit saved credentials &rarr; Router, switches &amp; Windows</b> and they'll prefill next time (per-lab, like the " +
          "SSH/sudo passwords). Because this writes live config to lab network infrastructure, double-check the commands first.",
      },
    ],
  },
  {
    title: "Fiber Test",
    items: [
      {
        q: "What does the Fiber Test tab check?",
        a: "<b>Test Fiber</b> checks both ends of every fiber link, like the lab's <code>mg2diagnostics --fiber-test</code>. At the same time it " +
          "logs in to every switch (read-only, the same way as <b>Check switches</b>) for each SFP's light levels and the port error counters, and asks each room's " +
          "Barco NMS for every encoder/decoder port's SFP light and receive errors. Each device port is matched to its switch port by MAC, which gives the " +
          "<b>loss in both directions</b>: device Tx &minus; switch Rx (<i>loss to switch</i>) and switch Tx &minus; device Rx (<i>loss to device</i>). " +
          "The device end needs the SSH and sudo passwords in the sidebar (the NMS login is read from each room, like the Facility check). " +
          "The test runs by itself the first time you open the tab, when a router password is saved for the lab." +
          "<ul>" +
          "<li><b>By switch</b>: each switch's ports coloured by result (green OK, amber warning, red problem, grey-blue copper/DAC, faded unused SFP, " +
          "dashed no module), with the problems and warnings listed under it. <b>Click a port</b> for its module (type, vendor, serial, temperature, laser bias), " +
          "the light at both ends, the loss each way and the errors. <b>Modules</b> (folded) lists every SFP in the switch.</li>" +
          "<li><b>By room</b>: every encoder/decoder fiber port per room, with its switch port, switch Rx, device Rx, the loss each way, errors and status.</li>" +
          "<li><b>Connected to</b>: the switch on the other end (LLDP), or the device the Network tab names on that port. " +
          "Run the <b>Facility</b> check first to get encoder/decoder names.</li>" +
          "<li><b>Errors</b>: the switch's CRC, alignment, fragment, jabber, undersize/oversize, transmit, collision and parity counters (since the switch started " +
          "or the counters were last cleared) and the device's receive errors. Dropped packets aren't counted (that's congestion, not the fiber).</li>" +
          "<li>After a re-test, &#9650;/&#9660; next to a reading shows how much the received light changed (e.g. after cleaning a connector).</li>" +
          "</ul>" +
          "<b>Export Excel</b> downloads the test as an Excel workbook (also listed under <b>Logs &rarr; Downloads</b>):" +
          "<ul>" +
          "<li><b>Summary</b>: when it was tested, the lab limits, one line per switch (fiber ports, linked, problems, warnings, unused SFPs, copper/DAC), " +
          "every problem and warning in one list, and any room whose device end couldn't be read.</li>" +
          "<li><b>One sheet per switch</b>: that switch's ports in port order. Problem rows are light red and warnings amber.</li>" +
          "<li><b>All links</b>: every switch in one table, problems first, for filtering across switches or combining several tests.</li>" +
          "</ul>" +
          "The first columns are what you usually need: <i>Result, Room, Device, Device port, Switch port, Link, Switch Rx, Device Rx, Loss to switch, " +
          "Loss to device</i>, then Tx, the change since the last test, errors and a short description of each issue. Module details (type, vendor, part, " +
          "serial, temperature, laser bias, voltage) are on the right for inventory and RMAs. The header row stays visible while scrolling and has filters, " +
          "so you can show only problems or one room. Nothing on the switches or rooms is changed and the counters aren't cleared.",
      },
      {
        q: "What do the Fiber Test problems and warnings mean?",
        a: "<ul>" +
          "<li><b>Rx outside the lab's range</b> (&minus;5 to &minus;1.5 dBm, at the switch or the device): check the cabling. Usually a dirty or loose connector, " +
          "a tight bend or a damaged patch cable. Clean both ends, reseat, and test again. Below &minus;22 dBm there's no light at all (link down).</li>" +
          "<li><b>Tx outside the lab's range</b> (&minus;4.5 to &minus;1.5 dBm): the SFP sending it is probably bad (in the switch or the device).</li>" +
          "<li><b>High loss</b> (more than 3 dB in one direction): that strand of the fiber pair is dirty, bent or damaged. If only one direction is high, " +
          "clean or replace that strand; the other one is fine.</li>" +
          "<li><b>Weak light received</b> / alarm and warning wording: the SFP's own limits were crossed (shown in the port panel).</li>" +
          "<li><b>Receiving too much light</b>: the receiver is overloaded, typically a long-reach (LR) optic on a short link. Use matching optics or an attenuator.</li>" +
          "<li><b>Sending too little/too much light</b>, <b>laser bias</b>, <b>running hot</b>, <b>supply voltage</b>: the SFP module itself is failing or overheating. Replace it or check the switch's airflow.</li>" +
          "<li><b>Light is arriving but there's no link</b>: the two ends don't match (1G vs 10G, SR vs LR, or a wrong optic on the device).</li>" +
          "<li><b>No light</b> on a port with an SFP and no link is shown as an <i>unused SFP</i> (greyed out). On a router uplink or switch " +
          "backbone port it's a problem: the fiber is unplugged or broken, or the far end is off.</li>" +
          "<li><b>Errors since the counters were last cleared</b> / <b>since it started</b> (warning): they may be old. Test again: <b>new errors since the last test</b> (problem) " +
          "means the link is corrupting traffic right now. The last test is remembered on this computer per lab, so the comparison also works after restarting the app.</li>" +
          "<li><b>The device reports...</b>: an SFP alert from the device's own diagnostics (NMS).</li>" +
          "<li><b>Switch end not found</b>: no switch port has learned that device port's MAC (idle link, or its switch couldn't be read).</li>" +
          "<li><b>SSH host key or NMS certificate changed</b> (red, under the summary): the app refused to connect to those rooms on purpose, so their device end " +
          "wasn't tested. If they were re-imaged or replaced, click <b>Select them in Actions</b> and use <b>Remove SSH Fingerprint</b>, then test again. " +
          "If nobody knows why the key changed, don't remove it; tell the team. A switch whose key changed says which line to delete in the app's known_hosts file " +
          "(Remove Fingerprint only covers rooms).</li>" +
          "</ul>" +
          "The SFP alarm/warning wording uses each module's own limits; the lab ranges are the ones the team's fiber test script uses.",
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
        a: "In <b>Tunnels</b>, pick the room, set <b>App</b> to <b>Matrix App (kiosk)</b>, and click <b>Enable DevTools</b> (restarts the app; needs sudo). " +
          "Then <b>Open DevTools</b> opens the inspector in a new tab (use Chrome or Edge), and <b>Screenshot</b> saves a PNG of the screen. " +
          "If the room's screen has gone to sleep, click <b>Wake Display</b> (needs sudo) first. " +
          "Click <b>Disable DevTools</b> when you're done. Open tunnels are listed under <b>Active Tunnels</b> and close when Matrix Facility Manager stops. " +
          "Each tunnel's link contains a random key: only that link works (other programs on this computer can't use the tunnel), so don't share it.",
      },
      {
        q: "How do I see or debug the Matrix Admin App (the app shown after exiting kiosk mode)?",
        a: "In <b>Tunnels</b>, pick the room, set <b>App</b> to <b>Matrix Admin App</b>, and click <b>Enable DevTools</b> (needs sudo). " +
          "It restarts the admin app in the background. Then click <b>Open DevTools</b>: it wakes the screen, brings the admin login screen up on the room " +
          "(the same thing Exit Kiosk Mode does) and opens DevTools, where you type the admin password in the page preview, or at the room. " +
          "<b>Screenshot</b> also brings the admin app on screen first, since a hidden window can't be captured. " +
          "Click <b>Disable DevTools</b> when you're done, which also returns the room to the kiosk.",
      },
      {
        q: "My DevTools tab says \"WebSocket disconnected\". What happened?",
        a: "Each DevTools tab is tied to one running copy of the app. Enable/Disable DevTools, a reboot, or the app restarting by itself ends that copy, " +
          "so the old tab can't reconnect (<b>Reconnect DevTools</b> won't help). Close it and click <b>Open DevTools</b> again. " +
          "Avoid typing addresses into the small address bar inside DevTools. If the admin app ends up on a broken page, Open DevTools reloads its login page.",
      },
      {
        q: "What do Uptime / Disk Space / Specs show?",
        a: "<b>Uptime</b> is how long the room has been up. <b>Disk Space</b> is free space, including <code>/tmp</code> where SWUs are " +
          "extracted. <b>Specs</b> covers OS/kernel version, CPU, memory and root filesystem usage.",
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
          "<li><b>Add Endpoint</b>: adds one host to <code>apiServer.trustedEndPoints</code>. Entries already present are skipped.</li>" +
          "</ul>",
      },
      {
        q: "What is Custom Command for?",
        a: "Runs one shell command on every selected room and prints the output per room. Tick <b>sudo</b> to run it as root. " +
          "Be careful: it runs exactly what you type, on every ticked room. It's a QA feature: Integration doesn't see it, and with a " +
          "QA password set it needs that password.",
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
    title: "Software Update (SWU)",
    items: [
      {
        q: "What's the normal SWU deployment workflow?",
        a: "<ul>" +
          "<li>Pick your lab in <b>Site</b> and tick the target rooms in the sidebar.</li>" +
          "<li>On <b>Software Update</b>, click <b>Download Latest SWU</b> and choose the build source, or <b>Browse</b> to an SWU file you already have.</li>" +
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
        a: "Use <b>Software Update &rarr; Jenkins Build &rarr; Trigger Build</b>. It starts a new Embedded Builder build (matrix / wrynose) " +
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
    title: "Web App (QA)",
    items: [
      {
        q: "Which Web App mode should I use?",
        a: "<ul>" +
          "<li><b>Deploy existing build</b>: point <b>Backend dist folder</b> at matrix-api's <code>dist</code> (the one with <code>server.js</code>) " +
          "and <b>Web assets folder</b> at the web app's <code>dist\\arthrex-synergy-matrix</code> (the one with <code>index.html</code>), not its parent <code>dist</code>.</li>" +
          "<li><b>Build + deploy</b>: runs <code>git pull</code>, <code>npm install</code> and <code>npm run build</code> in your local " +
          "<code>matrix-api-linux</code> / <code>matrix-app-linux</code> checkouts, then deploys. Needs Git and Node.js on this computer.</li>" +
          "<li><b>Build only</b>: the build step without touching any room.</li>" +
          "</ul>" +
          "A backend/web major-version mismatch is logged as a warning, not an error.",
      },
      {
        q: "What do Configure / Reset / Diagnose Web App do?",
        a: "<b>Configure Web App</b> points <code>apiServer</code> at the installed web app folders, sets the QA pairing key, and trusts every " +
          "room's external API origin, all in one edit plus a <code>matrix-api</code> restart. Because that pairing key is the well-known QA one, " +
          "it only runs on QA labs (labs with the QA port forwards) and is refused on a real facility. <b>Reset Web App</b> undoes a previous deploy " +
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
    title: "Troubleshooting",
    items: [
      {
        q: "Everything fails with \"Failed to fetch\" / a red \"Matrix Facility Manager isn't running\" banner.",
        a: "The page is still open but the app behind it stopped (e.g. it quit after its main window was closed while this one " +
          "was left open in a browser tab). Start <code>MatrixDeploy.exe</code> again. The page reconnects by itself within a few " +
          "seconds, or you can use the new window that opens and close this one.",
      },
      {
        q: "Setup Check says \"Lab network\" can't reach the router.",
        a: "You're not on the lab network. Connect to the lab network or VPN, then click <b>Re-run check</b>. If only one room is unreachable, " +
          "it may just be powered off.",
      },
      {
        q: "\"Its SSH host key changed\" / \"the NMS certificate changed\" / host key errors",
        a: "The app remembers each room's SSH key and NMS certificate the first time it connects, and refuses a room whose key later changes, " +
          "so nothing can pose as a room to collect the passwords. That's expected after a room is reflashed: click " +
          "<b>Actions &rarr; Power &amp; Maintenance &rarr; Remove SSH Fingerprint</b> for those rooms, and the next connection trusts the new key. " +
          "It only edits files on this computer (the app's remembered keys and your <code>known_hosts</code>) and never connects to the room. " +
          "If the room was <b>not</b> reflashed, don't remove it; ask whoever looks after the lab network. " +
          "For the router or a switch, delete its line in <code>%APPDATA%\\MatrixFacilityManager\\known_hosts</code>.",
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
