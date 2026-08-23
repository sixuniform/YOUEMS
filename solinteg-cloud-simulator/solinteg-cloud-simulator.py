#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Rickard Dahlstedt
"""Local acknowledgement server for the Solinteg cloud telemetry protocol.

This program is intended to listen behind a firewall/NAT REDIRECT rule that
redirects the inverter communication module's connection to
8.211.16.247:5743 onto this host.  It does not contact Solinteg or forward any
telemetry.  It validates each incoming ST frame and returns the 58-byte
application acknowledgement observed from the real Solinteg server.

Observed request families: 01:03, 01:04, and 01:44.

The acknowledgement format was derived from a packet capture made on
2026-08-23.  Run this initially under observation and retain the original
proxy setup as an immediate fallback.
"""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import socketserver
import struct
import sys
import threading
from typing import Final


MAGIC: Final = b"ST"
ACK_DECLARED_LENGTH: Final = 49
ACK_TOTAL_LENGTH: Final = 58
MAX_FRAME_LENGTH: Final = 1024 * 1024
MESSAGE_TYPE_DESCRIPTIONS: Final = {
    b"\x01\x03": "device/configuration register snapshot",
    b"\x01\x04": "current full telemetry snapshot",
    b"\x01\x44": "buffered historical telemetry snapshot",
}
KNOWN_MESSAGE_TYPES: Final = frozenset(MESSAGE_TYPE_DESCRIPTIONS)


def make_crc16_table() -> tuple[int, ...]:
    table: list[int] = []
    for value in range(256):
        crc = value
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
        table.append(crc)
    return tuple(table)


CRC16_TABLE: Final = make_crc16_table()


def crc16_modbus(data: bytes) -> int:
    """Return CRC-16/Modbus as an integer (wire order is little-endian)."""
    crc = 0xFFFF
    for value in data:
        crc = (crc >> 8) ^ CRC16_TABLE[(crc ^ value) & 0xFF]
    return crc & 0xFFFF


def frame_total_length(header: bytes) -> int:
    """Return total ST frame length from its first six bytes."""
    if len(header) < 6 or header[:2] != MAGIC:
        raise ValueError("not an ST frame header")
    # In the captured protocol, declared length is total length minus 9.
    return int.from_bytes(header[2:6], "big") + 9


def validate_request(frame: bytes) -> tuple[bool, str]:
    if len(frame) < 32:
        return False, f"frame too short ({len(frame)} bytes)"
    if frame[:2] != MAGIC:
        return False, "invalid magic"
    declared_total = frame_total_length(frame[:6])
    if declared_total != len(frame):
        return False, (
            f"length mismatch: header says {declared_total}, received {len(frame)}"
        )
    expected = int.from_bytes(frame[-2:], "little")
    calculated = crc16_modbus(frame[:-2])
    if calculated != expected:
        return False, (
            f"CRC mismatch: received 0x{expected:04x}, calculated 0x{calculated:04x}"
        )
    return True, "ok"


def build_acknowledgement(request: bytes) -> bytes:
    """Build the exact acknowledgement template observed from Solinteg."""
    valid, reason = validate_request(request)
    if not valid:
        raise ValueError(reason)

    body = b"".join(
        (
            MAGIC,
            ACK_DECLARED_LENGTH.to_bytes(4, "big"),
            request[6:8],       # Message family, e.g. 01:03
            request[10:26],     # Communication-module serial number
            request[26:32],     # Timestamp copied from the request
            b"\x00" * 10,
            b"\x01",
            b"\xff" * 15,
        )
    )
    acknowledgement = body + struct.pack("<H", crc16_modbus(body))
    if len(acknowledgement) != ACK_TOTAL_LENGTH:
        raise AssertionError("internal acknowledgement length error")
    return acknowledgement


def printable_serial(raw: bytes) -> str:
    text = raw.decode("ascii", errors="replace")
    return "".join(character if character.isprintable() else "?" for character in text)


def format_device_timestamp(raw: bytes) -> str:
    if len(raw) != 6:
        return raw.hex()
    year, month, day, hour, minute, second = raw
    if not (
        0 <= year <= 99
        and 1 <= month <= 12
        and 1 <= day <= 31
        and 0 <= hour <= 23
        and 0 <= minute <= 59
        and 0 <= second <= 59
    ):
        return raw.hex()
    return f"20{year:02d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"


class SolintegRequestHandler(socketserver.BaseRequestHandler):
    server: "SolintegServer"

    def handle(self) -> None:
        connection: socket.socket = self.request
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        logging.info("connection opened from %s", peer)

        buffer = bytearray()
        frame_number = 0
        try:
            while True:
                # The module may legitimately keep this connection idle for five
                # minutes or longer.  Leave session lifetime entirely to the peer;
                # a local idle timeout would recreate the failure we are avoiding.
                chunk = connection.recv(65536)
                if not chunk:
                    return
                buffer.extend(chunk)

                while True:
                    if len(buffer) < 2:
                        break

                    magic_offset = buffer.find(MAGIC)
                    if magic_offset < 0:
                        discarded = len(buffer) - (1 if buffer[-1:] == b"S" else 0)
                        if discarded:
                            logging.warning(
                                "discarded %d unframed byte(s) from %s", discarded, peer
                            )
                            del buffer[:discarded]
                        break
                    if magic_offset:
                        logging.warning(
                            "discarded %d byte(s) before ST magic from %s",
                            magic_offset,
                            peer,
                        )
                        del buffer[:magic_offset]

                    if len(buffer) < 6:
                        break

                    total_length = frame_total_length(buffer[:6])
                    if total_length < 32 or total_length > MAX_FRAME_LENGTH:
                        logging.error(
                            "invalid declared frame length %d from %s; resynchronising",
                            total_length,
                            peer,
                        )
                        del buffer[:2]
                        continue
                    if len(buffer) < total_length:
                        break

                    frame = bytes(buffer[:total_length])
                    del buffer[:total_length]
                    frame_number += 1
                    self.process_frame(connection, peer, frame_number, frame)
        except (ConnectionError, OSError) as error:
            logging.warning("connection error from %s: %s", peer, error)
        finally:
            logging.info("connection closed from %s", peer)

    def process_frame(
        self,
        connection: socket.socket,
        peer: str,
        frame_number: int,
        frame: bytes,
    ) -> None:
        valid, reason = validate_request(frame)
        if not valid:
            logging.error(
                "frame %d from %s rejected without acknowledgement: %s",
                frame_number,
                peer,
                reason,
            )
            return

        message_type = frame[6:8]
        type_text = message_type.hex(":")
        type_description = MESSAGE_TYPE_DESCRIPTIONS.get(
            message_type, "previously unseen message type"
        )
        serial = printable_serial(frame[10:26])
        device_time = format_device_timestamp(frame[26:32])

        if message_type not in KNOWN_MESSAGE_TYPES:
            logging.warning(
                "frame %d has previously unseen type %s; serial=%s length=%d time=%s",
                frame_number,
                type_text,
                serial,
                len(frame),
                device_time,
            )
            if self.server.strict_known_types:
                logging.warning(
                    "strict mode enabled: frame %d will not be acknowledged", frame_number
                )
                return

        acknowledgement = build_acknowledgement(frame)
        connection.sendall(acknowledgement)
        logging.info(
            "frame %d acknowledged: type=%s (%s) serial=%s length=%d time=%s",
            frame_number,
            type_text,
            type_description,
            serial,
            len(frame),
            device_time,
        )


class SolintegServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        strict_known_types: bool,
    ) -> None:
        self.strict_known_types = strict_known_types
        super().__init__(server_address, SolintegRequestHandler)

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        super().server_bind()


def run_self_test() -> None:
    if crc16_modbus(b"123456789") != 0x4B37:
        raise AssertionError("CRC-16/Modbus test vector failed")
    if KNOWN_MESSAGE_TYPES != frozenset(MESSAGE_TYPE_DESCRIPTIONS):
        raise AssertionError("known message types and descriptions differ")

    serial = b"TESTSIM000000001"  # Exactly 16 bytes.
    timestamp = bytes((26, 8, 23, 22, 20, 56))
    request_body = b"".join(
        (
            MAGIC,
            (39).to_bytes(4, "big"),  # Total request length: 48 bytes.
            b"\x01\x03\x00\x00",
            serial,
            timestamp,
            b"\x00" * 14,
        )
    )
    request = request_body + struct.pack("<H", crc16_modbus(request_body))
    if len(request) != 48:
        raise AssertionError(f"synthetic request is {len(request)} bytes, expected 48")
    valid, reason = validate_request(request)
    if not valid:
        raise AssertionError(f"synthetic request validation failed: {reason}")

    acknowledgement = build_acknowledgement(request)
    if acknowledgement[:6] != b"ST\x00\x00\x00\x31":
        raise AssertionError("acknowledgement header mismatch")
    if acknowledgement[6:8] != b"\x01\x03":
        raise AssertionError("acknowledgement type mismatch")
    if acknowledgement[8:24] != serial:
        raise AssertionError("acknowledgement serial mismatch")
    if acknowledgement[24:30] != timestamp:
        raise AssertionError("acknowledgement timestamp mismatch")
    if crc16_modbus(acknowledgement[:-2]) != int.from_bytes(
        acknowledgement[-2:], "little"
    ):
        raise AssertionError("acknowledgement CRC mismatch")
    print("Self-test passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Locally acknowledge Solinteg cloud telemetry frames."
    )
    parser.add_argument(
        "--bind",
        default="192.168.10.50",
        help="IPv4 address to listen on (default: 192.168.10.50)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5743,
        help="TCP port to listen on (default: 5743)",
    )
    parser.add_argument(
        "--strict-known-types",
        action="store_true",
        help="Do not acknowledge valid message types other than 01:03, 01:04, and 01:44",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run protocol and CRC self-tests, then exit",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main() -> int:
    args = parse_args()
    if args.self_test:
        run_self_test()
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        server = SolintegServer((args.bind, args.port), args.strict_known_types)
    except OSError as error:
        logging.error("cannot listen on %s:%d: %s", args.bind, args.port, error)
        return 1

    stop_once = threading.Event()

    def request_shutdown(signum: int, _frame: object) -> None:
        if stop_once.is_set():
            return
        stop_once.set()
        logging.info("received signal %d; shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    logging.info(
        "Solinteg simulator listening on %s:%d; strict=%s",
        args.bind,
        args.port,
        args.strict_known_types,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    logging.info("Solinteg simulator stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
