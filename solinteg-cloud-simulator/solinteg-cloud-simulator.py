#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Rickard Dahlstedt
"""Local acknowledgement server for the Solinteg cloud telemetry protocol.

This program is intended to listen behind a firewall/NAT REDIRECT rule that
redirects the inverter communication module's connection to
8.211.16.247:5743 onto this host.  It validates each incoming ST frame and
returns the 58-byte application acknowledgement observed from the real
Solinteg server.  An optional isolated worker can mirror acknowledged frames
to the real endpoint through SOCKS5. Cloud input is always logged and is
ignored unless an explicit command-forwarding or fake-ACK test mode is enabled.

Observed request families: 01:03, 01:04, and 01:44.

The acknowledgement format was derived from a packet capture made on
2026-08-23.  Run this initially under observation and retain the original
proxy setup as an immediate fallback.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import queue
import select
import signal
import socket
import socketserver
import struct
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, NamedTuple, Optional

from solinteg_modbus_map import decode_register_range
from solinteg_cloud_forwarder import (
    CloudForwarder,
    DEFAULT_SHADOW_RETENTION_SECONDS,
    advance_device_timestamp,
    build_cloud_write_ack,
    patch_register_snapshot,
    parse_cloud_write,
    parse_endpoint,
)


MAGIC: Final = b"ST"
ACK_DECLARED_LENGTH: Final = 49
ACK_TOTAL_LENGTH: Final = 58
MAX_FRAME_LENGTH: Final = 1024 * 1024
DEFAULT_UNKNOWN_LOG_FILE: Final = Path(
    "/var/log/solinteg-cloud-simulator/unknown-frames.jsonl"
)
DEFAULT_CLOUD_INCOMING_LOG_FILE: Final = Path(
    "/var/log/solinteg-cloud-simulator/cloud-incoming.jsonl"
)
DEFAULT_CLOUD_TARGET: Final = "iot.solinteg-cloud.com:5743"
MESSAGE_TYPE_DESCRIPTIONS: Final = {
    b"\x01\x03": "device/configuration register snapshot",
    b"\x01\x04": "current full telemetry snapshot",
    b"\x01\x44": "buffered historical telemetry snapshot",
}
KNOWN_MESSAGE_TYPES: Final = frozenset(MESSAGE_TYPE_DESCRIPTIONS)
UNKNOWN_LOG_LOCK: Final = threading.Lock()
CLOUD_COMMAND_QUEUE_SIZE: Final = 64
MAX_CLOUD_COMMANDS_PER_CYCLE: Final = 8


class RegisterRange(NamedTuple):
    """One contiguous register range embedded in a cloud frame."""

    start: int
    end: int
    values: tuple[int, ...]


class InverterCommandRouter:
    """Non-blocking handoff from the cloud worker to the active LAN session."""

    def __init__(self, queue_size: int = CLOUD_COMMAND_QUEUE_SIZE) -> None:
        self.queue_size = queue_size
        self._lock = threading.Lock()
        self._active_token: Optional[object] = None
        self._active_queue: Optional[queue.Queue[bytes]] = None

    def register(self) -> tuple[object, queue.Queue[bytes]]:
        token = object()
        command_queue: queue.Queue[bytes] = queue.Queue(maxsize=self.queue_size)
        with self._lock:
            self._active_token = token
            self._active_queue = command_queue
        return token, command_queue

    def unregister(self, token: object) -> None:
        with self._lock:
            if token is self._active_token:
                self._active_token = None
                self._active_queue = None

    def deliver(self, frame: bytes) -> str:
        """Queue a cloud frame without performing socket or file operations."""

        with self._lock:
            command_queue = self._active_queue
        if command_queue is None:
            return "ignored_no_inverter_connection"
        try:
            command_queue.put_nowait(frame)
            return "queued_for_inverter"
        except queue.Full:
            pass

        try:
            command_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            command_queue.put_nowait(frame)
            return "queued_for_inverter_replaced_stale"
        except queue.Full:
            return "ignored_inverter_queue_full"


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


def parse_register_snapshot(
    frame: bytes,
) -> tuple[str, tuple[RegisterRange, ...], bytes]:
    """Parse the sparse Modbus-register snapshot inside a valid ST frame."""

    if len(frame) < 51:
        raise ValueError("frame is too short to contain a register snapshot")

    snapshot_time = format_device_timestamp(frame[42:48])
    range_count = frame[48]
    cursor = 49
    data_end = len(frame) - 2
    ranges: list[RegisterRange] = []

    for record_number in range(1, range_count + 1):
        if cursor + 4 > data_end:
            raise ValueError(f"range {record_number} header extends past frame data")
        start, end = struct.unpack_from(">HH", frame, cursor)
        cursor += 4
        if end < start:
            raise ValueError(
                f"range {record_number} has descending addresses {start}..{end}"
            )

        register_count = end - start + 1
        values_end = cursor + register_count * 2
        if values_end > data_end:
            raise ValueError(
                f"range {record_number} ({start}..{end}) extends past frame data"
            )
        values = struct.unpack_from(f">{register_count}H", frame, cursor)
        ranges.append(RegisterRange(start, end, values))
        cursor = values_end

    return snapshot_time, tuple(ranges), frame[cursor:data_end]


def log_register_snapshot(frame_number: int, message_type: bytes, frame: bytes) -> None:
    """Log every register word, decoded with the Modbus Broker v5.12 map."""

    type_text = message_type.hex(":")
    try:
        snapshot_time, ranges, padding = parse_register_snapshot(frame)
    except ValueError as error:
        logging.warning(
            "frame %d type=%s register snapshot could not be decoded: %s",
            frame_number,
            type_text,
            error,
        )
        return

    logging.info(
        "frame %d type=%s register snapshot: time=%s ranges=%d",
        frame_number,
        type_text,
        snapshot_time,
        len(ranges),
    )
    for range_number, register_range in enumerate(ranges, start=1):
        for decoded in decode_register_range(
            register_range.start,
            register_range.values,
        ):
            logging.info(
                "frame %d type=%s snapshot=%s range=%d/%d register=%d "
                "name=%r raw=%s value=%r",
                frame_number,
                type_text,
                snapshot_time,
                range_number,
                len(ranges),
                decoded.address,
                decoded.name,
                list(decoded.raw_words),
                decoded.value,
            )

    if padding and any(value != 0xFF for value in padding):
        logging.warning(
            "frame %d type=%s has %d trailing byte(s), including non-FF data: %s",
            frame_number,
            type_text,
            len(padding),
            padding.hex(),
        )


def append_unknown_frame(
    log_file: Path,
    peer: str,
    frame_number: int,
    message_type: bytes,
    frame: bytes,
) -> None:
    """Append one valid, previously unseen frame as self-contained JSON Lines."""

    record = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "direction": "inverter_to_cloud",
        "peer": peer,
        "connection_frame_number": frame_number,
        "message_type": message_type.hex(":"),
        "length": len(frame),
        "sha256": hashlib.sha256(frame).hexdigest(),
        "frame_base64": base64.b64encode(frame).decode("ascii"),
    }
    encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")

    try:
        with UNKNOWN_LOG_LOCK:
            log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(log_file, flags, 0o600)
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short write while saving unknown frame")
                    view = view[written:]
            finally:
                os.close(descriptor)
    except OSError as error:
        logging.error(
            "could not save unknown frame %d type=%s to %s: %s",
            frame_number,
            message_type.hex(":"),
            log_file,
            error,
        )
        return

    logging.warning(
        "unknown frame %d type=%s saved to %s (sha256=%s)",
        frame_number,
        message_type.hex(":"),
        log_file,
        record["sha256"],
    )


class SolintegRequestHandler(socketserver.BaseRequestHandler):
    server: "SolintegServer"

    def handle(self) -> None:
        connection: socket.socket = self.request
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        logging.info("connection opened from %s", peer)

        buffer = bytearray()
        frame_number = 0
        command_token: Optional[object] = None
        command_queue: Optional[queue.Queue[bytes]] = None
        if self.server.cloud_command_router is not None:
            command_token, command_queue = self.server.cloud_command_router.register()
            logging.warning(
                "cloud-command forwarding armed for active inverter connection %s",
                peer,
            )
        try:
            while True:
                if command_queue is not None:
                    self.send_cloud_commands(connection, peer, command_queue)
                    readable, _writable, _exceptional = select.select(
                        [connection], [], [connection], 0.1
                    )
                    if connection in _exceptional:
                        raise ConnectionError("inverter socket exception")
                    if connection not in readable:
                        continue

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
        except (ConnectionError, OSError, ValueError) as error:
            logging.warning("connection error from %s: %s", peer, error)
        finally:
            if (
                command_token is not None
                and self.server.cloud_command_router is not None
            ):
                self.server.cloud_command_router.unregister(command_token)
            logging.info("connection closed from %s", peer)

    @staticmethod
    def send_cloud_commands(
        connection: socket.socket,
        peer: str,
        command_queue: queue.Queue[bytes],
    ) -> None:
        """Deliver queued commands from the handler thread that owns the socket."""

        for _command_number in range(MAX_CLOUD_COMMANDS_PER_CYCLE):
            try:
                frame = command_queue.get_nowait()
            except queue.Empty:
                return
            connection.sendall(frame)
            logging.warning(
                "cloud frame delivered to inverter %s: type=%s length=%d",
                peer,
                frame[6:8].hex(":") if len(frame) >= 8 else "unknown",
                len(frame),
            )

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
            if self.server.strict_known_types:
                logging.warning(
                    "frame %d has previously unseen type %s; strict mode will not "
                    "acknowledge it",
                    frame_number,
                    type_text,
                )
                if self.server.unknown_log_file is not None:
                    append_unknown_frame(
                        self.server.unknown_log_file,
                        peer,
                        frame_number,
                        message_type,
                        frame,
                    )
                return

        # The acknowledgement is deliberately sent before cloud forwarding,
        # verbose decoding, or disk logging.  SOCKS, Internet, cloud and
        # diagnostic work must never delay the inverter.
        acknowledgement = build_acknowledgement(frame)
        connection.sendall(acknowledgement)

        if self.server.cloud_forwarder is not None:
            self.server.cloud_forwarder.enqueue(frame)

        logging.info(
            "frame %d acknowledged: type=%s (%s) serial=%s length=%d time=%s",
            frame_number,
            type_text,
            type_description,
            serial,
            len(frame),
            device_time,
        )

        if message_type not in KNOWN_MESSAGE_TYPES:
            logging.warning(
                "frame %d has previously unseen type %s; serial=%s length=%d time=%s",
                frame_number,
                type_text,
                serial,
                len(frame),
                device_time,
            )
            if self.server.unknown_log_file is not None:
                append_unknown_frame(
                    self.server.unknown_log_file,
                    peer,
                    frame_number,
                    message_type,
                    frame,
                )

        if self.server.verbose_registers:
            log_register_snapshot(frame_number, message_type, frame)


class SolintegServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        strict_known_types: bool,
        verbose_registers: bool,
        unknown_log_file: Optional[Path],
        cloud_forwarder: Optional[CloudForwarder],
        cloud_command_router: Optional[InverterCommandRouter],
    ) -> None:
        self.strict_known_types = strict_known_types
        self.verbose_registers = verbose_registers
        self.unknown_log_file = unknown_log_file
        self.cloud_forwarder = cloud_forwarder
        self.cloud_command_router = cloud_command_router
        super().__init__(server_address, SolintegRequestHandler)

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        super().server_bind()


def run_self_test() -> None:
    if crc16_modbus(b"123456789") != 0x4B37:
        raise AssertionError("CRC-16/Modbus test vector failed")
    if KNOWN_MESSAGE_TYPES != frozenset(MESSAGE_TYPE_DESCRIPTIONS):
        raise AssertionError("known message types and descriptions differ")

    decoded_soc = list(decode_register_range(33000, [7654]))
    if len(decoded_soc) != 1 or decoded_soc[0].value != "76.54 %":
        raise AssertionError("Modbus register scaling test failed")
    decoded_signed = list(decode_register_range(53522, [0xFFF6]))
    if len(decoded_signed) != 1 or decoded_signed[0].value != "-1.0 A":
        raise AssertionError("signed Modbus register test failed")
    decoded_unknown = list(decode_register_range(12345, [0xABCD]))
    if len(decoded_unknown) != 1 or decoded_unknown[0].name != "Raw Field":
        raise AssertionError("unknown Modbus register test failed")

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

    cloud_write_body = b"".join(
        (
            MAGIC,
            ACK_DECLARED_LENGTH.to_bytes(4, "big"),
            b"\x01\x10",
            serial,
            timestamp,
            b"\x00" * 10,
            struct.pack(">HHH", 50009, 50009, 137),
            b"\xff" * 10,
        )
    )
    cloud_write_frame = cloud_write_body + struct.pack(
        "<H", crc16_modbus(cloud_write_body)
    )
    cloud_write = parse_cloud_write(cloud_write_frame)
    if cloud_write.start != 50009 or cloud_write.end != 50009:
        raise AssertionError("cloud write register-range test failed")
    decoded_limit = list(decode_register_range(cloud_write.start, cloud_write.values))
    if len(decoded_limit) != 1 or decoded_limit[0].value != "13.7 kW":
        raise AssertionError("cloud write register-value test failed")
    peak_shaving_cases = (
        (50016, 100, "10.0 kW"),
        (50017, 800, "80.0 %"),
        (50018, 50, "5.0 kW"),
        (50022, 1, "1 (On)"),
    )
    for address, raw_value, expected_value in peak_shaving_cases:
        decoded = list(decode_register_range(address, [raw_value]))
        if len(decoded) != 1 or decoded[0].value != expected_value:
            raise AssertionError(
                f"peak-shaving register {address} translation test failed"
            )
    fake_ack_timestamp = bytes((26, 8, 23, 22, 21, 57))
    fake_cloud_ack = build_cloud_write_ack(cloud_write_frame, fake_ack_timestamp)
    if fake_cloud_ack[24:30] != fake_ack_timestamp:
        raise AssertionError("fake cloud ACK timestamp test failed")
    if fake_cloud_ack[40:44] != cloud_write_frame[40:44]:
        raise AssertionError("fake cloud ACK register-range test failed")
    if fake_cloud_ack[44:56] != b"\x01" + b"\xff" * 11:
        raise AssertionError("fake cloud ACK status/padding test failed")
    if crc16_modbus(fake_cloud_ack[:-2]) != int.from_bytes(
        fake_cloud_ack[-2:], "little"
    ):
        raise AssertionError("fake cloud ACK CRC test failed")
    advanced_timestamp = advance_device_timestamp(
        bytes((26, 8, 23, 23, 59, 58)),
        5.9,
    )
    if advanced_timestamp != bytes((26, 8, 24, 0, 0, 3)):
        raise AssertionError("advancing device timestamp test failed")

    snapshot_body = b"".join(
        (
            MAGIC,
            b"\x00\x00\x00\x00",
            b"\x01\x03\x00\x00",
            serial,
            timestamp,
            b"\x00" * 10,
            timestamp,
            b"\x01",
            struct.pack(">HHH", 50007, 50007, 1),
        )
    )
    snapshot_total = len(snapshot_body) + 2
    snapshot_body = (
        snapshot_body[:2]
        + (snapshot_total - 9).to_bytes(4, "big")
        + snapshot_body[6:]
    )
    snapshot = snapshot_body + struct.pack("<H", crc16_modbus(snapshot_body))
    patched_snapshot, patched_addresses = patch_register_snapshot(
        snapshot,
        {50007: 0},
        advanced_timestamp,
    )
    if patched_addresses != (50007,):
        raise AssertionError("cloud shadow register-address test failed")
    if patched_snapshot[26:32] != advanced_timestamp:
        raise AssertionError("cloud shadow outer timestamp test failed")
    if patched_snapshot[42:48] != advanced_timestamp:
        raise AssertionError("cloud shadow snapshot timestamp test failed")
    if patched_snapshot[53:55] != b"\x00\x00":
        raise AssertionError("cloud shadow register-value test failed")
    if crc16_modbus(patched_snapshot[:-2]) != int.from_bytes(
        patched_snapshot[-2:], "little"
    ):
        raise AssertionError("cloud shadow CRC test failed")
    print("Self-test passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Locally acknowledge Solinteg cloud telemetry frames.",
        epilog=(
            "Cloud modes: omit --forward-socks5 for local-only fake ACKs; "
            "use --forward-socks5 to upload telemetry while blocking cloud "
            "commands; add --fake-ack-cloud-commands to report blocked writes "
            "successful; or add --allow-cloud-commands to relay real writes."
        ),
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
        help=(
            "decode and log inverter telemetry and incoming cloud writes using "
            "the Broker v5.12 table"
        ),
    )
    parser.add_argument(
        "--log-unknown",
        nargs="?",
        const=DEFAULT_UNKNOWN_LOG_FILE,
        type=Path,
        metavar="PATH",
        help=(
            "save valid frames of previously unseen types as JSON Lines; "
            f"default PATH: {DEFAULT_UNKNOWN_LOG_FILE}"
        ),
    )
    parser.add_argument(
        "--forward-socks5",
        type=str,
        metavar="HOST:PORT",
        help=(
            "mirror acknowledged inverter frames to the cloud through this "
            "SOCKS5 proxy; cloud input is always logged"
        ),
    )
    parser.add_argument(
        "--allow-cloud-commands",
        "--allow-full-communication",
        dest="allow_cloud_commands",
        action="store_true",
        help=(
            "DANGEROUS: forward cloud-initiated non-ACK frames to the active "
            "inverter connection; telemetry ACKs remain local"
        ),
    )
    parser.add_argument(
        "--fake-ack-cloud-commands",
        action="store_true",
        help=(
            "block 01:10 cloud writes but reproduce the genuine ACK and "
            "temporary 01:03 confirmation sequence"
        ),
    )
    parser.add_argument(
        "--cloud-shadow-retention",
        type=float,
        default=DEFAULT_SHADOW_RETENTION_SECONDS,
        metavar="SECONDS",
        help=(
            "retain and accumulate fake cloud-write values in outgoing 01:03 "
            "snapshots after the most recent write "
            f"(default: {DEFAULT_SHADOW_RETENTION_SECONDS:g})"
        ),
    )
    parser.add_argument(
        "--forward-target",
        default=DEFAULT_CLOUD_TARGET,
        metavar="HOST:PORT",
        help=(
            "remote cloud target sent to the SOCKS5 proxy "
            f"(default: {DEFAULT_CLOUD_TARGET})"
        ),
    )
    parser.add_argument(
        "--cloud-incoming-log",
        type=Path,
        default=DEFAULT_CLOUD_INCOMING_LOG_FILE,
        metavar="PATH",
        help=(
            "JSON Lines file for all cloud input "
            f"(default: {DEFAULT_CLOUD_INCOMING_LOG_FILE})"
        ),
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run protocol and CRC self-tests, then exit",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.forward_socks5 is not None:
        try:
            args.forward_socks5 = parse_endpoint(args.forward_socks5)
        except ValueError as error:
            parser.error(f"invalid --forward-socks5 value: {error}")
    if args.allow_cloud_commands and args.forward_socks5 is None:
        parser.error("--allow-cloud-commands requires --forward-socks5")
    if args.fake_ack_cloud_commands and args.forward_socks5 is None:
        parser.error("--fake-ack-cloud-commands requires --forward-socks5")
    if args.fake_ack_cloud_commands and args.allow_cloud_commands:
        parser.error(
            "--fake-ack-cloud-commands cannot be combined with "
            "--allow-cloud-commands"
        )
    if not 1.0 <= args.cloud_shadow_retention <= 3600.0:
        parser.error("--cloud-shadow-retention must be between 1 and 3600 seconds")
    if args.allow_cloud_commands and args.strict_known_types:
        parser.error(
            "--allow-cloud-commands cannot be combined with --strict-known-types"
        )
    try:
        args.forward_target = parse_endpoint(args.forward_target)
    except ValueError as error:
        parser.error(f"invalid --forward-target value: {error}")
    return args


def main() -> int:
    args = parse_args()
    if args.self_test:
        run_self_test()
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cloud_forwarder: Optional[CloudForwarder] = None
    cloud_command_router: Optional[InverterCommandRouter] = None
    if args.allow_cloud_commands:
        cloud_command_router = InverterCommandRouter()
    if args.forward_socks5 is not None:
        username = os.environ.get("SOLINTEG_SOCKS5_USERNAME")
        password = os.environ.get("SOLINTEG_SOCKS5_PASSWORD")
        if password and not username:
            logging.error(
                "SOLINTEG_SOCKS5_PASSWORD is set without "
                "SOLINTEG_SOCKS5_USERNAME"
            )
            return 2
        cloud_forwarder = CloudForwarder(
            proxy=args.forward_socks5,
            target=args.forward_target,
            incoming_log_file=args.cloud_incoming_log,
            username=username if username else None,
            password=password,
            verbose_registers=args.verbose,
            incoming_handler=(
                cloud_command_router.deliver
                if cloud_command_router is not None
                else None
            ),
            fake_ack_cloud_writes=args.fake_ack_cloud_commands,
            shadow_retention_seconds=args.cloud_shadow_retention,
        )

    try:
        server = SolintegServer(
            (args.bind, args.port),
            args.strict_known_types,
            args.verbose,
            args.log_unknown,
            cloud_forwarder,
            cloud_command_router,
        )
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

    if cloud_forwarder is not None:
        cloud_forwarder.start()
        if args.allow_cloud_commands:
            logging.warning(
                "FULL CLOUD COMMAND COMMUNICATION ENABLED: proxy=%s target=%s "
                "incoming=%s; non-ACK cloud frames can control the inverter",
                args.forward_socks5,
                args.forward_target,
                args.cloud_incoming_log,
            )
        elif args.fake_ack_cloud_commands:
            logging.warning(
                "FAKE CLOUD COMMAND ACKNOWLEDGEMENTS ENABLED: proxy=%s target=%s "
                "incoming=%s; 01:10 writes are blocked but reported with "
                "temporary cloud-only configuration confirmation; shadow "
                "retention=%.1fs",
                args.forward_socks5,
                args.forward_target,
                args.cloud_incoming_log,
                args.cloud_shadow_retention,
            )
        else:
            logging.info(
                "SOCKS5 cloud mirror enabled: proxy=%s target=%s incoming=%s; "
                "all cloud input will be logged and ignored",
                args.forward_socks5,
                args.forward_target,
                args.cloud_incoming_log,
            )
    else:
        logging.info("SOCKS5 cloud mirror disabled")

    logging.info(
        "Solinteg simulator listening on %s:%d; strict=%s verbose=%s log_unknown=%s",
        args.bind,
        args.port,
        args.strict_known_types,
        args.verbose,
        args.log_unknown if args.log_unknown is not None else "off",
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        if cloud_forwarder is not None:
            cloud_forwarder.stop()
    logging.info("Solinteg simulator stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
