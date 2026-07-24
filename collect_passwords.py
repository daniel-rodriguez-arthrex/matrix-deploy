#!/usr/bin/env python3
"""Collect per-room NMS passwords from act-mfg-eeprom display.

Usage:
    python collect_passwords.py

The script prompts for an SSH password and a sudo password, then connects to
every room defined in config/deploy_config.json and prints the value of
``barco_nms_password`` from ``sudo act-mfg-eeprom display``.

Leave the SSH password blank if the rooms accept key authentication.
"""

from __future__ import annotations

import getpass
import re

from matrix_deploy.config import AppConfig
from matrix_deploy.ssh_client import SSHError, SSHTarget, connect


def fetch_nms_password(client, sudo_password: str) -> str | None:
    """Run sudo act-mfg-eeprom display and return barco_nms_password."""
    stdin, stdout, stderr = client.exec_command(
        "sudo -S -p '' act-mfg-eeprom display"
    )
    stdin.write(sudo_password + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()

    output = stdout.read().decode(errors="replace").strip()
    stderr.read().decode(errors="replace")  # drain

    match = re.search(r"^barco_nms_password\s*=\s*(\S+)", output, re.MULTILINE)
    return match.group(1) if match else None


def main() -> None:
    config = AppConfig.load()
    conn = config.connection

    ssh_password = getpass.getpass("SSH password (Enter if key auth): ") or None
    sudo_password = getpass.getpass("Sudo password: ")

    print(f"\n{'Room':<10} {'Port':<6} {'Password':<30} {'Status'}")
    print("-" * 64)

    for room in config.rooms:
        port = room.ssh_port(conn.ssh_port_base)
        target = SSHTarget(
            host=conn.router_ip,
            port=port,
            username=conn.ssh_username,
            password=ssh_password,
        )
        try:
            client = connect(target)
            pw = fetch_nms_password(client, sudo_password)
            client.close()
            if pw:
                print(f"{room.name:<10} {port:<6} {pw:<30} ok")
            else:
                print(f"{room.name:<10} {port:<6} {'N/A':<30} no barco_nms_password")
        except SSHError as exc:
            print(f"{room.name:<10} {port:<6} {'N/A':<30} {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"{room.name:<10} {port:<6} {'N/A':<30} {exc}")


if __name__ == "__main__":
    main()
