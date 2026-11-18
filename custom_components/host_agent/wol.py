"""Wake-on-LAN magic packet, sent from the Home Assistant host."""

from __future__ import annotations

import re
import socket

from .const import DEFAULT_WOL_BROADCAST, WOL_PORT


def magic_packet(mac: str) -> bytes:
    """6 bytes of 0xFF followed by the MAC address repeated 16 times."""
    digits = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(digits) != 12:
        raise ValueError(f"invalid MAC address '{mac}'")
    return bytes.fromhex("ff" * 6 + digits * 16)


def send_magic_packet(mac: str, broadcast: str = DEFAULT_WOL_BROADCAST, port: int = WOL_PORT) -> None:
    """Blocking: call from an executor. Broadcasts do not cross routed links (VPN...)."""
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))
