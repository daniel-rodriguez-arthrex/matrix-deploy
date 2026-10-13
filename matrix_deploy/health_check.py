"""Standalone hardware health check for a single device (room PC or any box
reachable over SSH), read-only.

Runs one bounded collector script on the device - as root via ``sudo -S`` when
a sudo password is available (password on stdin, never in the command line -
same pattern as ``Deployer._run_sudo_script``), else unprivileged with reduced
coverage. The script prints ``@@@SECTION`` blocks of ``key=value`` /
``tag|field|...`` lines, parsed here into a report of checked items, each
ok/warn/bad. Built for the field reports of SWU update failures, flaky
network and corrupt downloads, so storage health and link quality get the
most attention.
"""

from __future__ import annotations

import re
import shlex
import time
from typing import Any, Dict, List, Optional

import paramiko

from .ssh_client import SSHError, SSHTarget, connect, redact, strip_ansi

# Worst a check can make a section, worst section makes the report state.
_RANK = {"ok": 0, "info": 0, "warn": 1, "bad": 2}
_STATE = {0: "healthy", 1: "warning", 2: "error"}

# Kernel-log signatures of storage trouble (30d window): bad blocks, media
# errors, filesystem errors, link/controller resets. Anything matching these
# explains corrupt downloads or failed SWU installs better than any counter.
_DISK_ERR_RE = (
    r"I/O error|medium error|critical medium|blk_update_request|Buffer I/O|"
    r"EXT4-fs error|XFS.*(error|corrupt)|nvme.*(I/O|error|fail|reset)|"
    # Not "SATA link down": that's an empty SATA port (e.g. a device with an NVMe disk).
    r"ata[0-9.]+.*(error|failed|exception)|hard resetting link|"
    r"SError|frozen|"
    r"mmcblk.*(error|timeout)|blk_update_request"
)
_OOM_RE = r"oom-kill|Out of memory|invoked oom-killer|Killed process"
_MCE_RE = r"machine check|mce:|EDAC|Hardware Error"
_PCIE_RE = r"PCIe Bus Error|AER:"
_FLAP_RE = r"link is up|link is down|NIC Link is|Link is (Up|Down)"

# One read-only pass over SSH, bounded output (everything counted or tail'd).
# $1/$2/$3 = matrix-api unit, nms unit, swupdate listen port.
COLLECTOR = r"""
sec() { printf '\n@@@%s\n' "$1"; }
SVC_API="${1:-matrix-api}"
SVC_NMS="${2:-barco-nms}"
SWU_PORT="${3:-8080}"

sec IDENT
echo "hostname=$(hostname 2>/dev/null)"
echo "kernel=$(uname -r 2>/dev/null) $(uname -m 2>/dev/null)"
echo "os=$(. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME")"
echo "uptime=$(uptime -p 2>/dev/null | sed 's/^up //')"
echo "uptime_s=$(cut -d' ' -f1 /proc/uptime 2>/dev/null)"
echo "load=$(cat /proc/loadavg 2>/dev/null)"
echo "vendor=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null | tr -s ' ')"
echo "product=$(cat /sys/class/dmi/id/product_name 2>/dev/null | tr -s ' ')"
echo "bios=$(cat /sys/class/dmi/id/bios_version 2>/dev/null)"
echo "serial=$(cat /sys/class/dmi/id/product_serial 2>/dev/null | tr -s ' ')"
ip -4 -o addr show scope global 2>/dev/null | awk '{print "ipaddr|" $2 "|" $4}'
echo "sw_version=$(cat /etc/software-version 2>/dev/null)"
echo "sw_build=$(sed -n 's/^Local Revision: *//p' /etc/arthrex-version 2>/dev/null)"
echo "now=$(date '+%Y-%m-%d %H:%M:%S %z' 2>/dev/null)"
if [ -f /etc/arthrex-version ] || [ -f /etc/software-version ]; then echo "is_matrix=1"; else echo "is_matrix=0"; fi
if journalctl -k -n 1 --no-pager >/dev/null 2>&1; then echo "journal_ok=1"; else echo "journal_ok=0"; fi
echo "reboots_30d=$(journalctl -k --since '-30 days' --no-pager 2>/dev/null | grep -c 'Linux version')"

sec CPU
cores=$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null)
echo "cores=$cores"
echo "model=$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ *//')"
for z in /sys/class/thermal/thermal_zone*; do
  [ -r "$z/temp" ] && echo "thermal|$(cat "$z/type" 2>/dev/null)|$(cat "$z/temp" 2>/dev/null)"
done
if command -v sensors >/dev/null 2>&1; then
  sensors -A 2>/dev/null | head -n 40 | sed 's/^/sensors|/'
fi
hw_hard=$(journalctl -k --since '-30 days' --no-pager 2>/dev/null | grep -icE '__MCE_RE__')
hw_pcie=$(journalctl -k --since '-30 days' --no-pager 2>/dev/null | grep -icE '__PCIE_RE__')
echo "hw_err_30d=$hw_hard"
echo "pcie_err_30d=$hw_pcie"
journalctl -k --since '-30 days' --no-pager 2>/dev/null | grep -iaE '__MCE_RE__|__PCIE_RE__' | tail -n 8 | sed 's/^/hwerr|/'

sec MEM
free -m 2>/dev/null | sed 's/^/free|/'
oom=$(journalctl -k --since '-30 days' --no-pager 2>/dev/null | grep -icE '__OOM_RE__')
echo "oom_30d=$oom"
journalctl -k --since '-30 days' --no-pager 2>/dev/null | grep -iaE '__OOM_RE__' | tail -n 6 | sed 's/^/oom|/'
# Memory controller error counters (EDAC): ce = corrected, ue = uncorrected.
edac=0
for m in /sys/devices/system/edac/mc/mc[0-9]*; do
  [ -r "$m/ce_count" ] || continue
  edac=1
  echo "edac|${m##*/}|$(cat "$m/ce_count" 2>/dev/null)|$(cat "$m/ue_count" 2>/dev/null)"
done
echo "edac_present=$edac"
echo "mem_err_30d=$(journalctl -k --since '-30 days' --no-pager 2>/dev/null | grep -icE 'EDAC|memory (read|scrub)? ?error|Corrected error.*memory|DIMM')"

sec DISK
if command -v lsblk >/dev/null 2>&1; then
  lsblk -d -P -o NAME,MODEL,SIZE,TYPE,TRAN,ROTA 2>/dev/null | sed 's/^/lsblk|/'
else
  for b in /sys/block/*; do
    [ -e "$b" ] || continue
    n="${b##*/}"
    case "$n" in loop*|ram*|dm-*) continue;; esac
    echo "lsblk|NAME=\"$n\" MODEL=\"$(cat "$b/device/model" 2>/dev/null | tr -s ' ')\" SIZE=\"$(( $(cat "$b/size" 2>/dev/null || echo 0) / 2048 ))MB\" TYPE=\"disk\" TRAN=\"\" ROTA=\"$(cat "$b/queue/rotational" 2>/dev/null)\""
  done
fi
df -hT -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs -x ramfs -x cgroup -x cgroup2 -x devpts -x mqueue -x shm -x hugetlbfs -x debugfs -x tracefs -x configfs -x fusectl -x pstore -x securityfs -x bpf -x autofs -x binfmt_misc 2>/dev/null | sed 's/^/df|/'
df -hT /tmp 2>/dev/null | tail -n 1 | sed 's/^/dftmp|/'
# Read-only disk mounts (some are read-only by design, e.g. the Matrix image's
# /media/rfs/ro); only the kernel's "remounting read-only" after errors is a fault.
if command -v findmnt >/dev/null 2>&1; then
  findmnt -rn -o TARGET,SOURCE,FSTYPE,OPTIONS 2>/dev/null | awk '$2 ~ /^\/dev\// && $4 ~ /(^|,)ro(,|$)/ {print "ro|" $0}'
else
  awk '$1 ~ /^\/dev\// && $4 ~ /(^|,)ro(,|$)/ {print "ro|" $2 " " $1 " " $3 " " $4}' /proc/mounts 2>/dev/null
fi
echo "ro_remount_30d=$(journalctl -k --since '-30 days' --no-pager 2>/dev/null | grep -icE 'remounting filesystem read-only|remount(ed|ing)? .*read-only')"
journalctl -k --since '-30 days' --no-pager 2>/dev/null | grep -iaE 'remounting filesystem read-only|remount(ed|ing)? .*read-only' | tail -n 5 | sed 's/^/rolog|/'
for d in /lost+found /home/lost+found /data/lost+found /var/lost+found; do
  [ -d "$d" ] && echo "lostfound|$d|$(ls -A "$d" 2>/dev/null | wc -l)"
done
root_src=$(findmnt -n -o SOURCE / 2>/dev/null || awk '$2=="/" {print $1}' /proc/mounts 2>/dev/null)
case "$root_src" in
  /dev/*)
    if command -v tune2fs >/dev/null 2>&1; then
      tune2fs -l "$root_src" 2>/dev/null | grep -E 'Filesystem state|Errors behavior|Mount count' | sed 's/^/fsstate|/'
    fi ;;
esac
for f in /sys/class/mmc_host/*/mmc*/life_time /sys/block/mmcblk*/device/life_time; do
  [ -r "$f" ] && echo "mmc|$f|$(tr -s ' \n' '  ' < "$f" 2>/dev/null)"
done
disks=""
if command -v lsblk >/dev/null 2>&1; then
  disks=$(lsblk -dn -o NAME,TYPE 2>/dev/null | awk '$2=="disk"{print "/dev/"$1}')
else
  for b in /sys/block/*; do case "${b##*/}" in loop*|ram*|dm-*) ;; *) disks="$disks /dev/${b##*/}";; esac; done
fi
if command -v smartctl >/dev/null 2>&1; then
  for dev in $disks; do
    echo "disk_begin|$dev"
    smartctl -i "$dev" 2>/dev/null | grep -iE 'Model Number|Device Model|Model Family|Serial Number|Rotation Rate|SMART support is' | sed 's/^/info|/'
    smartctl -H "$dev" 2>/dev/null | grep -iE 'overall-health|test result|SMART support is|SMART.*(unable|Unavailable|Disabled)' | sed 's/^/health|/'
    smartctl -A "$dev" 2>/dev/null | sed 's/^/attr|/'
    echo "disk_end|$dev"
  done
else
  echo "smart_missing=1"
fi
if command -v nvme >/dev/null 2>&1; then
  for dev in /dev/nvme[0-9]; do
    [ -b "$dev" ] || continue
    echo "nvme_begin|$dev"
    nvme smart-log "$dev" 2>/dev/null | sed 's/^/nvme|/'
    echo "nvme_end|$dev"
  done
fi
errs=$(journalctl -k --since '-30 days' --no-pager 2>/dev/null | grep -iaE '__DISK_ERR_RE__')
echo "disk_err_30d=$(printf '%s' "$errs" | grep -c . 2>/dev/null)"
printf '%s\n' "$errs" | tail -n 15 | sed 's/^/err|/'

sec NET
ip -s link show 2>/dev/null | sed 's/^/ip|/'
for n in /sys/class/net/*; do
  i="${n##*/}"
  echo "iface|$i|$(cat "$n/operstate" 2>/dev/null)|$(cat "$n/speed" 2>/dev/null)|$(cat "$n/carrier" 2>/dev/null)|$(cat "$n/mtu" 2>/dev/null)|$(cat "$n/address" 2>/dev/null)"
done
ip route show default 2>/dev/null | sed 's/^/route|/'
gw=$(ip route show default 2>/dev/null | awk 'NR==1{print $3}')
echo "gw=$gw"
if [ -n "$gw" ]; then
  ping -c 2 -W 2 "$gw" 2>/dev/null | grep -E 'packet loss|min/avg' | sed 's/^/ping|/'
fi
echo "link_flaps_30d=$(journalctl -k --since '-30 days' --no-pager 2>/dev/null | grep -icE '__FLAP_RE__')"

sec SVC
systemctl --failed --no-legend --plain 2>/dev/null | sed 's/^/failed|/'
systemctl is-active "$SVC_API" 2>/dev/null | sed "s/^/svc|$SVC_API|/"
systemctl is-active "$SVC_NMS" 2>/dev/null | sed "s/^/svc|$SVC_NMS|/"
if command -v swupdate-client >/dev/null 2>&1; then echo "swupdate_client=1"; else echo "swupdate_client=0"; fi
if pidof swupdate >/dev/null 2>&1 || pgrep -x swupdate >/dev/null 2>&1; then echo "swupdate_running=1"; else echo "swupdate_running=0"; fi
ss -tln 2>/dev/null | awk 'NR>1{n=split($4,a,":"); print a[n]}' | sort -un | sed 's/^/listen|/'

sec KERR
# Without known-harmless firmware/driver noise (ACPI BIOS table bugs, absent audio codecs).
KNOISE='ACPI BIOS Error|ACPI Error|Cannot probe codecs|firmware bug|-- No entries --|^-- '
echo "kern_err_7d=$(journalctl -k -p err --since '-7 days' --no-pager 2>/dev/null | grep -vE "$KNOISE" | grep -c .)"
journalctl -k -p err --since '-7 days' --no-pager 2>/dev/null | grep -vE "$KNOISE" | tail -n 25 | sed 's/^/kerr|/'
"""

COLLECTOR = (
    COLLECTOR
    .replace("__DISK_ERR_RE__", _DISK_ERR_RE)
    .replace("__OOM_RE__", _OOM_RE)
    .replace("__MCE_RE__", _MCE_RE)
    .replace("__PCIE_RE__", _PCIE_RE)
    .replace("__FLAP_RE__", _FLAP_RE)
)

READ_TIMEOUT_S = 150


def _exec_collector(
    client: paramiko.SSHClient,
    sudo_password: Optional[str],
    args: tuple = (),
    timeout: int = READ_TIMEOUT_S,
) -> str:
    """Run the collector script through ``bash -s`` (elevated with
    ``sudo -S`` when a password is available). stderr is merged into stdout
    so there is no second pipe to drain; the sudo password goes on the first
    stdin line, the script after it."""
    argv = " ".join(shlex.quote(str(a)) for a in args)
    if sudo_password:
        # -k: always read the password line, even if sudo has a cached
        # timestamp - otherwise bash would get it as its first script line.
        cmd = f"sudo -k -S -p '' bash -s -- {argv} 2>&1"
    else:
        cmd = f"bash -s -- {argv} 2>&1"
    stdin, stdout, _stderr = client.exec_command(cmd)
    if sudo_password:
        stdin.write(sudo_password + "\n")
    stdin.write(COLLECTOR + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()

    chan = stdout.channel
    chan.settimeout(5)
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while True:
        if chan.recv_ready():
            buf += chan.recv(65536)
            continue
        if chan.exit_status_ready() and not chan.recv_ready():
            break
        if time.monotonic() > deadline:
            try:
                chan.close()
            except Exception:  # noqa: BLE001
                pass
            raise SSHError("The health check took too long on the device and was stopped.")
        time.sleep(0.05)
    text = bytes(buf).decode(errors="replace")
    return redact(client, strip_ansi(text))


def _sudo_works(client: paramiko.SSHClient, sudo_password: str) -> bool:
    """One ``sudo -S true`` with only the password on stdin, so a wrong
    password fails once instead of sudo reading script lines as retries."""
    stdin, stdout, _stderr = client.exec_command("sudo -S -p '' true 2>/dev/null")
    stdin.write(sudo_password + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()
    stdout.channel.settimeout(30)
    try:
        return stdout.channel.recv_exit_status() == 0
    except Exception:  # noqa: BLE001
        return False


def _split_sections(text: str) -> Dict[str, List[str]]:
    """``@@@NAME`` marker blocks -> {NAME: [lines]}."""
    out: Dict[str, List[str]] = {}
    cur: Optional[str] = None
    for line in text.splitlines():
        m = re.match(r"^@@@([A-Z_]+)\s*$", line.strip())
        if m:
            cur = m.group(1)
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return out


def _kv(lines: List[str]) -> Dict[str, str]:
    out = {}
    for line in lines:
        if "=" in line and "|" not in line.split("=", 1)[0]:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _tagged(lines: List[str], tag: str) -> List[List[str]]:
    return [line[len(tag) + 1:].split("|") for line in lines if line.startswith(tag + "|")]


def _int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _item(label: str, value: str, state: str = "ok", detail: str = "", action: str = "") -> Dict[str, Any]:
    """``action``: the plain next step for a field tech, shown only when the
    item is a warning or problem."""
    return {"label": label, "value": value, "state": state, "detail": detail,
            "action": action if state in ("warn", "bad") else ""}


# Next steps shared by several checks (same wording on Windows, see the .ps1).
DO_REPLACE_DISK = "Replace the disk (or the device). It is failing and will corrupt updates and downloads."
DO_FREE_SPACE = "Free up disk space (old logs, old update files), then retry the update."
DO_CHECK_CABLE = "Replace the network cable or move it to another switch port, then check again."
DO_COOLING = "Check the fans and airflow: clean dust and make sure the vents aren't blocked."
DO_REPLACE_HW = "Replace the device: the hardware (CPU, memory or board) reports errors."
DO_RESTART = "Restart the device and check again. If it comes back, export this report and escalate."
DO_POWER = "Check the power supply, power cable and UPS: the device is losing power or resetting."


class _Score:
    """A 0-100 health score built from penalties (``hit``) and ceilings
    (``cap``, for "this alone means failing"). Free space is NOT part of it:
    the scores say how healthy the hardware is, not how full it is. Same
    rules as the Windows script (MatrixHealthCheck.ps1, New-Score)."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.score = 100.0
        self.ceiling = 100.0
        self.reasons: List[str] = []
        self.missing: List[str] = []
        self.facts: List[str] = []
        # Key numbers shown prominently (life left, power-on time, cycles...).
        self.stats: List[Dict[str, str]] = []

    def stat(self, label: str, value: str, state: str = "info", hint: str = "") -> None:
        self.stats.append({"label": label, "value": value, "state": state, "hint": hint})

    def hit(self, points: float, reason: str) -> None:
        self.score -= points
        self.reasons.append(reason)

    def cap(self, ceiling: float, reason: str = "") -> None:
        self.ceiling = min(self.ceiling, ceiling)
        if reason:
            self.reasons.append(reason)

    def result(self) -> Dict[str, Any]:
        s = int(round(max(0.0, min(self.score, self.ceiling))))
        grade = "Good" if s >= 90 else "Fair" if s >= 70 else "Poor" if s >= 40 else "Failing"
        return {"title": self.title, "score": s, "grade": grade,
                "state": {"Good": "ok", "Fair": "warn"}.get(grade, "bad"),
                "reasons": self.reasons, "facts": self.facts, "stats": self.stats,
                # What couldn't be read (the score may be too optimistic).
                "missing": self.missing}


def _eval_ident(lines: List[str]) -> Dict[str, Any]:
    d = _kv(lines)
    items = []
    host = d.get("hostname", "")
    device = " ".join(p for p in (d.get("vendor"), d.get("product")) if p)
    items.append(_item("Hostname", host or "?", "info"))
    if device:
        items.append(_item("Device", device, "info", f"BIOS {d.get('bios', '')}".strip()))
    items.append(_item("OS", d.get("os") or "?", "info", d.get("kernel", "")))
    if d.get("uptime"):
        items.append(_item("Uptime", d["uptime"], "info"))
    reboots = _int(d.get("reboots_30d"))
    if reboots is not None and journal_ok(d):
        items.append(_item("Restarts (last 30 days)", str(reboots),
                           "warn" if reboots > 20 else "ok" if reboots > 5 else "info",
                           "Unexpected reboots are a common field-failure cause." if reboots > 5 else "", DO_POWER))
    sw = d.get("sw_version")
    if sw or d.get("sw_build"):
        items.append(_item("Software", f"{sw or '?'} ({d.get('sw_build') or 'unknown build'})", "info"))
    if d.get("is_matrix") != "1":
        items.append(_item("Matrix image", "not detected", "info",
                           "No /etc/arthrex-version or /etc/software-version - not a Matrix room image."))
    return {"id": "ident", "title": "System", "items": items}


def journal_ok(d: Dict[str, str]) -> bool:
    return d.get("journal_ok") == "1"


def _eval_cpu(lines: List[str], ident: Dict[str, str]) -> Dict[str, Any]:
    d = _kv(lines)
    items = []
    cores = _int(d.get("cores"))
    if d.get("model") or cores:
        items.append(_item("CPU", " \u00b7 ".join(p for p in (d.get("model"), f"{cores} cores" if cores else "") if p), "info"))
    load = (ident.get("load") or "").split()
    if len(load) >= 3 and cores:
        try:
            l15 = float(load[2])
            state = "bad" if l15 > cores * 4 else "warn" if l15 > cores * 2 else "ok"
            items.append(_item("CPU busy (load 1/5/15 min)", " / ".join(load[:3]), state,
                               "Sustained load far above the core count points at a stuck process or hardware stall." if state != "ok" else "",
                               DO_RESTART))
        except ValueError:
            pass
    worst_state, temps = "ok", []
    for name, milli in (t[:2] for t in _tagged(lines, "thermal") if len(t) >= 2):
        t = _int(milli)
        if t is None:
            continue
        c = t / 1000.0
        temps.append(f"{name} {c:.0f}\u00b0C")
        if c >= 90:
            worst_state = "bad"
        elif c >= 75 and worst_state != "bad":
            worst_state = "warn"
    if temps:
        items.append(_item("Temperatures", ", ".join(temps), worst_state,
                           "Overheating causes throttling, freezes and silent reboots." if worst_state != "ok" else "", DO_COOLING))
    sensors = [l.split("|", 1)[1] for l in lines if l.startswith("sensors|")]
    hw = _int(d.get("hw_err_30d"), 0) or 0
    pcie = _int(d.get("pcie_err_30d"), 0) or 0
    hw_lines = [l.split("|", 1)[1] for l in lines if l.startswith("hwerr|")]
    if hw:
        items.append(_item("Hardware error reports (30 days)", str(hw), "bad",
                           "Machine-check / EDAC errors mean bad CPU, RAM or board.\n" + "\n".join(hw_lines), DO_REPLACE_HW))
    if pcie:
        items.append(_item("Internal bus (PCIe) errors (30 days)", str(pcie), "warn" if not hw else "info",
                           "PCIe/AER errors - corrected ones are often benign, uncorrected ones break devices.\n" + "\n".join(hw_lines),
                           "Usually harmless on its own. If the device also has other problems, replace it."))
    sec = {"id": "cpu", "title": "CPU, temperature & power", "items": items}
    if sensors:
        sec["raw"] = "\n".join(sensors)
    return sec


DO_REPLACE_RAM = "Replace the memory (or the device): a memory module is going bad. This causes crashes and corrupt files."


def _eval_mem(lines: List[str], cpu_lines: List[str], journal_readable: bool = True) -> Dict[str, Any]:
    items = []
    d = _kv(lines)
    score = _Score("Memory health")
    # Error counters first: they say whether the RAM itself is failing.
    edac = [f for f in _tagged(lines, "edac") if len(f) >= 3]
    ce = sum(_int(f[1], 0) or 0 for f in edac)
    ue = sum(_int(f[2], 0) or 0 for f in edac)
    if edac:
        state = "bad" if ue or ce >= 10 else "warn" if ce else "ok"
        items.append(_item("Memory errors (ECC counters)", "none" if not (ce or ue) else f"{ce} corrected, {ue} uncorrectable", state,
                           "Counted by the memory controller since boot (EDAC). Corrected errors mean a module is starting to fail; "
                           "uncorrectable ones corrupt data.\n" + "\n".join(f"{f[0]}: ce={f[1]} ue={f[2]}" for f in edac), DO_REPLACE_RAM))
        if ue:
            score.cap(20, f"{ue} uncorrectable memory errors")
        if ce:
            score.hit(30 if ce < 10 else 50, f"{ce} corrected memory errors (a module is going bad)")
    else:
        score.missing.append("memory error counters (this hardware doesn't report them)")
    mem_log = _int(d.get("mem_err_30d"), 0) or 0
    if mem_log and not (ce or ue):
        score.hit(20, f"{mem_log} memory error messages in the last 30 days")
        items.append(_item("Memory error messages (30 days)", str(mem_log), "warn",
                           "The kernel logged memory (EDAC/DIMM) errors.", DO_REPLACE_RAM))
    hw = _int(_kv(cpu_lines).get("hw_err_30d"), 0) or 0
    if hw:
        score.hit(25, f"{hw} hardware error reports (CPU, memory or board)")
    for f in _tagged(lines, "free"):
        row = " ".join(f).split()
        if row and row[0].rstrip(":") == "Mem" and len(row) >= 7:
            total, avail = _int(row[1]), _int(row[6])
            if total and avail is not None:
                state = "bad" if avail < 96 else "warn" if avail < 256 or avail * 100 // max(total, 1) < 5 else "ok"
                score.stat("Installed", f"{total / 1024:.1f} GB")
                score.stat("Free right now", f"{avail / 1024:.1f} GB", state)
                if state == "bad":
                    score.hit(20, "almost no free memory right now")
                elif state == "warn":
                    score.hit(10, "low on free memory right now")
                items.append(_item("Free memory", f"{avail} MB free of {total} MB", state,
                                   "Almost no usable RAM - services get killed or stall." if state != "ok" else "", DO_RESTART))
        if row and row[0].rstrip(":") == "Swap" and len(row) >= 3:
            stot, sfree = _int(row[1]), _int(row[3])
            if stot and sfree is not None and sfree * 5 < stot:
                items.append(_item("Swap space", f"{sfree} MB free of {stot} MB", "warn", "Swap is nearly exhausted.", DO_RESTART))
    oom = _int(d.get("oom_30d"), 0) or 0
    oom_lines = [l.split("|", 1)[1] for l in lines if l.startswith("oom|")]
    if oom:
        score.hit(25 if oom > 5 else 15, f"programs were killed for lack of memory {oom} time(s)")
        items.append(_item("Programs killed for lack of memory (30 days)", str(oom), "bad",
                           "The kernel killed processes to free memory (OOM).\n" + "\n".join(oom_lines), DO_RESTART))
    if edac:
        score.stat("Memory errors (ECC)", f"{ce + ue:,}", "bad" if ue or ce >= 10 else "warn" if ce else "ok")
    score.stat("Killed for lack of memory (30 days)", f"{oom:,}", "bad" if oom else "ok")
    if not journal_readable:
        score.missing.append("the memory error history (needs the sudo password)")
    return {"id": "mem", "title": "Memory", "items": items, "score": score.result()}


def _smart_attrs(attr_lines: List[str]) -> Dict[str, Any]:
    """Interesting ATA SMART attributes by name -> raw value."""
    out: Dict[str, Any] = {}
    for line in attr_lines:
        cols = line.split()
        # ATA table row: ID# NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN RAW
        if len(cols) >= 10 and cols[0].isdigit():
            # The whole RAW_VALUE column: "32 (Min/Max 23/67)", "2338 (Average 2307)".
            out[cols[1]] = " ".join(cols[9:])
            out[cols[1] + "@v"] = cols[3]  # normalized value (wear attrs)
        elif ":" in line:
            # NVMe-style "Percentage Used: 5%" output from smartctl -A.
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


# Erase cycles a typical (TLC) SSD's flash is rated for; used to estimate life
# left from the erase count when the drive doesn't report it honestly.
RATED_ERASE_CYCLES = 3000


def _life_left(attrs: Dict[str, Any], raw: Any, nvme_used: Optional[int]) -> tuple:
    """``(percent left or None, text)``. NVMe "Percentage Used" is reliable.
    ATA drives report life as a normalized value counting down from 100, but
    some (e.g. Phison-based Kingston SSDs, QA1 OR 6) leave every normalized
    value stuck at 100 for life: then it's estimated from the average erase
    count instead, and never shown as 100% just because the drive says so."""
    if nvme_used is not None:
        left = max(0, 100 - nvme_used)
        return left, f"{left}% life left"
    norms = [int(v) for k, v in attrs.items() if k.endswith("@v") and "Temperature" not in k and str(v).isdigit()]
    frozen = len(norms) >= 5 and all(n == 100 for n in norms)
    if not frozen:
        for a in ("Media_Wearout_Indicator", "SSD_Life_Left", "Percent_Lifetime_Remain", "Wear_Leveling_Count"):
            v = raw(a + "@v")
            if v is not None and 0 <= v <= 100:
                return v, f"{v}% life left"
    avg = raw("Average_Erase_Count") or raw("Avg_Erase_Count") or raw("Average_Block-Erase_Count")
    m = re.search(r"Average\s+(\d+)", str(attrs.get("MaxAvgErase_Ct", "")))
    if not avg and m:
        avg = int(m.group(1))
    if avg:
        left = max(0, round(100 - 100 * avg / RATED_ERASE_CYCLES))
        return left, (f"about {left}% life left (estimated: each block erased {avg:,} times on average, "
                      f"typical rating ~{RATED_ERASE_CYCLES:,})")
    if frozen:
        return None, "life left (this disk doesn't report it)"
    return None, ""


def _tb_written(attrs: Dict[str, Any], raw: Any, poh: Optional[int] = None) -> str:
    """Total data written to the disk, when it reports it."""
    gib = raw("Lifetime_Writes_GiB") or raw("Host_Writes_GiB")
    if not gib and raw("Host_Writes_32MiB"):
        gib = raw("Host_Writes_32MiB") / 32
    if not gib and raw("Total_LBAs_Written"):
        lbas = raw("Total_LBAs_Written")
        gib = lbas * 512 / 2 ** 30
        # Some vendors (Kingston/Phison, QA1 OR 7) count this in GiB, not
        # 512-byte sectors: under 10 GB after weeks of running isn't real.
        if gib < 10 and (poh or 0) > 200:
            gib = float(lbas)
    if not gib and raw("Data Units Written"):
        gib = raw("Data Units Written") * 512000 / 2 ** 30
    return f"~{gib / 1024:,.0f} TB written" if gib and gib >= 1024 else (f"~{gib:,.0f} GB written" if gib else "")


def _eval_disk(lines: List[str], journal_readable: bool) -> Dict[str, Any]:
    items = []

    disks = []
    for raw in _tagged(lines, "lsblk"):
        kv = dict(re.findall(r'(\w+)="([^"]*)"', raw[0]))
        if kv.get("TYPE") == "disk":
            disks.append(kv)

    smart_missing = any(l.startswith("smart_missing=") for l in lines)
    per_disk: List[Any] = []  # (dev, model, transport, _Score)

    # Per-disk SMART blocks.
    blocks: Dict[str, Dict[str, List[str]]] = {}
    cur: Optional[str] = None
    for line in lines:
        if line.startswith("disk_begin|") or line.startswith("nvme_begin|"):
            cur = line.split("|", 1)[1]
            blocks[cur] = {"info": [], "health": [], "attr": [], "nvme": []}
        elif line.startswith("disk_end|") or line.startswith("nvme_end|"):
            cur = None
        elif cur is not None:
            tag, _, rest = line.partition("|")
            if tag in blocks[cur]:
                blocks[cur][tag].append(rest)

    for dev, blk in blocks.items():
        # The drive's own model, not its controller family ("Phison Driven SSDs").
        name = next((re.sub(r"^\s*(Model Number|Device Model)\s*:\s*", "", i) for i in blk["info"]
                     if re.match(r"\s*(Model Number|Device Model)\s*:", i)), None) or next(
            (re.sub(r"^\s*Model Family\s*:\s*", "", i) for i in blk["info"] if "Model Family" in i), dev)
        health = " ".join(blk["health"])
        attrs = _smart_attrs(blk["attr"] + blk["nvme"])
        problems: List[str] = []
        warns: List[str] = []
        if re.search(r"PASSED", health, re.I):
            smart = "ok"
        elif re.search(r"FAILED", health, re.I):
            smart = "bad"
            problems.append("SMART overall health FAILED - replace the disk")
        elif re.search(r"Unavailable|Disabled|unable", health, re.I):
            smart = "info"
            warns.append("SMART not available")
        else:
            smart = "info"
            warns.append("no SMART result")

        def raw(name: str) -> Optional[int]:
            v = attrs.get(name)
            m = re.search(r"\d+", str(v).replace(",", "")) if v is not None else None
            return int(m.group()) if m else None

        # ATA attributes.
        # (attribute, warn at, bad at, text); None = never that level.
        for attr, warn_at, bad_at, text in (
            ("Reallocated_Sector_Ct", 1, 50, "reallocated sector(s)"),
            ("Current_Pending_Sector", None, 1, "unstable sector(s) pending"),
            ("Offline_Uncorrectable", None, 1, "uncorrectable sector(s)"),
            ("Reported_Uncorrectable_Errors", 1, None, "reported uncorrectable error(s)"),
            ("UDMA_CRC_Error_Count", 1, None, "interface CRC error(s) - usually the cable/backplane"),
            ("Command_Timeout", 1, None, "command timeout(s)"),
        ):
            v = raw(attr)
            if v is None:
                continue
            if bad_at is not None and v >= bad_at:
                problems.append(f"{v} {text}")
                smart = "bad"
            elif warn_at is not None and v >= warn_at:
                warns.append(f"{v} {text}")
                smart = "warn" if smart == "ok" else smart
        wear = raw("Media_Wearout_Indicator@v")
        if wear is not None and wear <= 10:
            warns.append(f"wear indicator {wear} - SSD near end of life")
            smart = "warn" if smart == "ok" else smart
        # NVMe (smartctl -A "Key: value" or nvme smart-log lines).
        crit = raw("critical_warning")
        if crit:
            problems.append(f"NVMe critical_warning={crit}")
            smart = "bad"
        used = raw("percentage_used") or raw("Percentage Used")
        if used is not None and used >= 90:
            warns.append(f"NVMe {used}% of rated life used")
            smart = "warn" if smart == "ok" else smart
        spare, spare_t = raw("available_spare") or raw("Available Spare"), raw("available_spare_threshold") or raw("Available Spare Threshold")
        if spare is not None and spare_t is not None and spare <= spare_t:
            problems.append(f"NVMe spare blocks exhausted ({spare}% <= threshold {spare_t}%)")
            smart = "bad"
        media_errs = raw("media_errors") or raw("Media and Data Integrity Errors")
        if media_errs:
            problems.append(f"{media_errs} media/data integrity error(s) - corrupt reads/writes")
            smart = "bad"
        # The NVMe error log counts any command the disk refused (often
        # harmless tool queries), not data errors: media_errors above is the
        # real signal. Shown in the details only.
        log_entries = raw("num_err_log_entries") or raw("Error Information Log Entries")
        # Lab/field units get switched off at the wall now and then (QA1 rooms:
        # 20-35); only a lot of power cuts is a pattern worth flagging.
        unsafe = raw("unsafe_shutdowns") or raw("Unsafe Shutdowns") or raw("Unsafe_Shutdown_Count")
        if unsafe is not None and unsafe > 100:
            warns.append(f"{unsafe} unsafe shutdowns (power cuts corrupt the disk)")
            smart = "warn" if smart == "ok" else smart
        # Drive health score for this disk (worst real disk = the device's score).
        ds = _Score("Drive health")
        if re.search(r"FAILED", health, re.I):
            ds.cap(10, "the disk's own self-test says FAILED")
        # Vendors name these differently (Phison/Kingston: Reallocated_Event_Count,
        # Reported_Uncorrect, CRC_Error_Count).
        realloc = raw("Reallocated_Sector_Ct") or raw("Reallocated_Event_Count")
        if realloc:
            ds.hit(10 if realloc < 10 else 20 if realloc < 50 else 40, f"{realloc} worn-out sectors already replaced")
            if not raw("Reallocated_Sector_Ct"):
                warns.append(f"{realloc} reallocated sector(s)")
                smart = "warn" if smart == "ok" else smart
        for attr, text in (("Reported_Uncorrect", "read errors the disk couldn't fix"), ("CRC_Error_Count", "cable/connection errors")):
            v = raw(attr)
            if v:
                ds.hit(20 if attr == "Reported_Uncorrect" else 10, f"{v} {text}")
                warns.append(f"{v} {text}")
                smart = "warn" if smart == "ok" else smart
        for attr, text in (("Current_Pending_Sector", "unreadable sectors waiting to be replaced"),
                           ("Offline_Uncorrectable", "sectors that can't be read at all")):
            v = raw(attr)
            if v:
                ds.hit(40, f"{v} {text}")
                ds.cap(30)
        for attr, pts, text in (("Reported_Uncorrectable_Errors", 20, "read errors the disk couldn't fix"),
                                ("UDMA_CRC_Error_Count", 10, "cable/connection errors"),
                                ("Command_Timeout", 10, "commands that timed out")):
            v = raw(attr)
            if v:
                ds.hit(pts, f"{v} {text}")
        if crit:
            ds.cap(20, "the disk raised a critical warning")
        if spare is not None and spare_t is not None and spare <= spare_t:
            ds.cap(20, "the disk has run out of spare blocks")
        if media_errs:
            ds.hit(40, f"{media_errs} data integrity errors (corrupt reads/writes)")
        if unsafe is not None and unsafe > 100:
            ds.hit(5, f"{unsafe} unsafe power-offs")
        # Key numbers, shown as labelled values on the Drive health row.
        life, life_text = _life_left(attrs, raw, used)
        if life is not None:
            estimated = "estimated" in life_text
            hint = life_text.split("(", 1)[1].rstrip(")").capitalize() if estimated else ""
            ds.stat("Life left", f"about {life}%" if estimated else f"{life}%",
                    "bad" if life <= 10 else "warn" if life <= 50 else "ok", hint)
            if life < 50:
                ds.hit((50 - life) * 0.6, f"only about {life}% of its rated life left")
            if life <= 25 and not any("life" in w for w in warns):
                warns.append(f"about {life}% life left")
                smart = "warn" if smart == "ok" else smart
        elif life_text:
            ds.missing.append(life_text)
            ds.stat("Life left", "not reported", "info", life_text)
        poh = raw("Power_On_Hours") or raw("power_on_hours") or raw("Power On Hours")
        if poh:
            ds.stat("Power-on time", f"{poh:,} h ({poh / 8760:.1f} years)")
        cycles = raw("Power_Cycle_Count") or raw("power_cycles") or raw("Power Cycles")
        if cycles:
            ds.stat("Power cycles", f"{cycles:,}")
        if unsafe is not None:
            ds.stat("Unsafe power-offs", f"{unsafe:,}", "warn" if unsafe > 100 else "info")
        written = _tb_written(attrs, raw, poh)
        if written:
            ds.stat("Data written", written.lstrip("~").replace(" written", ""))
        temp = raw("Temperature_Celsius") or raw("Airflow_Temperature_Cel") or raw("temperature") or raw("Temperature")
        if temp and 0 < temp < 120:
            ds.stat("Temperature", f"{temp}\u00b0C", "bad" if temp >= 70 else "warn" if temp >= 60 else "ok")
        bad_sectors = sum(raw(a) or 0 for a in ("Reallocated_Sector_Ct", "Current_Pending_Sector", "Offline_Uncorrectable")) \
            or (raw("Reallocated_Event_Count") or 0)
        if any(raw(a) is not None for a in ("Reallocated_Sector_Ct", "Reallocated_Event_Count", "Current_Pending_Sector")):
            ds.stat("Bad / replaced sectors", f"{bad_sectors:,}", "warn" if bad_sectors else "ok")
        if media_errs is not None:
            ds.stat("Data integrity errors", f"{media_errs:,}", "bad" if media_errs else "ok")
        if smart == "info":
            ds.missing.append("the disk's own health report (SMART)")
        tran = next((d.get("TRAN", "") for d in disks if dev.split("/")[-1].startswith(d.get("NAME", "?"))
                     or d.get("NAME", "?").startswith(dev.split("/")[-1])), "")
        per_disk.append((dev, name, tran, ds))

        label = f"Disk health: {dev} {name}".strip()
        detail = "; ".join(problems + warns)
        joined = detail.lower()
        if smart == "bad":
            action = DO_REPLACE_DISK
        elif "crc" in joined and len(warns) == 1:
            action = "Reseat or replace the disk's data cable (interface errors), then check again."
        elif "unsafe shutdowns" in joined and len(warns) == 1:
            action = DO_POWER
        else:
            action = "Plan to replace the disk soon: it is wearing out or reporting errors."
        value = detail or "healthy (SMART ok)"
        if life is not None and "life left" not in value:
            value += f", about {life}% life left"
        items.append(_item(label, value, smart,
                           "\n".join(blk["info"] + blk["health"] + blk["nvme"][:12]), action))

    if smart_missing and disks:
        items.append(_item("Disk health (SMART)", "can't be read on this device", "info",
                           "smartmontools isn't installed on this image, so the disk's own health report isn't available."))
    # The device's drive score: its worst built-in disk (USB sticks don't count).
    builtin = [p for p in per_disk if p[2] != "usb"] or per_disk
    if builtin:
        dev, model, _, score = min(builtin, key=lambda p: p[3].result()["score"])
        score.facts.insert(0, f"{model} ({dev})" if model != dev else dev)
        others = [p for p in builtin if p[3] is not score and p[3].result()["state"] != "ok"]
        score.reasons += [f"other disk {p[0]}: {p[3].result()['grade'].lower()} ({p[3].result()['score']}/100)" for p in others]
    else:
        score = _Score("Drive health")
        if disks:
            score.facts.append(", ".join(f"{d.get('MODEL') or d['NAME']}" for d in disks))
    if smart_missing:
        score.missing.append("the disk's own health report (SMART is not installed on this device)")
    if disks:
        items.insert(0, _item("Disks", ", ".join(
            f"/dev/{d['NAME']} {d.get('MODEL') or '?'} ({d.get('SIZE', '?')})" for d in disks), "info"))

    # Filesystem usage (read-only mounts can't fill up: skipped).
    ro_mounts = {(" ".join(f).split() or [""])[0] for f in _tagged(lines, "ro")}
    for f in _tagged(lines, "df"):
        cols = " ".join(f).split()
        if len(cols) < 7 or cols[0] == "Filesystem" or cols[6] in ro_mounts:
            continue
        use = _int(cols[5].rstrip("%"))
        state = "bad" if use is not None and use >= 95 else "warn" if use is not None and use >= 85 else "ok"
        items.append(_item(f"Free space on {cols[6]}", f"{cols[4]} free of {cols[2]} ({cols[5]} used)", state,
                           "A nearly-full filesystem breaks SWU installs and corrupts downloads." if state != "ok" else "", DO_FREE_SPACE))
    for f in _tagged(lines, "dftmp"):
        cols = " ".join(f).split()
        if len(cols) >= 7 and cols[0] != "Filesystem":
            avail = cols[4]
            avail_kb = None
            m = re.match(r"([\d.]+)([KMGTP]?)", avail)
            if m:
                avail_kb = float(m.group(1)) * {"": 1 / 1024, "K": 1, "M": 1024, "G": 1024 ** 2, "T": 1024 ** 3}.get(m.group(2), 0)
            low = avail_kb is not None and avail_kb < 2 * 1024 * 1024
            items.append(_item("Space to unpack an update (/tmp)", f"{avail} free", "warn" if low else "ok",
                               "swupdate extracts artifacts here; an SWU needs roughly its own size +15%." if low else "", DO_FREE_SPACE))

    # Read-only mounts are normal (the Matrix image's system partition is
    # read-only by design); only the kernel remounting one after errors counts.
    ro = [" ".join(f) for f in _tagged(lines, "ro")]
    ro = [t for t in ro if not ("squashfs" in t or "/snap/" in t or "loop" in t)]
    remounts = _int(_kv(lines).get("ro_remount_30d"), 0) or 0
    ro_log = [l.split("|", 1)[1] for l in lines if l.startswith("rolog|")]
    if remounts:
        score.hit(25, f"a drive was locked read-only after errors ({remounts}x in 30 days)")
        items.append(_item("Drive locked read-only after errors (30 days)", str(remounts), "bad",
                           "The kernel switched a file system to read-only because of disk errors.\n" + "\n".join(ro_log + ro),
                           "Restart the device. If it is still read-only afterwards, replace the disk."))
    elif ro:
        items.append(_item("Read-only drives", ", ".join(t.split()[0] for t in ro), "info",
                           "Mounted read-only on purpose (e.g. the Matrix image's system partition); no errors logged.\n" + "\n".join(ro)))
    for f in _tagged(lines, "fsstate"):
        joined = " ".join(f)
        state = "bad" if re.search(r"with errors|not clean|error", joined, re.I) and "Errors behavior" not in joined else "ok"
        if "Filesystem state" in joined:
            if state == "bad":
                score.hit(25, "the file system is damaged")
            items.append(_item("File system", joined.split(":", 1)[-1].strip(), state, joined,
                               "The file system is damaged: restart so it gets repaired, and replace the disk if it comes back."))
    for f in _tagged(lines, "lostfound"):
        if len(f) >= 2 and (_int(f[1]) or 0) > 0:
            score.hit(5, "damaged files were recovered in the past")
            items.append(_item("Recovered damaged files", f"{f[1]} in {f[0]}", "warn",
                               "fsck left recovered files in lost+found - the filesystem was corrupted at some point.",
                               "The disk was corrupted before. If other disk checks also warn, replace the disk."))
    for f in _tagged(lines, "mmc"):
        # "mmc|<life_time path>|0x01 0x02" (older collectors glued the values: "0x010x02").
        if len(f) >= 2:
            raw_life = f[1].strip()
            vals = [int(v, 16) for v in re.findall(r"0[xX][0-9a-fA-F]{2}", raw_life)]
            worst = max(vals) if vals else 0
            state = "bad" if worst >= 0x0A else "warn" if worst >= 0x08 else "ok"
            if worst:
                # eMMC life_time: 0x01 = 0-10% used ... 0x0A = 90-100%, 0x0B = beyond.
                left = max(0, 100 - worst * 10)
                score.stat("Flash life left", f"about {left}%", "bad" if left <= 10 else "warn" if left <= 30 else "ok",
                           "Built-in eMMC flash (reported in 10% steps).")
                if worst >= 0x0A:
                    score.cap(30, "the built-in flash storage is worn out")
                elif worst >= 0x08:
                    score.hit(20, "the built-in flash storage is wearing out")
            parts = f[0].split("/")
            name = parts[-3] if len(parts) >= 3 and parts[-2] == "device" else parts[-2] if len(parts) >= 2 else f[0]
            items.append(_item(f"Flash storage wear ({name})", f"{raw_life} (0x0B = worn out)", state, "",
                               "The built-in flash storage is wearing out: plan to replace the device."))

    errs = _int(_kv(lines).get("disk_err_30d"), 0) or 0
    err_lines = [l.split("|", 1)[1] for l in lines if l.startswith("err|")]
    if errs:
        score.hit(30, f"{errs} disk read/write errors in the last 30 days")
        items.append(_item("Disk read/write errors (30 days)", str(errs), "bad",
                           "I/O / media / filesystem errors in the kernel log - explains corrupt downloads and failed updates.\n"
                           + "\n".join(err_lines), DO_REPLACE_DISK))
    if not journal_readable:
        score.missing.append("the disk error history (needs the sudo password)")
        items.append(_item("Error history", "not readable", "info",
                           "Run with a sudo password for disk/hardware error history."))
    if not items:
        items.append(_item("Storage", "no block devices found", "info"))
    return {"id": "disk", "title": "Storage", "items": items, "score": score.result()}


def _eval_net(lines: List[str]) -> Dict[str, Any]:
    items = []
    d = _kv(lines)

    # ip -s link: "N: name: <...> ..." then link/..., RX: <hdr>, <vals>, TX: <hdr>, <vals>.
    stats: Dict[str, Dict[str, int]] = {}
    ip_lines = [l[3:] for l in lines if l.startswith("ip|")]
    name = None
    i = 0
    while i < len(ip_lines):
        m = re.match(r"^\s*\d+:\s*([^:@\s]+)", ip_lines[i])
        if m:
            name = m.group(1)
            stats.setdefault(name, {})
        elif name and ip_lines[i].strip().startswith("RX:") and i + 1 < len(ip_lines):
            vals = ip_lines[i + 1].split()
            if len(vals) >= 4:
                stats[name].update(rx_pkts=_int(vals[1], 0) or 0, rx_err=_int(vals[2], 0) or 0, rx_drop=_int(vals[3], 0) or 0)
            i += 1
        elif name and ip_lines[i].strip().startswith("TX:") and i + 1 < len(ip_lines):
            vals = ip_lines[i + 1].split()
            if len(vals) >= 4:
                stats[name].update(tx_pkts=_int(vals[1], 0) or 0, tx_err=_int(vals[2], 0) or 0, tx_drop=_int(vals[3], 0) or 0)
            i += 1
        i += 1

    route = [l for l in lines if l.startswith("route|")]
    gw_if = None
    if route:
        m = re.search(r"\bdev\s+(\S+)", route[0])
        gw_if = m.group(1) if m else None

    for f in _tagged(lines, "iface"):
        if len(f) < 6 or f[0] == "lo":
            continue
        iname, oper, speed, carrier = f[0], f[1], _int(f[2]), f[3]
        st = stats.get(iname, {})
        errs = (st.get("rx_err", 0) + st.get("tx_err", 0))
        drops = (st.get("rx_drop", 0) + st.get("tx_drop", 0))
        problems = []
        state = "ok"
        if iname == gw_if and oper != "up":
            problems.append("disconnected (this is the main network port)")
            state = "bad"
        elif oper != "up":
            problems.append("not connected")
            state = "info"
        if oper == "up" and speed is not None and 0 < speed < 1000:
            problems.append(f"running at only {speed} Mbps (should be 1000+)")
            state = "warn"
        pkts = st.get("rx_pkts", 0) + st.get("tx_pkts", 0)
        # A handful of bad packets among millions is normal; flag a real rate
        # (more than 1 in 10,000) or a large count.
        if errs and (errs > 1000 or (pkts and errs / pkts > 0.0001)):
            problems.append(f"{errs} damaged packets")
            state = "warn" if state in ("ok", "info") else state
        elif errs:
            problems.append(f"{errs} damaged packets (normal level)")
        if drops > 5000:
            problems.append(f"{drops} dropped packets")
            state = "warn" if state in ("ok", "info") else state
        elif drops:
            problems.append(f"{drops} dropped")
        val = "connected" if oper == "up" else (oper or "?")
        if oper == "up" and speed and speed > 0:
            val += f" at {speed} Mbps"
        if carrier == "0":
            val += " (no cable signal)"
        items.append(_item(f"Network port {iname}", (val + (" - " + "; ".join(problems) if problems else "")), state,
                           f"errors rx/tx {st.get('rx_err', 0)}/{st.get('tx_err', 0)}, dropped rx/tx {st.get('rx_drop', 0)}/{st.get('tx_drop', 0)}",
                           DO_CHECK_CABLE))

    if not route:
        items.append(_item("Route to the outside network", "none", "warn", "No default gateway - updates and downloads can't leave the device.",
                           "Check the device's network settings (gateway) and that the cable is plugged in."))
    ping = [l.split("|", 1)[1] for l in lines if l.startswith("ping|")]
    if ping:
        m = re.search(r"(\d+)% packet loss", ping[0])
        loss = int(m.group(1)) if m else None
        if loss is not None:
            state = "bad" if loss == 100 else "warn" if loss > 0 else "ok"
            items.append(_item("Reaches the router (ping)", f"{d.get('gw')} - {loss}% lost", state,
                               "\n".join(ping) + ("\nCan't reach the default gateway." if loss == 100 else ""), DO_CHECK_CABLE))
    flaps = _int(d.get("link_flaps_30d"), 0) or 0
    if flaps:
        # Every reboot or replug also drops the link once, so only a lot counts.
        items.append(_item("Network disconnects (30 days)", str(flaps), "bad" if flaps > 100 else "warn" if flaps > 30 else "info",
                           "Link up/down messages in the kernel log. A few come from reboots and replugging; "
                           "dozens point at cable, SFP or switch port trouble.",
                           "Replace the network cable (or fiber module) and try another switch port: the link keeps dropping."))
    if not items:
        items.append(_item("Network", "no interfaces found", "info"))
    return {"id": "net", "title": "Network", "items": items}


def _eval_svc(lines: List[str], ident: Dict[str, str], swu_port: int) -> Dict[str, Any]:
    items = []
    failed = [" ".join(f) for f in _tagged(lines, "failed") if f and f[0].strip()]
    if failed:
        items.append(_item("Background services that failed", str(len(failed)), "warn", "\n".join(failed), DO_RESTART))
    else:
        items.append(_item("Background services that failed", "none", "ok"))
    names = {"matrix-api": "Matrix API service", "barco-nms": "Barco NMS service"}
    for f in _tagged(lines, "svc"):
        if len(f) < 2:
            continue
        is_matrix = ident.get("is_matrix") == "1"
        state = "ok" if f[1] == "active" else ("bad" if is_matrix else "info")
        items.append(_item(names.get(f[0], f"Service {f[0]}"), "running" if f[1] == "active" else (f[1] or "unknown"), state,
                           f"systemd unit {f[0]}: {f[1]}. " + ("SWU updates and facility checks need this running." if is_matrix else "Not a Matrix image."),
                           "Restart the device. If the service still doesn't start, collect logs (Logs tab) and escalate."))
    d = _kv(lines)
    is_matrix = ident.get("is_matrix") == "1"
    if is_matrix:
        reinstall = "Restart the device. If it's still missing, the software image is damaged and needs reinstalling."
        if d.get("swupdate_client") == "1":
            items.append(_item("Update tool (swupdate-client)", "installed", "ok"))
        else:
            items.append(_item("Update tool (swupdate-client)", "missing", "bad", "SWU updates can't run without it.", reinstall))
        if d.get("swupdate_running") == "1":
            items.append(_item("Update service (swupdate)", "running", "ok"))
        else:
            items.append(_item("Update service (swupdate)", "not running", "bad",
                               "swupdate-client hands the SWU to this daemon; installs fail without it.", reinstall))
        listen = {f[0] for f in _tagged(lines, "listen") if f}
        if str(swu_port) in listen:
            items.append(_item(f"Update web port {swu_port}", "listening", "ok"))
        else:
            items.append(_item(f"Update web port {swu_port}", "not listening", "info",
                               "Only the router's SWU forward uses it; installs from this app go over SSH."))
    return {"id": "svc", "title": "Software services & update tool", "items": items}


def _eval_kerr(lines: List[str]) -> Dict[str, Any]:
    d = _kv(lines)
    count = _int(d.get("kern_err_7d"), 0) or 0
    err_lines = [l.split("|", 1)[1] for l in lines if l.startswith("kerr|")]
    # A few kernel errors are normal (driver noise); only many are a sign.
    items = [_item("System error messages (7 days)", str(count),
                   "warn" if count > 20 else "info" if count else "ok",
                   "\n".join(err_lines) if err_lines else "",
                   "Many system errors: export this report and the logs (Logs tab) and escalate.")]
    return {"id": "kerr", "title": "Recent system errors", "items": items}


def collect_device_health(
    target: SSHTarget,
    label: str,
    services: tuple = ("matrix-api", "barco-nms"),
    swu_port: int = 8080,
) -> Dict[str, Any]:
    """SSH into ``target`` and return a hardware health report dict.

    Never raises: a failed connection or collection is reported in
    ``state``/``issues``. The report is a list of sections of checked items
    ({id,title,state,items:[{label,value,state,detail}],raw?}) plus a flat
    ``issues`` list - everything needed to render or export it.
    """
    started = time.monotonic()
    report: Dict[str, Any] = {
        "state": "unreachable",
        "label": label,
        "target": {"host": target.host, "port": target.port},
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sudo": bool(target.sudo_password),
        "issues": [],
        "sections": [],
    }
    try:
        client = connect(target)
    except SSHError as exc:
        report["issues"].append(f"Can't SSH in: {exc}")
        return report
    notes: List[str] = []
    args = (*services, str(swu_port))
    try:
        sudo_pw = target.sudo_password
        if sudo_pw and not _sudo_works(client, sudo_pw):
            # Wrong password / not a sudoer: still report what an ordinary
            # account can read.
            notes.append("sudo was refused, so this ran without root: SMART, some disk details and the "
                         "kernel error history may be missing. Enter the right sudo password for a full check.")
            report["sudo"] = False
            sudo_pw = None
        secs = _split_sections(_exec_collector(client, sudo_pw, args))
    except Exception as exc:  # noqa: BLE001
        report["state"] = "error"
        report["issues"].append(f"Health check failed: {exc}")
        return report
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    if not secs:
        report["state"] = "error"
        report["issues"].append("The device didn't return a health report (it needs bash).")
        return report
    if not target.sudo_password:
        notes.append("No sudo password, so this ran without root: SMART and the kernel error history may be missing.")
    report["notes"] = notes

    ident = _kv(secs.get("IDENT", []))
    # Drives and memory first: they're what the scores are about.
    sections = [
        _eval_disk(secs.get("DISK", []), journal_ok(ident)),
        _eval_mem(secs.get("MEM", []), secs.get("CPU", []), journal_ok(ident)),
        _eval_cpu(secs.get("CPU", []), ident),
        _eval_net(secs.get("NET", [])),
        _eval_svc(secs.get("SVC", []), ident, swu_port),
        _eval_ident(secs.get("IDENT", [])),
        _eval_kerr(secs.get("KERR", [])),
    ]
    report["scores"] = {"disk": sections[0].pop("score"), "memory": sections[1].pop("score")}
    report["glance"] = build_glance(sections)
    report["device"] = build_device(secs)
    # Restarts are a power/hardware sign: shown with the hardware, not system info.
    ident_sec, cpu_sec = sections[5], sections[2]
    cpu_sec["items"] += [i for i in ident_sec["items"] if i["label"].startswith("Restarts")]
    ident_sec["items"] = [i for i in ident_sec["items"] if not i["label"].startswith("Restarts")]
    for sec in sections:
        worst = max((_RANK.get(i["state"], 0) for i in sec["items"]), default=0)
        sec["state"] = {0: "ok", 1: "warn", 2: "bad"}[worst]
        sec["group"] = "health" if sec["id"] in HEALTH_SECTIONS else "other"
        raw_lines = secs.get(sec["id"].upper(), [])
        if raw_lines and "raw" not in sec:
            # Bounded raw output for the details fold (no secrets - sudo pw redacted).
            sec["raw"] = "\n".join(raw_lines)[:8000]
    report["sections"] = sections
    finish_report(report, sections)
    report["duration_s"] = round(time.monotonic() - started, 1)
    return report


# The device's health: what the verdict, issues and "What to do" are about.
# Everything else (services, update tool, system info, software errors) is
# shown under "Other details" and doesn't change the result.
HEALTH_SECTIONS = ("disk", "mem", "cpu", "net")


def finish_report(report: Dict[str, Any], sections: List[Dict[str, Any]]) -> None:
    health = [s for s in sections if s.get("group", "health") == "health"]
    report["issues"] = [f"{s['title']}: {it['label']} - {it['value']}"
                        for s in health for it in s["items"] if it["state"] in ("bad", "warn")]
    report["actions"] = report_actions(health)
    report["state"] = _STATE[max((_RANK.get(s["state"], 0) for s in health), default=0)]


def build_device(secs: Dict[str, List[str]]) -> List[Dict[str, str]]:
    """The "Device" summary at the top of the report: what this box is."""
    ident, cpu = _kv(secs.get("IDENT", [])), _kv(secs.get("CPU", []))
    out: List[Dict[str, str]] = []

    def add(label: str, value: Optional[str]) -> None:
        if value and value.strip():
            out.append({"label": label, "value": " ".join(value.split())})

    add("Name", ident.get("hostname"))
    add("Model", " ".join(p for p in (ident.get("vendor"), ident.get("product")) if p))
    serial = ident.get("serial", "")
    if serial and not re.fullmatch(r"(?i)(default string|to be filled.*|none|0+|system serial number)", serial):
        add("Serial number", serial)
    add("IP address", ", ".join(f"{f[1].split('/')[0]} ({f[0]})" for f in _tagged(secs.get("IDENT", []), "ipaddr") if len(f) >= 2))
    sw = ident.get("sw_version") or ""
    build = ident.get("sw_build") or ""
    add("Matrix software", f"{sw} ({build})" if sw and build else sw or build)
    add("Operating system", ident.get("os"))
    cores = _int(cpu.get("cores"))
    add("CPU", f"{cpu.get('model', '')}{f', {cores} cores' if cores else ''}".strip(", "))
    for f in _tagged(secs.get("MEM", []), "free"):
        row = " ".join(f).split()
        if row and row[0].rstrip(":") == "Mem" and len(row) >= 2 and _int(row[1]):
            add("Memory", f"{_int(row[1]) / 1024:.1f} GB")
    disks = [dict(re.findall(r'(\w+)="([^"]*)"', f[0])) for f in _tagged(secs.get("DISK", []), "lsblk")]
    add("Disk", ", ".join(f"{d.get('MODEL') or d.get('NAME')} ({d.get('SIZE')})" for d in disks
                          if d.get("TYPE") == "disk" and d.get("TRAN") != "usb"))
    add("Running for", ident.get("uptime"))
    return out


def build_glance(sections: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """The "at a glance" strip: free space, free memory, network link, uptime."""
    items = [it for s in sections for it in s["items"]]
    out: List[Dict[str, str]] = []

    def add(label: str, it: Dict[str, Any]) -> None:
        out.append({"label": label, "value": it["value"], "state": it["state"]})

    for it in [i for i in items if i["label"].startswith("Free space on ")][:3]:
        add(it["label"].replace("Free space on ", "Drive "), it)
    for it in items:
        if it["label"].startswith("Space to unpack"):
            add("Free for updates (/tmp)", it)
        elif it["label"] == "Free memory":
            add("Free memory", it)
    ports = [i for i in items if i["label"].startswith("Network port")]
    main = next((i for i in ports if i["value"].startswith("connected")), ports[0] if ports else None)
    if main:
        out.append({"label": main["label"].replace("Network port", "Network"), "value": main["value"].split(" - ")[0], "state": main["state"]})
    return out


def report_actions(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The "What to do" list: each distinct next step once, problems first,
    with the checks that led to it."""
    out: Dict[str, Dict[str, Any]] = {}
    for level in ("bad", "warn"):
        for sec in sections:
            for it in sec["items"]:
                if it["state"] == level and it.get("action"):
                    a = out.setdefault(it["action"], {"text": it["action"], "state": level, "because": []})
                    a["because"].append(it["label"])
    return list(out.values())


if __name__ == "__main__":
    # Manual smoke test: python -m matrix_deploy.health_check <host> <user> [port]
    import getpass
    import json
    import sys

    _host = sys.argv[1]
    _user = sys.argv[2]
    _port = int(sys.argv[3]) if len(sys.argv) > 3 else 22
    _pw = getpass.getpass("SSH password: ")
    _sudo = getpass.getpass("Sudo password (blank = same as SSH): ") or _pw
    print(json.dumps(collect_device_health(
        SSHTarget(host=_host, port=_port, username=_user, password=_pw, sudo_password=_sudo),
        f"{_user}@{_host}:{_port}"), indent=2))
