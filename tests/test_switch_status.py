import pytest

from matrix_deploy import switch_status as ss
from matrix_deploy.ssh_client import SSHError

from fakes import FakeSSHClient

PORTS = """\
Port     Virtual    Port  Link  Auto   Speed      Duplex
         router     State State Neg  Cfg Actual Cfg Actual
1        VR-Default E     A     ON   AUTO 10000 AUTO FULL
2        VR-Default E     R     ON   AUTO
3        VR-Default D     D     OFF  1000
5        VR-Default E     A     ON   AUTO 1000  AUTO FULL
49       VR-Default E     A     OFF  40000 40000 FULL FULL
"""

IPARP = """\
VR          Destination      Mac                Age  Static  VLAN          VID   Port
VR-Default  10.1.1.13        00:04:A5:11:22:33  12   NO      Room1         101   1
VR-Default  10.1.1.50        00:05:a6:00:00:01  3    NO      Room1         101   5
VR-Default  10.9.9.9         00:00:00:00:00:09  3    NO      Room9         109   77
"""

FDB = """\
00:04:a5:11:22:33  Room1(0101) 0000  d m       1
00:04:a5:aa:bb:cc  Room1(0101) 0000  d m       1
de:ad:be:ef:00:01  Room1(0101) 0000  d m       44
"""

LLDP = """\
49   00:04:96:01:02:03   1:49   120   0   QALAB-Switch2
50   00:04:96:01:02:04   1:50   120   0   Elsewhere
"""

XCVR = """\
header line
Port :  1
    Media Type          : SFP+ (Unsupported)
    Vendor Name         : FINISAR
    Part Number         : FTLX8571
    Serial Number       : ABC123
    Wavelength          : 850
    Temp (Celsius)      : 35.00        Status : Normal
        Low Warn Threshold  : -5.00    High Warn Threshold  : 75.00
        Low Alarm Threshold : -10.00   High Alarm Threshold : 80.00
    Rx Power (dBm)      : -3.10        Status : Normal
        Low Warn Threshold  : -10.0    High Warn Threshold  : 0.5
    Rx Power (dBm)      : -25.00       Status : Low Alarm
        Low Warn Threshold  : -11.0    High Warn Threshold  : 0.5
    Rx Power (dBm)      : -4.00        Status : Low Warn
        Low Warn Threshold  : -99      High Warn Threshold  : 99
    Tx Power (dBm)      : N/A          Status : Normal
Port :  2
    Error: Transceiver not present.
Port :  3
    Error: DDMI is not supported on this DAC transceiver.
Port :  5
    Error: something else
"""

RXERR = """\
Port      Link  Rx      Rx      Rx      Rx      Rx      Rx      Rx
          State Crc     Over    Under   Frag    Jabber  Align   Lost
1         A     4       0       0       0       0       0       1
2         R     0       0       0       0       0       0       0
"""

TXERR = """\
1         A     0       0       0       0       0       2
5         A     0       0       0       0       0       0
"""


def test_parse_ports():
    p = ss.parse_ports(PORTS)
    assert p[1] == {"enabled": True, "link": True, "speed": "10G"}
    assert p[2] == {"enabled": True, "link": False, "speed": None}
    assert p[3] == {"enabled": False, "link": False, "speed": None}
    assert p[5]["speed"] == "1000" and p[49]["speed"] == "40G"


def test_parse_tables():
    arp = ss.parse_iparp(IPARP)
    assert arp[0] == {"ip": "10.1.1.13", "mac": "00:04:a5:11:22:33", "vlan": "Room1", "port": 1}
    fdb = ss.parse_fdb(FDB)
    assert fdb[1] == {"mac": "00:04:a5:aa:bb:cc", "vlan": "Room1", "port": 1}
    assert ss.parse_lldp(LLDP) == {49: "QALAB-Switch2", 50: "Elsewhere"}
    assert ss.vendor("00:04:A5:00:00:00") == "Barco" and ss.vendor("ff:ff:ff:00:00:00") == ""


def test_parse_transceivers():
    t = ss.parse_transceivers(XCVR)
    p1 = t[1]
    assert p1["present"] and p1["ddmi"] and not p1["supported"]
    assert (p1["media"], p1["vendor"], p1["serial"], p1["wavelength"]) == ("SFP+", "FINISAR", "ABC123", "850")
    assert p1["ddm"]["temp"] == {"value": 35.0, "status": "Normal", "low_warn": -5.0, "high_warn": 75.0,
                                 "low_alarm": -10.0, "high_alarm": 80.0}
    # Worst lane kept, with its own thresholds.
    assert p1["ddm"]["rx_power"] == {"value": -25.0, "status": "Low Alarm", "low_warn": -11.0, "high_warn": 0.5}
    assert p1["ddm"]["tx_power"]["value"] is None
    assert t[2] == {"present": False, "ddmi": False}
    assert t[3]["media"] == "DAC" and t[5]["media"] is None


def test_parse_error_counters():
    rx = ss.parse_error_counters(RXERR, ss._RX_ERRORS)
    assert rx[1]["crc"] == 4 and rx[1]["lost"] == 1 and rx[2]["crc"] == 0
    tx = ss.parse_error_counters(TXERR, ss._TX_ERRORS)
    assert tx[1]["parity"] == 2 and 5 in tx


def _outputs(**over):
    out = {ss.PORTS_COMMAND: PORTS, "show iparp": IPARP, "show fdb": FDB, "show lldp neighbors": LLDP,
           "show ports transceiver information detail": XCVR, "show ports rxerrors no-refresh": RXERR,
           "show ports txerrors no-refresh": TXERR}
    out.update(over)
    return out


def test_read_switch(monkeypatch):
    client = FakeSSHClient()
    seen = {}

    def fake_connect(target, **kw):
        seen.update(target=target, **kw)
        return client

    def fake_show(c, commands, setup=()):
        seen["commands"], seen["setup"] = commands, setup
        return _outputs()

    monkeypatch.setattr(ss, "connect", fake_connect)
    monkeypatch.setattr(ss, "run_cli_show", fake_show)
    data = ss.read_switch("172.16.0.5", 21022, "admin", "pw", fiber=True)
    assert seen["try_keys"] is False and seen["sha1"] == "allow" and seen["target"].port == 21022
    assert seen["setup"] == ("disable clipaging",) and len(seen["commands"]) == 7
    assert client.closed
    p1 = data["ports"]["1"]
    assert [d["mac"] for d in p1["devices"]] == ["00:04:a5:11:22:33", "00:04:a5:aa:bb:cc"]
    assert p1["devices"][0] == {"ip": "10.1.1.13", "mac": "00:04:a5:11:22:33", "vendor": "Barco"}
    assert p1["devices"][1]["ip"] is None
    assert data["ports"]["49"]["neighbor"] == "QALAB-Switch2"
    assert p1["optic"]["vendor"] == "FINISAR" and p1["errors"]["rx"]["crc"] == 4
    assert data["ports"]["5"]["errors"] == {"rx": {}, "tx": {"coll": 0, "late_coll": 0, "deferred": 0, "errors": 0,
                                                               "lost": 0, "parity": 0}}
    assert "optic" not in data["ports"]["49"]
    assert list(data["ports"]) == ["1", "2", "3", "5", "49"]


def test_read_switch_errors(monkeypatch):
    monkeypatch.setattr(ss, "connect", lambda t, **k: FakeSSHClient())
    monkeypatch.setattr(ss, "run_cli_show", lambda c, cmds, setup=(): _outputs(**{ss.PORTS_COMMAND: "garbage"}))
    with pytest.raises(SSHError, match="port list"):
        ss.read_switch("h", 1, "u", "p")
    monkeypatch.setattr(ss, "run_cli_show", lambda c, cmds, setup=(): _outputs(
        **{"show ports transceiver information detail": ""}))
    with pytest.raises(SSHError, match="transceiver"):
        ss.read_switch("h", 1, "u", "p", fiber=True)
    data = ss.read_switch("h", 1, "u", "p")
    assert "optic" not in data["ports"]["1"]
