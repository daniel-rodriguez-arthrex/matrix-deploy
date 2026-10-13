"""Health Check parser, fed fixture collector output (no Linux box needed)."""

import pytest

from matrix_deploy import health_check as hc
from matrix_deploy.ssh_client import SSHError, SSHTarget

from fakes import FakeChannel, FakeSSHClient

ATTR = "attr|{id:>3} {name:<24} 0x0032   {v:>3}   100   000    Old_age   Always       -       {raw}"


def attr(i, name, raw, v=100):
    return ATTR.format(id=i, name=name, raw=raw, v=v)


TROUBLED = "\n".join([
    "noise before the first section",
    "@@@IDENT",
    "hostname=or6", "kernel=6.1.0 x86_64", "os=Matrix OS 2.0", "uptime=3 days, 2 hours", "uptime_s=266400.5",
    "load=9.0 8.0 40.0 2/300 1234", "vendor=Advantech", "product=MIC-770", "bios=1.2", "serial=SN12345",
    "ipaddr|eno1|10.1.6.13/24", "sw_version=2.0.0", "sw_build=860-0108-07-0_2.0.0", "is_matrix=1", "journal_ok=1",
    "reboots_30d=25",
    "@@@CPU",
    "cores=4", "model=Intel(R) Core(TM) i7", "thermal|x86_pkg_temp|92000", "thermal|acpitz|76000",
    "thermal|bad|notanumber", "sensors|coretemp-isa-0000", "sensors|Core 0: +92.0C", "hw_err_30d=2",
    "pcie_err_30d=3", "hwerr|mce: [Hardware Error]: CPU 0",
    "@@@MEM",
    "free|              total        used        free      shared  buff/cache   available",
    "free|Mem:           7800        7700          50          10          50          80",
    "free|Swap:          2000        1900         100",
    "oom_30d=7", "oom|Out of memory: Killed process 123", "edac|mc0|12|1", "edac_present=1", "mem_err_30d=4",
    "@@@DISK",
    'lsblk|NAME="sda" MODEL="KINGSTON SA400" SIZE="240G" TYPE="disk" TRAN="sata" ROTA="0"',
    'lsblk|NAME="nvme0n1" MODEL="Samsung 970" SIZE="500G" TYPE="disk" TRAN="nvme" ROTA="0"',
    'lsblk|NAME="sdb" MODEL="USB Stick" SIZE="16G" TYPE="disk" TRAN="usb" ROTA="0"',
    'lsblk|NAME="sr0" MODEL="DVD" SIZE="1G" TYPE="rom" TRAN="sata" ROTA="1"',
    "df|Filesystem     Type  Size  Used Avail Use% Mounted on",
    "df|/dev/sda2      ext4   50G   48G  1.0G  97% /",
    "df|/dev/sda3      ext4  100G   86G   14G  86% /data",
    "df|/dev/sda1      ext4    1G  0.5G  0.5G  50% /media/rfs/ro",
    "df|short line",
    "dftmp|tmpfs tmpfs 3.9G 3.0G 900M 77% /tmp",
    "ro|/media/rfs/ro /dev/sda1 ext4 ro,relatime", "ro|/snap/core /dev/loop0 squashfs ro",
    "ro_remount_30d=1", "rolog|EXT4-fs (sda3): Remounting filesystem read-only",
    "lostfound|/lost+found|3", "lostfound|/data/lost+found|0",
    "fsstate|Filesystem state:         clean with errors", "fsstate|Errors behavior:          Continue",
    "disk_begin|/dev/sda",
    "info|Model Family:     Phison Driven SSDs", "info|Device Model:     KINGSTON SA400", "info|Serial Number: X1",
    "health|SMART overall-health self-assessment test result: PASSED",
    attr(1, "Raw_Read_Error_Rate", 0), attr(5, "Reallocated_Sector_Ct", 60), attr(9, "Power_On_Hours", 50705),
    attr(12, "Power_Cycle_Count", 300), attr(167, "Average_Erase_Count", 2310),
    attr(174, "Unsafe_Shutdown_Count", 150), attr(187, "Reported_Uncorrect", 2),
    attr(194, "Temperature_Celsius", "65 (Min/Max 23/67)"), attr(199, "UDMA_CRC_Error_Count", 3),
    attr(197, "Current_Pending_Sector", 1), attr(241, "Total_LBAs_Written", 5),
    "attr|garbage line",
    "disk_end|/dev/sda",
    "nvme_begin|/dev/nvme0",
    "nvme|critical_warning : 4", "nvme|temperature : 45 C", "nvme|available_spare : 5%",
    "nvme|available_spare_threshold : 10%", "nvme|percentage_used : 95%", "nvme|media_errors : 3",
    "nvme|num_err_log_entries : 20", "nvme|unsafe_shutdowns : 20", "nvme|power_cycles : 100",
    "nvme|power_on_hours : 1,000", "nvme|Data Units Written : 3,000,000 [1.53 TB]",
    "nvme_end|/dev/nvme0",
    "disk_begin|/dev/sdb",
    "health|SMART support is: Unavailable - device lacks SMART capability.",
    "disk_end|/dev/sdb",
    "disk_err_30d=4", "err|ata1.00: failed command: READ FPDMA QUEUED",
    "@@@NET",
    "ip|1: lo: <LOOPBACK,UP> mtu 65536",
    "ip|    RX: bytes  packets  errors  dropped missed  mcast",
    "ip|    100        10       0       0       0       0",
    "ip|2: eno1: <BROADCAST,UP> mtu 1500",
    "ip|    link/ether 00:04:a5:00:00:01",
    "ip|    RX: bytes  packets  errors  dropped missed  mcast",
    "ip|    1000000    10000    5       6000    0       0",
    "ip|    TX: bytes  packets  errors  dropped carrier collsns",
    "ip|    1000000    10000    0       0       0       0",
    "ip|3: eno2@if5: <BROADCAST> mtu 1500",
    "ip|    RX: bytes packets errors dropped",
    "ip|    0 1000000 3 10",
    "ip|    TX: bytes packets errors dropped",
    "ip|    short",
    "iface|lo|unknown|0|1|65536|00:00:00:00:00:00", "iface|eno1|up|100|1|1500|00:04:a5:00:00:01",
    "iface|eno2|down||0|1500|00:04:a5:00:00:02", "iface|short|x",
    "route|default via 10.1.6.1 dev eno1 proto static", "gw=10.1.6.1",
    "ping|2 packets transmitted, 1 received, 50% packet loss, time 1001ms",
    "ping|rtt min/avg/max/mdev = 0.3/0.4/0.5/0.1 ms",
    "link_flaps_30d=150",
    "@@@SVC",
    "failed|nms-sync.service loaded failed failed Sync", "failed| ",
    "svc|matrix-api|active", "svc|barco-nms|failed", "svc|x",
    "swupdate_client=0", "swupdate_running=0", "listen|22",
    "@@@KERR",
    "kern_err_7d=30", "kerr|some error",
])

HEALTHY = "\n".join([
    "@@@IDENT", "hostname=box", "os=Debian 12", "serial=Default string", "is_matrix=0", "journal_ok=0",
    "load=0.1 0.1 0.1",
    "@@@CPU", "cores=2", "model=Celeron", "thermal|cpu|45000",
    "@@@MEM",
    "free|Mem:   16000   2000   12000   10   2000   13000",
    "free|Swap:  0   0   0",
    "oom_30d=0", "edac_present=0", "mem_err_30d=0",
    "@@@DISK",
    'lsblk|NAME="sda" MODEL="" SIZE="1T" TYPE="disk" TRAN="sata" ROTA="1"',
    "df|/dev/sda1 ext4 1T 100G 900G 10% /",
    "dftmp|tmpfs tmpfs 8G 1M 8G 1% /tmp",
    "smart_missing=1", "disk_err_30d=0", "ro_remount_30d=0",
    "@@@NET",
    "iface|eth0|up|1000|1|1500|aa:bb:cc:dd:ee:ff",
    "route|default via 192.168.1.1 dev eth0", "gw=192.168.1.1",
    "ping|2 packets transmitted, 2 received, 0% packet loss",
    "link_flaps_30d=2",
    "@@@SVC", "svc|matrix-api|inactive", "swupdate_client=1",
    "@@@KERR", "kern_err_7d=0",
])

TARGET = SSHTarget(host="10.0.0.1", port=206, username="arthrex", password="pw", sudo_password="sudo")


def run(monkeypatch, output, sudo_ok=True, target=TARGET):
    client = FakeSSHClient(lambda cmd: ("", 0 if sudo_ok else 1) if cmd.endswith("true 2>/dev/null") else (output, 0))
    monkeypatch.setattr(hc, "connect", lambda t: client)
    report = hc.collect_device_health(target, "OR 6")
    return report, client


def items(report, sec_id):
    return {i["label"]: i for i in next(s for s in report["sections"] if s["id"] == sec_id)["items"]}


def test_troubled_device(monkeypatch):
    report, client = run(monkeypatch, TROUBLED)
    assert client.closed and client.stdins[0].data == "sudo\n"
    collector_stdin = client.stdins[1].data
    assert collector_stdin.startswith("sudo\n") and "@@@%s" in collector_stdin and "__DISK_ERR_RE__" not in collector_stdin
    assert client.commands[1].startswith("sudo -k -S -p '' bash -s -- matrix-api barco-nms 8080")
    assert report["state"] == "error" and report["sudo"] and report["notes"] == []
    assert [s["id"] for s in report["sections"]] == ["disk", "mem", "cpu", "net", "svc", "ident", "kerr"]
    assert all(s["group"] == ("health" if s["id"] in hc.HEALTH_SECTIONS else "other") for s in report["sections"])

    # Every warning or problem tells the field tech what to do.
    for sec in report["sections"]:
        for it in sec["items"]:
            assert it["state"] not in ("warn", "bad") or it["action"], it["label"]

    disk = items(report, "disk")
    sda = disk["Disk health: /dev/sda KINGSTON SA400"]
    assert sda["state"] == "bad" and "60 reallocated sector(s)" in sda["value"] and sda["action"] == hc.DO_REPLACE_DISK
    assert "about 23% life left" in sda["value"]
    assert disk["Disk health: /dev/nvme0 /dev/nvme0"]["state"] == "bad"
    assert disk["Disk health: /dev/sdb /dev/sdb"]["state"] == "info"
    assert disk["Disks"]["value"].startswith("/dev/sda KINGSTON SA400 (240G)")
    assert disk["Free space on /"]["state"] == "bad" and disk["Free space on /data"]["state"] == "warn"
    assert "Free space on /media/rfs/ro" not in disk
    assert disk["Space to unpack an update (/tmp)"]["state"] == "warn"
    assert disk["Drive locked read-only after errors (30 days)"]["state"] == "bad"
    assert disk["File system"]["state"] == "bad" and disk["Recovered damaged files"]["state"] == "warn"
    assert disk["Disk read/write errors (30 days)"]["value"] == "4"

    ds = report["scores"]["disk"]
    assert ds["grade"] == "Failing" and ds["facts"][0] == "Samsung 970 (/dev/nvme0)" or ds["facts"][0].endswith(")")
    assert any("other disk" in r for r in ds["reasons"])
    mem = report["scores"]["memory"]
    assert mem["score"] <= 20 and mem["grade"] == "Failing"
    assert "1 uncorrectable memory errors" in mem["reasons"]
    m = items(report, "mem")
    assert m["Free memory"]["state"] == "bad" and m["Swap space"]["state"] == "warn"
    assert m["Memory errors (ECC counters)"]["state"] == "bad"
    assert m["Programs killed for lack of memory (30 days)"]["state"] == "bad"

    cpu = items(report, "cpu")
    assert cpu["CPU busy (load 1/5/15 min)"]["state"] == "bad" and cpu["Temperatures"]["state"] == "bad"
    assert cpu["Hardware error reports (30 days)"]["state"] == "bad"
    assert cpu["Internal bus (PCIe) errors (30 days)"]["state"] == "info"
    assert cpu["Restarts (last 30 days)"]["state"] == "warn"  # moved here from System
    assert "Restarts (last 30 days)" not in items(report, "ident")
    cpu_sec = next(s for s in report["sections"] if s["id"] == "cpu")
    assert cpu_sec["raw"].startswith("coretemp")

    net = items(report, "net")
    eno1 = net["Network port eno1"]
    assert eno1["state"] == "warn" and "only 100 Mbps" in eno1["value"] and "5 damaged packets" in eno1["value"]
    assert "6000 dropped packets" in eno1["value"]
    eno2 = net["Network port eno2"]
    assert eno2["state"] == "info" and "(no cable signal)" in eno2["value"] and "(normal level)" in eno2["value"]
    assert net["Reaches the router (ping)"]["state"] == "warn"
    assert net["Network disconnects (30 days)"]["state"] == "bad"

    svc = items(report, "svc")
    assert svc["Background services that failed"]["value"] == "1"
    assert svc["Barco NMS service"]["state"] == "bad" and svc["Matrix API service"]["value"] == "running"
    assert svc["Update tool (swupdate-client)"]["state"] == "bad"
    assert svc["Update service (swupdate)"]["state"] == "bad"
    assert svc["Update web port 8080"]["state"] == "info"
    assert items(report, "kerr")["System error messages (7 days)"]["state"] == "warn"

    # Verdict, issues and actions come from the health sections only.
    assert not any(i.startswith("Software services") for i in report["issues"])
    assert report["actions"][0]["state"] == "bad"
    assert {a["text"] for a in report["actions"]} >= {hc.DO_REPLACE_DISK, hc.DO_REPLACE_RAM, hc.DO_COOLING}
    assert len({a["text"] for a in report["actions"]}) == len(report["actions"])

    device = {d["label"]: d["value"] for d in report["device"]}
    assert device["Serial number"] == "SN12345" and device["IP address"] == "10.1.6.13 (eno1)"
    assert device["Matrix software"] == "2.0.0 (860-0108-07-0_2.0.0)" and device["Memory"] == "7.6 GB"
    assert device["Disk"] == "KINGSTON SA400 (240G), Samsung 970 (500G)" and device["CPU"] == "Intel(R) Core(TM) i7, 4 cores"
    glance = [g["label"] for g in report["glance"]]
    assert glance[:2] == ["Drive /", "Drive /data"] and "Free memory" in glance and "Network eno1" in glance


def test_healthy_device_without_sudo(monkeypatch):
    target = SSHTarget(host="h", port=22, username="u", password="pw")
    report, client = run(monkeypatch, HEALTHY, target=target)
    assert client.commands[0].startswith("bash -s -- ")
    assert report["state"] == "healthy" and report["issues"] == [] and report["actions"] == []
    assert "No sudo password" in report["notes"][0]
    disk = items(report, "disk")
    assert disk["Disk health (SMART)"]["state"] == "info" and disk["Error history"]["value"] == "not readable"
    assert any("SMART is not installed" in m for m in report["scores"]["disk"]["missing"])
    assert report["scores"]["memory"]["grade"] == "Good"
    assert any("needs the sudo password" in m for m in report["scores"]["memory"]["missing"])
    assert items(report, "svc")["Matrix API service"]["state"] == "info"
    assert items(report, "ident")["Matrix image"]["value"] == "not detected"
    device = {d["label"] for d in report["device"]}
    assert "Serial number" not in device and "Matrix software" not in device


def test_sudo_refused_and_failures(monkeypatch):
    report, client = run(monkeypatch, HEALTHY, sudo_ok=False)
    assert not report["sudo"] and "sudo was refused" in report["notes"][0]
    assert client.commands[1].startswith("bash -s")
    report, _ = run(monkeypatch, "no sections here")
    assert report["state"] == "error" and "needs bash" in report["issues"][0]

    def boom(t):
        raise SSHError("No answer")

    monkeypatch.setattr(hc, "connect", boom)
    report = hc.collect_device_health(TARGET, "x")
    assert report["state"] == "unreachable" and report["issues"] == ["Can't SSH in: No answer"]
    client = FakeSSHClient(lambda cmd: RuntimeError("channel died") if "bash" in cmd else ("", 0))
    monkeypatch.setattr(hc, "connect", lambda t: client)
    report = hc.collect_device_health(TARGET, "x")
    assert report["state"] == "error" and "Health check failed: channel died" in report["issues"][0]


def test_exec_collector_timeout(monkeypatch):
    class Stuck(FakeChannel):
        def exit_status_ready(self):
            return False

        def close(self):
            raise OSError("already closed")

    class Client(FakeSSHClient):
        def exec_command(self, command, get_pty=False):
            stdin, stdout, stderr = super().exec_command(command)
            stdout.channel = Stuck()
            return stdin, stdout, stderr

    ticks = iter([0, 0, 1000])
    monkeypatch.setattr(hc.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    with pytest.raises(SSHError, match="took too long"):
        hc._exec_collector(Client(), None, timeout=10)


def test_sudo_works_exception():
    class Bad(FakeChannel):
        def recv_exit_status(self):
            raise OSError("gone")

    class Client(FakeSSHClient):
        def exec_command(self, command, get_pty=False):
            stdin, stdout, stderr = super().exec_command(command)
            stdout.channel = Bad()
            return stdin, stdout, stderr

    assert not hc._sudo_works(Client(), "pw")


def _disk(*lines, journal=True):
    return {i["label"]: i for i in hc._eval_disk(list(lines), journal)["items"]}


def test_disk_action_variants():
    crc = _disk("disk_begin|/dev/sda", "health|PASSED", attr(199, "UDMA_CRC_Error_Count", 3), "disk_end|/dev/sda")
    assert "data cable" in crc["Disk health: /dev/sda /dev/sda"]["action"]
    unsafe = _disk("disk_begin|/dev/sda", "health|PASSED", attr(174, "Unsafe_Shutdown_Count", 500), "disk_end|/dev/sda")
    assert unsafe["Disk health: /dev/sda /dev/sda"]["action"] == hc.DO_POWER
    failed = hc._eval_disk(["disk_begin|/dev/sda", "info|Model Family: Old Disk", "health|result: FAILED",
                            attr(5, "Reallocated_Event_Count", 4), attr(177, "Wear_Leveling_Count", 5, v=5),
                            attr(188, "Command_Timeout", 2), attr(198, "Offline_Uncorrectable", 1),
                            attr(196, "Reported_Uncorrectable_Errors", 1), attr(184, "CRC_Error_Count", 2),
                            attr(233, "Media_Wearout_Indicator", 5, v=5),
                            attr(246, "Host_Writes_32MiB", 3_000_000), "disk_end|/dev/sda"], True)
    item = failed["items"][0]
    assert item["label"] == "Disk health: /dev/sda Old Disk" and item["state"] == "bad"
    assert "4 reallocated sector(s)" in item["value"] and "wear indicator 5" in item["value"]
    score = failed["score"]
    assert score["grade"] == "Failing" and any(s["label"] == "Data written" and "TB" in s["value"] for s in score["stats"])
    info = _disk("disk_begin|/dev/sda", "disk_end|/dev/sda")
    assert info["Disk health: /dev/sda /dev/sda"]["value"] == "no SMART result"
    assert _disk()["Storage"]["value"] == "no block devices found"


def test_disk_missing_list_and_ro_info():
    res = hc._eval_disk(['lsblk|NAME="mmcblk0" MODEL="" SIZE="8G" TYPE="disk" TRAN="" ROTA="0"',
                         "ro|/media/rfs/ro /dev/mmcblk0p1 ext4 ro", "lostfound|/lost+found|0",
                         "fsstate|Filesystem state: clean", "dftmp|tmpfs tmpfs 4G 0 3.5G 1% /tmp"], True)
    labels = {i["label"]: i for i in res["items"]}
    assert labels["Read-only drives"]["value"] == "/media/rfs/ro" and labels["File system"]["state"] == "ok"
    assert labels["Space to unpack an update (/tmp)"]["state"] == "ok"
    assert res["score"]["facts"] == ["mmcblk0"]


# The collector prints "mmc|<path>|<life_time>"; older collectors glued the two values together.
@pytest.mark.parametrize("value", ["0x0A 0x09 ", "0x0A0x09"])
def test_emmc_wear_as_the_collector_prints_it(value):
    res = hc._eval_disk([f"mmc|/sys/class/mmc_host/mmc0/mmc0:0001/life_time|{value}"], True)
    item = res["items"][0]
    assert item["label"] == "Flash storage wear (mmc0:0001)" and item["state"] == "bad"
    assert item["value"] == f"{value.strip()} (0x0B = worn out)"
    assert res["score"]["score"] <= 30 and res["score"]["stats"][0] == {
        "label": "Flash life left", "value": "about 0%", "state": "bad", "hint": "Built-in eMMC flash (reported in 10% steps)."}


def test_emmc_wear_levels():
    res = hc._eval_disk(["mmc|/sys/block/mmcblk0/device/life_time|0x08 0x01",
                         "mmc|/sys/block/mmcblk1/device/life_time|0x01 0x01", "mmc|odd|n/a"], True)
    labels = {i["label"]: i["state"] for i in res["items"]}
    assert labels == {"Flash storage wear (mmcblk0)": "warn", "Flash storage wear (mmcblk1)": "ok",
                      "Flash storage wear (odd)": "ok"}
    assert any("wearing out" in r for r in res["score"]["reasons"])


def test_collector_keeps_emmc_values_apart():
    line = next(l for l in hc.COLLECTOR.splitlines() if 'echo "mmc|' in l)
    assert "tr -d ' '" not in line and "tr -s" in line


def test_life_left_and_tb_written():
    def raw_of(attrs):
        import re

        def raw(name):
            v = attrs.get(name)
            m = re.search(r"\d+", str(v).replace(",", "")) if v is not None else None
            return int(m.group()) if m else None
        return raw

    assert hc._life_left({}, raw_of({}), 7) == (93, "93% life left")
    attrs = {"SSD_Life_Left@v": "88", "A@v": "99"}
    assert hc._life_left(attrs, raw_of(attrs), None) == (88, "88% life left")
    frozen = {f"A{i}@v": "100" for i in range(5)}
    assert hc._life_left(frozen, raw_of(frozen), None) == (None, "life left (this disk doesn't report it)")
    frozen["MaxAvgErase_Ct"] = "50 (Average 300)"
    left, text = hc._life_left(frozen, raw_of(frozen), None)
    assert left == 90 and "estimated" in text
    assert hc._life_left({}, raw_of({}), None) == (None, "")
    lba = {"Total_LBAs_Written": "4294967296"}
    assert hc._tb_written(lba, raw_of(lba)) == "~2 TB written"
    gib = {"Lifetime_Writes_GiB": "500"}
    assert hc._tb_written(gib, raw_of(gib)) == "~500 GB written"
    assert hc._tb_written({}, raw_of({})) == ""


def test_net_variants():
    res = hc._eval_net(["iface|eth0|down|1000|0|1500|mac", "route|default via 1.1.1.1 dev eth0",
                        "ping|100% packet loss", "link_flaps_30d=40"])
    labels = {i["label"]: i for i in res["items"]}
    assert labels["Network port eth0"]["state"] == "bad" and "main network port" in labels["Network port eth0"]["value"]
    assert labels["Reaches the router (ping)"]["state"] == "bad"
    assert labels["Network disconnects (30 days)"]["state"] == "warn"
    res = hc._eval_net(["ip|1: eth0: <UP>", "ip|    RX: a b c d", "ip|    1 1000 2000 0", "iface|eth0|up|1000|1|1500|m",
                        "ping|no loss info"])
    labels = {i["label"]: i for i in res["items"]}
    assert "2000 damaged packets" in labels["Network port eth0"]["value"]
    assert labels["Route to the outside network"]["state"] == "warn"
    assert hc._eval_net([])["items"][-1]["label"] == "Route to the outside network"
    assert hc._eval_net(["route|default via x"]) ["items"] == [
        {"label": "Network", "value": "no interfaces found", "state": "info", "detail": "", "action": ""}]


def test_small_evaluators():
    ident = hc._eval_ident(["hostname=h", "reboots_30d=8", "journal_ok=1", "is_matrix=1", "sw_build=b"])
    labels = {i["label"]: i for i in ident["items"]}
    assert labels["Restarts (last 30 days)"]["state"] == "ok" and labels["Software"]["value"] == "? (b)"
    cpu = hc._eval_cpu(["cores=4", "pcie_err_30d=1", "thermal|t|80000"], {"load": "1 1 9"})
    labels = {i["label"]: i for i in cpu["items"]}
    assert labels["CPU busy (load 1/5/15 min)"]["state"] == "warn" and labels["Temperatures"]["state"] == "warn"
    assert labels["Internal bus (PCIe) errors (30 days)"]["state"] == "warn" and "raw" not in cpu
    assert len(hc._eval_cpu(["cores=4"], {"load": "a b c"})["items"]) == 1
    mem = hc._eval_mem(["edac|mc0|3|0", "free|Mem: 8000 1 1 1 1 200", "mem_err_30d=2"], ["hw_err_30d=1"], True)
    labels = {i["label"]: i for i in mem["items"]}
    assert labels["Memory errors (ECC counters)"]["state"] == "warn" and labels["Free memory"]["state"] == "warn"
    assert "Memory error messages (30 days)" not in labels
    mem = hc._eval_mem(["mem_err_30d=2", "edac|mc0|0|0"], [], True)
    assert {i["label"]: i["state"] for i in mem["items"]} == {"Memory errors (ECC counters)": "ok",
                                                             "Memory error messages (30 days)": "warn"}
    mem = hc._eval_mem(["mem_err_30d=2"], [], True)
    assert mem["items"][0]["label"] == "Memory error messages (30 days)"
    assert hc._eval_kerr(["kern_err_7d=3"])["items"][0]["state"] == "info"
    assert hc._eval_kerr([])["items"][0]["state"] == "ok"
    svc = hc._eval_svc(["swupdate_client=1", "swupdate_running=1", "listen|8080"], {"is_matrix": "1"}, 8080)
    assert all(i["state"] == "ok" for i in svc["items"])


def test_score_grades():
    s = hc._Score("x")
    assert s.result()["grade"] == "Good"
    s.hit(15, "a")
    assert s.result()["grade"] == "Fair" and s.result()["state"] == "warn"
    s.cap(50)
    assert s.result()["grade"] == "Poor" and s.result()["reasons"] == ["a"]
    s.hit(200, "b")
    assert s.result()["score"] == 0 and s.result()["grade"] == "Failing"


def test_glance_and_actions_helpers():
    secs = [{"items": [{"label": "Network port eth1", "value": "down", "state": "info"}]}]
    assert hc.build_glance(secs) == [{"label": "Network eth1", "value": "down", "state": "info"}]
    assert hc.build_glance([{"items": []}]) == []
    report = {}
    hc.finish_report(report, [{"title": "T", "state": "warn", "items": [
        {"label": "a", "value": "1", "state": "warn", "action": "do"},
        {"label": "b", "value": "2", "state": "bad", "action": "do"},
        {"label": "c", "value": "3", "state": "ok", "action": ""}]}])
    assert report["state"] == "warning" and report["issues"] == ["T: a - 1", "T: b - 2"]
    assert report["actions"] == [{"text": "do", "state": "bad", "because": ["b", "a"]}]
