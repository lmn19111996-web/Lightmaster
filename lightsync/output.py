"""Picks the WLED sender for the configured protocol. Both share send/black/close/send_errors."""
from __future__ import annotations

from .output_ddp import DDP_PORT, DDPSender
from .output_udp import UDP_PORT, WledUdpSender

PROTOCOLS = ("udp", "ddp")
DEFAULT_PORTS = {"udp": UDP_PORT, "ddp": DDP_PORT}


def make_sender(protocol: str, host: str, port: int | None = None, udp_timeout_s: int = 2):
    if protocol == "udp":
        return WledUdpSender(host, port or UDP_PORT, udp_timeout_s)
    if protocol == "ddp":
        return DDPSender(host, port or DDP_PORT)
    raise ValueError(f"unknown protocol {protocol!r}")
