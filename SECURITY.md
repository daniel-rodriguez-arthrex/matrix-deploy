# Matrix Facility Manager - How We Protect It

How Matrix Facility Manager keeps lab passwords, room secrets and room access
safe, and why we made each choice.

**Last reviewed:** 2026-10-05 (full security review of the codebase).

---

## The short version

- The app only runs on **your own computer** and only answers **its own
  window**. Nobody else on the network, no website you visit, and no other
  program on your PC can use it.
- **Saved passwords are encrypted** for your Windows account. A copied file
  is useless on someone else's computer or account.
- **Room secrets** (API keys, NMS/Extron passwords) never get written to your
  disk, and staged copies on the rooms are deleted as soon as they're applied.
- **QA-only features need a QA passphrase.** Every build we share is locked by
  default.
- **Every room, router and NMS is fingerprinted** on first contact. If one
  later shows a different identity, the app refuses to connect.

---

## 1. Who can use the app

The app is a small web server that runs on your PC and opens in its own
window. Three layers make sure only that window can use it:

| Protection | What it stops |
|---|---|
| Listens on `127.0.0.1` only | Other computers on the network can't reach it at all. |
| Accepts only `localhost`/`127.0.0.1` as the address, and only requests from its own page | Malicious websites in your browser can't send it commands or read your saved passwords (cross-site requests, "DNS rebinding"). |
| A new random sign-in key every time it starts | Other programs, or other Windows users on the same PC, can't use it. Only the window the app opened has the key. |

On top of that, the app tells the browser to block scripts from anywhere but
the app itself, never to show it inside another page, and never to cache
replies that contain passwords.

**Why:** "only on localhost" sounds safe, but any website you visit can try to
talk to programs on your PC. The sign-in key and origin checks close that
gap.

## 2. Saved passwords and tokens

- Passwords you save (lab SSH/sudo, router, switch, Artifactory and Jenkins
  tokens) are stored in `.env` files **encrypted with Windows DPAPI**: only
  your Windows account on your computer can decrypt them.
- Plain-text passwords (for example a value typed into a file by hand) are
  encrypted automatically the next time the app starts.
- Passwords you only type into the app's fields stay in memory and are gone
  when the app closes.
- Saved passwords and tokens are only decrypted inside the app's server and
  never sent to its window. The window only learns *whether* one is saved
  (the box says "Saved - leave blank to use it"), and the server fills it in.
  So even a bug in the page couldn't read them.
- The app never prints passwords to the console. The NMS password is copied to
  your clipboard, never shown. The sign-in key is blanked out of the request
  log.

**Why:** the app folder often lives in OneDrive or gets copied around.
Encryption means a copied or synced file doesn't leak anything.

## 3. Secrets on the rooms

When the app changes a room's configuration (which contains API keys and the
NMS/Extron passwords):

- The new config is sent **straight from memory**. Nothing is written to your
  PC's temp folder.
- On the room it's staged as a file **only the login account can read**, then
  applied and **deleted right away**, even if applying fails.
- Earlier versions left copies in `%TEMP%`. The app now deletes those on
  startup, plus downloaded logs/support bundles older than 7 days.
- The room's `sudo` password is passed over a private channel, never on the
  command line where other accounts on the room could see it.
- Everything else the app stages on a room (web app uploads, journal exports,
  support bundles) goes in the login account's home folder, readable only by
  that account, and is deleted afterwards, even if the step fails. Never a
  fixed name in the shared `/tmp`, where another account on the room could
  read it or plant its own files to be installed as root.
- The NMS login never follows a redirect, so the password only goes to the
  pinned NMS.

## 4. Knowing we're talking to the real room

- **SSH (rooms, router, switches):** the first time the app connects, it
  remembers the device's fingerprint. If it ever changes, the app refuses and
  explains why. A re-imaged room is the usual reason. Use **Actions > Remove
  Fingerprint** for that room, but only once you know why it changed.
- **NMS (HTTPS):** the NMS uses self-signed certificates, so we pin each
  room's certificate the same way.

**Why:** without this, anything on the lab network pretending to be a room
could collect the passwords we're about to send it.

## 5. QA-only features and the QA passphrase

The **Tunnels, Config, Software Update and Web App** tabs (and Actions > NMS
Configuration and Custom Command) are QA-only.

- Choosing **QA** as your team asks for the **QA passphrase**. Without it the
  tabs stay hidden **and** the app itself refuses those features. Calling it
  directly doesn't get around that.
- Wrong guesses are slowed down, and the passphrase is never stored: the app
  only keeps a strongly hashed fingerprint of it.
- **Every shared build is locked by default:** the build refuses to package
  without a QA passphrase, and the lock is built into the app. Deleting or
  replacing files in the app folder doesn't unlock it.
- "Remember this computer" keeps you unlocked on that PC only. The saved key is
  encrypted like other passwords.

**Choosing the passphrase:** use a fresh, random one used for nothing else,
such as four or five random words. Don't use a company-wide or reused
password: the hashed fingerprint ships in every copy of the app, so a weak or
guessable passphrase can be cracked offline.

**Changing it:** set it under Settings > Your team > QA password on the
computer that builds the app (running from source), then rebuild and share
the new zip. Copies already handed out keep the old passphrase.

**What it is and isn't:** it keeps QA-only tools away from people who
shouldn't use them casually. It isn't a substitute for the room passwords:
someone with the room's sudo password can still do the same things over SSH.

## 6. DevTools tunnels (Tunnels tab)

Opening DevTools gives full control of a room's Matrix App, so:

- Each tunnel gets its own **random key** in its link. Only that link works.
  Other programs or users on your PC can't use the tunnel.
- The tunnel also checks which page is talking to it, so websites can't use it
  either.
- Tunnels close when you click **Close** or when the app stops.

Don't share a tunnel link. It is access to that room's app while it's open.

## 7. Router changes

**Network > Fix router config > Apply** only ever sends
`ip nat inside source static ...` lines (or `no ...` to remove one). Any other
line is refused, so a typo or a pasted snippet can't change anything else on
the router. Reading the router and switches is read-only.

## 8. Facility planner import

- Planner zips are checked and size-limited before reading (protects against
  "zip bombs").
- Only the layout goes into the lab profile; passwords go to the lab's
  encrypted `.env`. **The zip itself, including its NMS CA private key, is
  never stored.**

## 9. Web App Configuration and the pairing key

**Configure Web App** sets the well-known QA pairing key. It's fine on QA labs
but must never land on a real facility, so the app **refuses it on any lab
without the QA test port forwards**.

## 10. Sharing the app (the zip)

- The app zip contains **no labs and no passwords**: no lab profiles (router
  IPs, room layout), no lab `.env` files, no personal Artifactory/Jenkins
  tokens and no `.env`. The build checks this and stops if one slips in. A
  leaked app zip gives nobody access to anything.
- **Access to a lab comes from its facility planner zip.** Each person
  imports the zip for the site they work on (Settings > Facility config). It
  brings the lab layout and the site passwords, including the room SSH/sudo
  login, which are encrypted for that person's Windows account on import. The
  router and switch passwords aren't in it; the app asks for them.
- **Treat the facility zip like a password**: it holds the room login, site
  API keys and the NMS CA private key in plain text. Only give it to people
  who should have access to that site, don't forward it, and delete it once
  imported. The app never keeps a copy.
- The exe is a plain, visible console app. We deliberately don't hide the
  console or use tricks that security software flags. If SentinelOne
  quarantines it, the fix is IT allowlisting or signing, not code changes.

## 11. Dependencies

All third-party libraries are pinned to exact versions, and known security
advisories were fixed at the last review. We accept one known issue on
purpose: the SSH library still allows the older `ssh-rsa` signature, because
the lab's Extreme switches only support that.

### Accepted: paramiko 4.0.0 and SHA-1 `ssh-rsa`

Scanners (pip-audit, Snyk, Dependabot) report this one advisory under three
IDs. They are the same issue:

- **CVE-2026-44405**
- **GHSA-r374-rxx8-8654**
- **PYSEC-2026-2858**

**What it is:** paramiko (the SSH library) up to 4.0.0 still allows the
`ssh-rsa` signature, which uses SHA-1. SHA-1 is weak enough that, in
theory, an attacker could fake a device's SSH identity.

**Why we can't upgrade:** paramiko 5.0 removed `ssh-rsa` host keys, and the
lab's Extreme (EXOS) switches only offer `ssh-rsa`. Upgrading would mean the
app can no longer read the switches (Network and Fiber Test tabs).

**How we limit it:** SHA-1 is refused unless a connection opts in
(`ssh_client.connect(..., sha1="refuse")` is the default):

| Device | SHA-1 | Why |
|---|---|---|
| Rooms | Refused, always | They run modern OpenSSH and hold the secrets (sudo, NMS passwords, room configs). A room that only offers SHA-1 gets a "don't connect" error on purpose. |
| Router | Only if it offers nothing better | Modern signatures are tried first (QA1's router uses `rsa-sha2-512`). |
| Switches | Allowed | EXOS only supports `ssh-rsa`. The app only runs read-only `show` commands on switches and never changes their config. |

On top of that, every device's SSH fingerprint is saved on first contact and
the app refuses to connect if it changes (section 4).

**Real-world risk: low.** Exploiting it means pretending to be a lab switch
on the lab network, which needs a costly SHA-1 forgery. Even then the
attacker only gets the switch password, not room or NMS secrets.

**When to revisit:** when the switches get EXOS firmware that supports
`rsa-sha2-256/512`, upgrade paramiko to 5.x and remove this exception.
Running `pip-audit -r requirements.txt --ignore-vuln GHSA-r374-rxx8-8654`
keeps this accepted issue out of the results without hiding anything new.

## 12. Source code

- The GitHub repository is **private** (it was public until 2026-10-02). Its
  older history mentions internal lab addresses and build-server names, so it
  must stay private.
- Lab passwords, `.env` files, lab profiles, the QA lock and any keys or
  certificates are excluded from git.

---

## Known limits (accepted)

| Limit | Why we accept it |
|---|---|
| The facility planner zip holds site passwords in plain text. | It's the planner's export format. Mitigation: hand it only to people who should have that site, delete it once imported; the app encrypts what it imports and never keeps the zip. |
| The QA lock is a deterrent, not full access control. | A desktop app can't fully protect itself from someone who controls the PC, and the room passwords already allow the same actions. |
| Anyone already using your Windows account can use your saved passwords. | That's how Windows account encryption works; lock your PC when away. |
| `ssh-rsa` is still allowed (paramiko 4.0.0: CVE-2026-44405 / GHSA-r374-rxx8-8654 / PYSEC-2026-2858). | Required by the lab switches; refused for rooms. See section 11. |

## If something looks wrong

- **"Host key changed" / "NMS certificate changed":** don't click through.
  Confirm the room was re-imaged or replaced, then use Remove Fingerprint. If
  nobody knows why, tell the team.
- **The exe disappears or gets blocked:** don't restore it yourself; contact
  IT.
- **You think a password leaked** (a zip shared too widely, a laptop lost):
  tell the team so the lab passwords and the QA passphrase can be changed.
