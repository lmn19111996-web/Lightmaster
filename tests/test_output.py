import struct

import numpy as np
import pytest

from lightsync.output import make_sender
from lightsync.output_ddp import DDPSender
from lightsync.output_udp import WledUdpSender


def test_drgb_packet_for_56_pixels():
    tx = WledUdpSender("127.0.0.1", timeout_s=2)
    rgb = np.arange(56 * 3, dtype=np.uint8).reshape(56, 3)
    (pkt,) = tx.packets(rgb)
    assert pkt[:2] == bytes([2, 2]) and pkt[2:] == rgb.tobytes() and len(pkt) == 2 + 168
    tx.close()


def test_dnrgb_chunks_for_long_strips():
    tx = WledUdpSender("127.0.0.1")
    rgb = np.zeros((1000, 3), np.uint8)
    pkts = tx.packets(rgb)
    starts = [(p[2] << 8) | p[3] for p in pkts]
    assert [p[0] for p in pkts] == [4, 4, 4] and starts == [0, 489, 978]
    assert sum((len(p) - 4) // 3 for p in pkts) == 1000
    tx.close()


def test_udp_rejects_float_frames_and_clamps_timeout():
    tx = WledUdpSender("127.0.0.1", timeout_s=999)
    assert tx.timeout_s == 254
    with pytest.raises(TypeError):
        tx.send(np.zeros((4, 3)))
    tx.close()


def test_send_to_localhost_works():
    for tx in (make_sender("udp", "127.0.0.1", 50999), make_sender("ddp", "127.0.0.1", 50998)):
        assert tx.send(np.zeros((56, 3), np.uint8)) and tx.frames_sent == 1
        tx.close()


def test_ddp_header():
    class FakeSock:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append(data)

    tx = DDPSender("127.0.0.1")
    tx._sock.close()
    tx._sock = FakeSock()                                      # capture instead of sending
    tx.send(np.zeros((600, 3), np.uint8))
    flags = [p[0] for p in tx._sock.sent]
    offsets = [struct.unpack("!I", p[4:8])[0] for p in tx._sock.sent]
    assert flags == [0x40, 0x41] and offsets == [0, 1440]     # PUSH only on the last packet


def test_unknown_protocol():
    with pytest.raises(ValueError):
        make_sender("artnet", "127.0.0.1")
