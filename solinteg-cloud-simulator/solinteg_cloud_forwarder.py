#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Rickard Dahlstedt
"""Asynchronous SOCKS5 cloud mirror for solinteg-cloud-simulator.

All SOCKS, cloud, and file operations in this module run in a dedicated daemon
thread. The cloud thread can reach the LAN side only through a bounded,
non-blocking callback; the inverter-facing handler retains ownership of its
socket and sends the local acknowledgement before queuing outgoing telemetry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import select
import socket
import struct
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Final, NamedTuple, Optional

from solinteg_modbus_map import decode_register_range


MAGIC: Final = b"ST"
MAX_FRAME_LENGTH: Final = 1024 * 1024
DEFAULT_QUEUE_SIZE: Final = 256
CONNECT_TIMEOUT_SECONDS: Final = 10.0
SELECT_TIMEOUT_SECONDS: Final = 0.25
MAX_RECONNECT_DELAY_SECONDS: Final = 30.0
GENERATED_REPLY_QUEUE_SIZE: Final = 64
SHADOW_CONFIRMATION_DELAY_SECONDS: Final = 2.0
DEFAULT_SHADOW_RETENTION_SECONDS: Final = 60.0
CLOUD_TELEMETRY_ACK_TYPES: Final = frozenset(
    (b"\x01\x03", b"\x01\x04", b"\x01\x44")
)
CLOUD_WRITE_TYPE: Final = b"\x01\x10"


class Endpoint(NamedTuple):
    host: str
    port: int

    def __str__(self) -> str:
        if ":" in self.host:
            return f"[{self.host}]:{self.port}"
        return f"{self.host}:{self.port}"


class CloudWrite(NamedTuple):
    """One register range carried by a cloud 01:10 command."""

    start: int
    end: int
    values: tuple[int, ...]
    padding: bytes


class GeneratedCloudFrame(NamedTuple):
    """One simulator-generated frame waiting for the cloud socket."""

    frame: bytes
    purpose: str


def parse_cloud_write(frame: bytes) -> CloudWrite:
    """Decode the Modbus-style register range in a cloud 01:10 frame."""

    if len(frame) < 46 or frame[:2] != MAGIC or frame[6:8] != CLOUD_WRITE_TYPE:
        raise ValueError("not a complete cloud 01:10 write frame")
    start, end = struct.unpack_from(">HH", frame, 40)
    if end < start:
        raise ValueError(f"descending register range {start}..{end}")
    register_count = end - start + 1
    values_end = 44 + register_count * 2
    data_end = len(frame) - 2
    if values_end > data_end:
        raise ValueError(
            f"register range {start}..{end} extends past command data"
        )
    values = struct.unpack_from(f">{register_count}H", frame, 44)
    return CloudWrite(start, end, values, frame[values_end:data_end])


def parse_endpoint(value: str) -> Endpoint:
    """Parse HOST:PORT or [IPv6]:PORT without resolving the hostname."""

    value = value.strip()
    if not value:
        raise ValueError("endpoint is empty")

    if value.startswith("["):
        closing = value.find("]")
        if closing < 0 or closing + 1 >= len(value) or value[closing + 1] != ":":
            raise ValueError("IPv6 endpoints must use [address]:port")
        host = value[1:closing]
        port_text = value[closing + 2 :]
    else:
        if value.count(":") != 1:
            raise ValueError("endpoint must use host:port")
        host, port_text = value.rsplit(":", 1)

    if not host:
        raise ValueError("endpoint host is empty")
    try:
        port = int(port_text, 10)
    except ValueError as error:
        raise ValueError("endpoint port is not an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("endpoint port must be between 1 and 65535")
    return Endpoint(host, port)


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise ConnectionError("SOCKS5 proxy closed the connection")
        result.extend(chunk)
    return bytes(result)


def _read_socks5_address(connection: socket.socket, address_type: int) -> None:
    if address_type == 0x01:
        _recv_exact(connection, 4)
    elif address_type == 0x03:
        length = _recv_exact(connection, 1)[0]
        _recv_exact(connection, length)
    elif address_type == 0x04:
        _recv_exact(connection, 16)
    else:
        raise ConnectionError(
            f"SOCKS5 proxy returned unknown address type 0x{address_type:02x}"
        )
    _recv_exact(connection, 2)


def open_socks5_tunnel(
    proxy: Endpoint,
    target: Endpoint,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: float = CONNECT_TIMEOUT_SECONDS,
) -> socket.socket:
    """Open a TCP tunnel and ask the SOCKS5 proxy to resolve target.host."""

    connection = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        credentials_supplied = username is not None
        methods = b"\x00\x02" if credentials_supplied else b"\x00"
        connection.sendall(b"\x05" + bytes((len(methods),)) + methods)
        version, method = _recv_exact(connection, 2)
        if version != 0x05:
            raise ConnectionError(
                f"SOCKS5 proxy returned protocol version 0x{version:02x}"
            )
        if method == 0xFF:
            raise ConnectionError("SOCKS5 proxy accepted no authentication method")
        if method == 0x02:
            if not credentials_supplied:
                raise ConnectionError("SOCKS5 proxy requires username/password")
            username_bytes = username.encode("utf-8")
            password_bytes = (password or "").encode("utf-8")
            if not 1 <= len(username_bytes) <= 255 or len(password_bytes) > 255:
                raise ValueError("SOCKS5 credentials exceed protocol limits")
            auth_request = b"\x01" + bytes((len(username_bytes),)) + username_bytes
            auth_request += bytes((len(password_bytes),)) + password_bytes
            connection.sendall(auth_request)
            auth_version, auth_status = _recv_exact(connection, 2)
            if auth_version != 0x01 or auth_status != 0x00:
                raise ConnectionError("SOCKS5 username/password authentication failed")
        elif method != 0x00:
            raise ConnectionError(
                f"SOCKS5 proxy selected unsupported authentication method 0x{method:02x}"
            )

        # ATYP 03 deliberately sends the hostname to the remote proxy.  Do not
        # resolve the Solinteg endpoint locally: the local /32 route points back
        # at the interception host and would create a routing loop.
        target_bytes = target.host.encode("idna")
        if not 1 <= len(target_bytes) <= 255:
            raise ValueError("SOCKS5 target hostname exceeds protocol limits")
        connect_request = b"\x05\x01\x00\x03" + bytes((len(target_bytes),))
        connect_request += target_bytes + struct.pack(">H", target.port)
        connection.sendall(connect_request)

        version, reply, reserved, address_type = _recv_exact(connection, 4)
        if version != 0x05 or reserved != 0x00:
            raise ConnectionError("invalid SOCKS5 CONNECT response")
        _read_socks5_address(connection, address_type)
        if reply != 0x00:
            descriptions = {
                0x01: "general failure",
                0x02: "connection not allowed",
                0x03: "network unreachable",
                0x04: "host unreachable",
                0x05: "connection refused",
                0x06: "TTL expired",
                0x07: "command unsupported",
                0x08: "address type unsupported",
            }
            description = descriptions.get(reply, "unknown error")
            raise ConnectionError(
                f"SOCKS5 CONNECT failed: {description} (0x{reply:02x})"
            )

        connection.setblocking(False)
        return connection
    except BaseException:
        connection.close()
        raise


def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build_cloud_write_ack(command: bytes, device_timestamp: bytes) -> bytes:
    """Build the genuine 58-byte inverter acknowledgement for an 01:10 write."""

    if len(command) != 58:
        raise ValueError(f"cloud write is {len(command)} bytes, expected 58")
    parse_cloud_write(command)
    if _crc16_modbus(command[:-2]) != int.from_bytes(command[-2:], "little"):
        raise ValueError("cloud write has invalid CRC")
    if len(device_timestamp) != 6:
        raise ValueError("device timestamp must contain six bytes")

    # Genuine inverter replies preserve the command header, identifier,
    # reserved bytes and target range. They replace the command timestamp,
    # replace all register values with one-byte status 01, pad with FF, and
    # calculate a new CRC over the 56-byte body.
    body = b"".join(
        (
            command[:24],
            device_timestamp,
            command[30:44],
            b"\x01",
            b"\xff" * 11,
        )
    )
    if len(body) != 56:
        raise AssertionError("internal cloud-write acknowledgement length error")
    return body + struct.pack("<H", _crc16_modbus(body))


def advance_device_timestamp(timestamp: bytes, elapsed_seconds: float) -> bytes:
    """Advance a six-byte Solinteg timestamp by elapsed monotonic time."""

    if len(timestamp) != 6:
        raise ValueError("device timestamp must contain six bytes")
    try:
        observed = datetime(
            2000 + timestamp[0],
            timestamp[1],
            timestamp[2],
            timestamp[3],
            timestamp[4],
            timestamp[5],
        )
    except ValueError as error:
        raise ValueError("device timestamp contains an invalid date or time") from error

    # Never invent time before the observation, and do not round into the
    # future. Genuine command replies advance the module clock continuously
    # rather than repeating the timestamp of the preceding telemetry frame.
    advanced = observed + timedelta(seconds=max(0, int(elapsed_seconds)))
    return bytes(
        (
            advanced.year % 100,
            advanced.month,
            advanced.day,
            advanced.hour,
            advanced.minute,
            advanced.second,
        )
    )


def patch_register_snapshot(
    frame: bytes,
    replacements: dict[int, int],
    timestamp: Optional[bytes] = None,
) -> tuple[bytes, tuple[int, ...]]:
    """Patch selected register words in a cloud-bound telemetry snapshot."""

    if len(frame) < 51 or frame[:2] != MAGIC:
        raise ValueError("not a complete Solinteg register snapshot")
    if int.from_bytes(frame[2:6], "big") + 9 != len(frame):
        raise ValueError("register snapshot has an invalid declared length")
    if _crc16_modbus(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise ValueError("register snapshot has invalid CRC")
    if timestamp is not None and len(timestamp) != 6:
        raise ValueError("device timestamp must contain six bytes")

    patched = bytearray(frame)
    if timestamp is not None:
        patched[26:32] = timestamp
        patched[42:48] = timestamp

    cursor = 49
    data_end = len(frame) - 2
    patched_addresses: list[int] = []
    for _range_number in range(frame[48]):
        if cursor + 4 > data_end:
            raise ValueError("truncated register-range header")
        start, end = struct.unpack_from(">HH", frame, cursor)
        cursor += 4
        if end < start:
            raise ValueError(f"descending register range {start}..{end}")
        word_count = end - start + 1
        values_end = cursor + word_count * 2
        if values_end > data_end:
            raise ValueError(f"register range {start}..{end} is truncated")
        for address in range(start, end + 1):
            value = replacements.get(address)
            if value is None:
                continue
            if not 0 <= value <= 0xFFFF:
                raise ValueError(f"replacement for register {address} is not U16")
            struct.pack_into(">H", patched, cursor + (address - start) * 2, value)
            patched_addresses.append(address)
        cursor = values_end

    patched[-2:] = struct.pack("<H", _crc16_modbus(patched[:-2]))
    return bytes(patched), tuple(patched_addresses)


class CloudForwarder:
    """Mirror acknowledged inverter frames through SOCKS5 in isolation."""

    def __init__(
        self,
        proxy: Endpoint,
        target: Endpoint,
        incoming_log_file: Path,
        username: Optional[str] = None,
        password: Optional[str] = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        verbose_registers: bool = False,
        incoming_handler: Optional[Callable[[bytes], str]] = None,
        fake_ack_cloud_writes: bool = False,
        shadow_retention_seconds: float = DEFAULT_SHADOW_RETENTION_SECONDS,
    ) -> None:
        self.proxy = proxy
        self.target = target
        self.incoming_log_file = incoming_log_file
        self.username = username
        self.password = password
        self.verbose_registers = verbose_registers
        self.incoming_handler = incoming_handler
        self.fake_ack_cloud_writes = fake_ack_cloud_writes
        self.shadow_retention_seconds = shadow_retention_seconds
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=queue_size)
        self._generated_replies: deque[GeneratedCloudFrame] = deque(
            maxlen=GENERATED_REPLY_QUEUE_SIZE
        )
        self._scheduled_replies: deque[tuple[float, GeneratedCloudFrame]] = deque(
            maxlen=GENERATED_REPLY_QUEUE_SIZE
        )
        self._state_lock = threading.Lock()
        self._latest_device_timestamp: Optional[bytes] = None
        self._latest_device_timestamp_observed_at: Optional[float] = None
        self._latest_configuration_frame: Optional[bytes] = None
        self._shadow_registers: dict[int, int] = {}
        self._shadow_expires_at: Optional[float] = None
        self._pending_shadow_confirmations: dict[tuple[bytes, bytes], int] = {}
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="solinteg-cloud-forwarder",
            daemon=True,
        )
        self._dropped_frames = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop_event.set()
        self._thread.join(timeout=timeout)

    def enqueue(self, frame: bytes) -> None:
        """Queue one copy without waiting for SOCKS, DNS, cloud, or disk I/O."""

        self._expire_shadow_if_due()
        message_type = frame[6:8] if len(frame) >= 8 else b""
        if len(frame) >= 32 and message_type in CLOUD_TELEMETRY_ACK_TYPES:
            with self._state_lock:
                self._latest_device_timestamp = frame[26:32]
                self._latest_device_timestamp_observed_at = time.monotonic()
                if message_type == b"\x01\x03":
                    # Keep the real frame as the source for later cloud-only
                    # shadow snapshots. Never feed a patched copy back here.
                    self._latest_configuration_frame = frame
                shadow_registers = dict(self._shadow_registers)
            if shadow_registers:
                try:
                    cloud_frame, patched_addresses = patch_register_snapshot(
                        frame,
                        shadow_registers,
                    )
                except ValueError as error:
                    logging.error("could not apply cloud register shadow: %s", error)
                else:
                    if patched_addresses:
                        frame = cloud_frame
                        logging.info(
                            "cloud-bound frame shadowed: type=%s registers=%s",
                            message_type.hex(":"),
                            ",".join(str(value) for value in patched_addresses),
                        )

        try:
            self._queue.put_nowait(frame)
            return
        except queue.Full:
            pass

        # Prefer current telemetry to stale telemetry if the remote path is down.
        try:
            self._queue.get_nowait()
            self._dropped_frames += 1
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            self._dropped_frames += 1

    def _append_incoming(
        self,
        kind: str,
        payload: bytes,
        action: str = "ignored",
    ) -> None:
        record = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "direction": "cloud_to_inverter",
            "action": action,
            "kind": kind,
            "target": str(self.target),
            "length": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "frame_base64": base64.b64encode(payload).decode("ascii"),
        }
        if kind == "st_frame" and len(payload) >= 8:
            record["message_type"] = payload[6:8].hex(":")
            if len(payload) >= 2:
                record["crc_valid"] = _crc16_modbus(payload[:-2]) == int.from_bytes(
                    payload[-2:], "little"
                )

        encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.incoming_log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(self.incoming_log_file, flags, 0o600)
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short write while saving cloud input")
                    view = view[written:]
            finally:
                os.close(descriptor)
        except OSError as error:
            logging.error("could not save cloud input: %s", error)
            return

        if kind == "st_frame":
            message_type = payload[6:8] if len(payload) >= 8 else b""
            if message_type == CLOUD_WRITE_TYPE:
                self._log_cloud_write(payload, action, record["sha256"])
            else:
                logging.info(
                    "cloud frame %s and logged: type=%s length=%d sha256=%s",
                    action.replace("_", " "),
                    record.get("message_type", "unknown"),
                    len(payload),
                    record["sha256"],
                )
        else:
            logging.warning(
                "cloud %s bytes ignored and logged: length=%d sha256=%s",
                kind,
                len(payload),
                record["sha256"],
            )

    def _log_cloud_write(self, frame: bytes, action: str, sha256: str) -> None:
        try:
            command = parse_cloud_write(frame)
        except ValueError as error:
            logging.warning(
                "cloud type=01:10 frame %s and logged but could not be decoded: "
                "%s sha256=%s",
                action.replace("_", " "),
                error,
                sha256,
            )
            return

        logging.warning(
            "cloud write %s and logged: registers=%d..%d count=%d sha256=%s",
            action.replace("_", " "),
            command.start,
            command.end,
            len(command.values),
            sha256,
        )
        if self.verbose_registers:
            for decoded in decode_register_range(command.start, command.values):
                logging.info(
                    "cloud write action=%s register=%d name=%r raw=%s value=%r",
                    action,
                    decoded.address,
                    decoded.name,
                    list(decoded.raw_words),
                    decoded.value,
                )
        if command.padding and any(value != 0xFF for value in command.padding):
            logging.warning(
                "cloud write registers=%d..%d has %d padding byte(s), including "
                "non-FF data: %s",
                command.start,
                command.end,
                len(command.padding),
                command.padding.hex(),
            )

    def _current_device_timestamp(self) -> bytes:
        with self._state_lock:
            timestamp = self._latest_device_timestamp
            observed_at = self._latest_device_timestamp_observed_at
        if timestamp is not None and observed_at is not None:
            try:
                return advance_device_timestamp(
                    timestamp,
                    time.monotonic() - observed_at,
                )
            except ValueError as error:
                logging.warning(
                    "could not advance cached inverter timestamp: %s; "
                    "using local clock",
                    error,
                )

        local_now = datetime.now().astimezone()
        return bytes(
            (
                local_now.year % 100,
                local_now.month,
                local_now.day,
                local_now.hour,
                local_now.minute,
                local_now.second,
            )
        )

    def _queue_generated(
        self,
        frame: bytes,
        purpose: str,
        delay: float = 0.0,
    ) -> None:
        generated = GeneratedCloudFrame(frame, purpose)
        if delay > 0.0:
            if len(self._scheduled_replies) == self._scheduled_replies.maxlen:
                self._scheduled_replies.popleft()
                logging.warning(
                    "scheduled cloud-shadow queue discarded its oldest frame"
                )
            self._scheduled_replies.append((time.monotonic() + delay, generated))
            return
        if len(self._generated_replies) == self._generated_replies.maxlen:
            self._generated_replies.popleft()
            logging.warning("generated cloud-frame queue discarded its oldest frame")
        self._generated_replies.append(generated)

    def _track_shadow_confirmation(self, frame: bytes) -> None:
        key = (frame[6:8], frame[26:32])
        with self._state_lock:
            self._pending_shadow_confirmations[key] = (
                self._pending_shadow_confirmations.get(key, 0) + 1
            )

    def _expire_shadow_if_due(self, now: Optional[float] = None) -> bool:
        """Discard an accumulated cloud-only register shadow after its grace period."""

        if now is None:
            now = time.monotonic()
        with self._state_lock:
            expires_at = self._shadow_expires_at
            if expires_at is None or now < expires_at:
                return False
            register_count = len(self._shadow_registers)
            pending_count = sum(self._pending_shadow_confirmations.values())
            self._shadow_registers.clear()
            self._shadow_expires_at = None
            self._pending_shadow_confirmations.clear()
        logging.warning(
            "cloud command shadow expired after %.1f seconds: registers=%d "
            "unacknowledged_confirmations=%d; genuine values will be sent again",
            self.shadow_retention_seconds,
            register_count,
            pending_count,
        )
        return True

    def _consume_shadow_confirmation_ack(self, frame: bytes) -> bool:
        if len(frame) < 30:
            return False
        # Cloud ACK frames omit the two reserved bytes present in inverter
        # telemetry, so their matching timestamp is at bytes 24..29.
        key = (frame[6:8], frame[24:30])
        with self._state_lock:
            count = self._pending_shadow_confirmations.get(key, 0)
            if count == 0:
                return False
            if count == 1:
                del self._pending_shadow_confirmations[key]
            else:
                self._pending_shadow_confirmations[key] = count - 1
            remaining = sum(self._pending_shadow_confirmations.values())
            shadow_count = len(self._shadow_registers)
            expires_at = self._shadow_expires_at
            retain_for = (
                max(0.0, expires_at - time.monotonic())
                if expires_at is not None
                else 0.0
            )

        logging.warning(
            "cloud acknowledged shadow confirmation: type=%s device_time=%s "
            "remaining=%d",
            frame[6:8].hex(":"),
            "20%02d-%02d-%02d %02d:%02d:%02d" % tuple(frame[24:30]),
            remaining,
        )
        if remaining == 0:
            logging.warning(
                "cloud command confirmation complete; temporary register "
                "shadow retained for up to %.1f more seconds: registers=%d",
                retain_for,
                shadow_count,
            )
        return True

    def _route_cloud_frame(self, frame: bytes) -> str:
        message_type = frame[6:8] if len(frame) >= 8 else b""
        if len(frame) < 2 or _crc16_modbus(frame[:-2]) != int.from_bytes(
            frame[-2:], "little"
        ):
            return "ignored_invalid_crc"
        if message_type in CLOUD_TELEMETRY_ACK_TYPES:
            if self._consume_shadow_confirmation_ack(frame):
                return "shadow_confirmation_acknowledged"
            return "ignored_local_ack_already_sent"
        if self.incoming_handler is not None:
            try:
                return self.incoming_handler(frame)
            except Exception as error:
                logging.error("cloud input router failed; frame ignored: %s", error)
                return "ignored_router_error"
        if self.fake_ack_cloud_writes and message_type == CLOUD_WRITE_TYPE:
            timestamp = self._current_device_timestamp()
            try:
                command = parse_cloud_write(frame)
                acknowledgement = build_cloud_write_ack(frame, timestamp)
            except ValueError as error:
                logging.error("could not build fake cloud-write ACK: %s", error)
                return "ignored_fake_ack_error"

            with self._state_lock:
                self._shadow_registers.update(
                    zip(
                        range(command.start, command.end + 1),
                        command.values,
                    )
                )
                # Closely spaced app changes form one accumulating transaction
                # window. Every later 01:03 sent during the window contains all
                # staged values, even if acknowledgements for earlier commands
                # arrive in between.
                self._shadow_expires_at = (
                    time.monotonic() + self.shadow_retention_seconds
                )
                shadow_registers = dict(self._shadow_registers)
                configuration_frame = self._latest_configuration_frame

            confirmation_count = 0
            if configuration_frame is not None:
                try:
                    first_confirmation, first_patched = patch_register_snapshot(
                        configuration_frame,
                        shadow_registers,
                        timestamp,
                    )
                    target_addresses = set(range(command.start, command.end + 1))
                    if target_addresses.intersection(first_patched):
                        second_timestamp = advance_device_timestamp(
                            timestamp,
                            SHADOW_CONFIRMATION_DELAY_SECONDS,
                        )
                        second_confirmation, _second_patched = (
                            patch_register_snapshot(
                                configuration_frame,
                                shadow_registers,
                                second_timestamp,
                            )
                        )
                        self._queue_generated(
                            first_confirmation,
                            "shadow_confirmation",
                        )
                        self._track_shadow_confirmation(first_confirmation)
                        confirmation_count += 1
                        self._queue_generated(
                            second_confirmation,
                            "shadow_confirmation",
                            SHADOW_CONFIRMATION_DELAY_SECONDS,
                        )
                        self._track_shadow_confirmation(second_confirmation)
                        confirmation_count += 1
                except ValueError as error:
                    logging.error(
                        "could not build cloud-shadow confirmation: %s",
                        error,
                    )
            else:
                logging.warning(
                    "no cached 01:03 configuration frame is available for "
                    "cloud-shadow confirmation"
                )

            # The genuine sequence puts the first refreshed 01:03 before the
            # 01:10 command response, followed by a second 01:03 about two
            # seconds later.
            self._queue_generated(acknowledgement, "write_ack")
            if confirmation_count == 0:
                logging.warning(
                    "cloud write has no immediate 01:03 confirmation; its "
                    "register shadow remains eligible for later configuration "
                    "snapshots for %.1f seconds",
                    self.shadow_retention_seconds,
                )
            return (
                "fake_ack_and_shadow_queued"
                if confirmation_count
                else "fake_ack_queued"
            )
        return "ignored"

    def _consume_incoming(self, buffer: bytearray) -> None:
        while buffer:
            magic_offset = buffer.find(MAGIC)
            if magic_offset < 0:
                keep = 1 if buffer[-1:] == b"S" else 0
                payload_length = len(buffer) - keep
                if payload_length:
                    self._append_incoming("unframed", bytes(buffer[:payload_length]))
                    del buffer[:payload_length]
                return
            if magic_offset:
                self._append_incoming("unframed", bytes(buffer[:magic_offset]))
                del buffer[:magic_offset]
            if len(buffer) < 6:
                return

            total_length = int.from_bytes(buffer[2:6], "big") + 9
            if total_length < 9 or total_length > MAX_FRAME_LENGTH:
                self._append_incoming("unframed", bytes(buffer[:2]))
                del buffer[:2]
                continue
            if len(buffer) < total_length:
                return
            frame = bytes(buffer[:total_length])
            del buffer[:total_length]
            self._append_incoming(
                "st_frame",
                frame,
                self._route_cloud_frame(frame),
            )

    def _close_connection(
        self,
        connection: Optional[socket.socket],
        incoming_buffer: bytearray,
    ) -> None:
        if incoming_buffer:
            self._append_incoming("partial", bytes(incoming_buffer))
            incoming_buffer.clear()
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _run(self) -> None:
        connection: Optional[socket.socket] = None
        incoming_buffer = bytearray()
        pending_frame: Optional[bytes] = None
        pending_offset = 0
        pending_generated_purpose: Optional[str] = None
        activated = False
        reconnect_delay = 0.0
        next_connect_at = 0.0
        reported_drops = 0
        connected_at: Optional[float] = None

        while not self._stop_event.is_set():
            now = time.monotonic()
            self._expire_shadow_if_due(now)
            while (
                self._scheduled_replies
                and self._scheduled_replies[0][0] <= now
            ):
                _send_at, generated = self._scheduled_replies.popleft()
                if len(self._generated_replies) == self._generated_replies.maxlen:
                    self._generated_replies.popleft()
                    logging.warning(
                        "generated cloud-frame queue discarded its oldest frame"
                    )
                self._generated_replies.append(generated)

            if pending_frame is None:
                if self._generated_replies:
                    generated = self._generated_replies.popleft()
                    pending_frame = generated.frame
                    pending_offset = 0
                    pending_generated_purpose = generated.purpose
                else:
                    try:
                        pending_frame = self._queue.get_nowait()
                        pending_offset = 0
                        pending_generated_purpose = None
                        activated = True
                    except queue.Empty:
                        pass

            if self._dropped_frames != reported_drops:
                difference = self._dropped_frames - reported_drops
                reported_drops = self._dropped_frames
                logging.warning(
                    "cloud mirror queue discarded %d stale frame(s); total=%d",
                    difference,
                    reported_drops,
                )

            if connection is None:
                if not activated:
                    self._stop_event.wait(SELECT_TIMEOUT_SECONDS)
                    continue
                now = time.monotonic()
                if now < next_connect_at:
                    self._stop_event.wait(
                        min(SELECT_TIMEOUT_SECONDS, next_connect_at - now)
                    )
                    continue
                try:
                    connection = open_socks5_tunnel(
                        self.proxy,
                        self.target,
                        self.username,
                        self.password,
                    )
                    connected_at = time.monotonic()
                    logging.info(
                        "cloud mirror connected through SOCKS5 %s to %s",
                        self.proxy,
                        self.target,
                    )
                except (ConnectionError, OSError, ValueError) as error:
                    logging.warning(
                        "cloud mirror connection failed through SOCKS5 %s: %s; "
                        "local acknowledgements are unaffected",
                        self.proxy,
                        error,
                    )
                    next_connect_at = time.monotonic() + reconnect_delay
                    reconnect_delay = (
                        0.1
                        if reconnect_delay == 0.0
                        else min(reconnect_delay * 2.0, MAX_RECONNECT_DELAY_SECONDS)
                    )
                    continue

            readers = [connection]
            writers = [connection] if pending_frame is not None else []
            try:
                readable, writable, _exceptional = select.select(
                    readers,
                    writers,
                    readers,
                    SELECT_TIMEOUT_SECONDS,
                )
                if connection in writable and pending_frame is not None:
                    written = connection.send(pending_frame[pending_offset:])
                    if written <= 0:
                        raise ConnectionError("cloud socket accepted no outgoing bytes")
                    pending_offset += written
                    if pending_offset == len(pending_frame):
                        if pending_generated_purpose == "write_ack":
                            logging.warning(
                                "fake cloud-write ACK sent to socket: type=%s "
                                "length=%d device_time=%s sha256=%s",
                                pending_frame[6:8].hex(":")
                                if len(pending_frame) >= 8
                                else "unknown",
                                len(pending_frame),
                                "20%02d-%02d-%02d %02d:%02d:%02d"
                                % tuple(pending_frame[24:30]),
                                hashlib.sha256(pending_frame).hexdigest(),
                            )
                            if self.verbose_registers:
                                logging.info(
                                    "fake cloud-write ACK frame_base64=%s",
                                    base64.b64encode(pending_frame).decode("ascii"),
                                )
                        elif pending_generated_purpose == "shadow_confirmation":
                            logging.warning(
                                "cloud-shadow confirmation sent to socket: "
                                "type=%s length=%d device_time=%s sha256=%s",
                                pending_frame[6:8].hex(":"),
                                len(pending_frame),
                                "20%02d-%02d-%02d %02d:%02d:%02d"
                                % tuple(pending_frame[26:32]),
                                hashlib.sha256(pending_frame).hexdigest(),
                            )
                            if self.verbose_registers:
                                logging.info(
                                    "cloud-shadow confirmation frame_base64=%s",
                                    base64.b64encode(pending_frame).decode("ascii"),
                                )
                        else:
                            logging.info(
                                "cloud mirror forwarded frame: type=%s length=%d",
                                pending_frame[6:8].hex(":")
                                if len(pending_frame) >= 8
                                else "unknown",
                                len(pending_frame),
                            )
                        pending_frame = None
                        pending_offset = 0
                        pending_generated_purpose = None

                if connection in readable:
                    incoming = connection.recv(65536)
                    if not incoming:
                        raise ConnectionError("cloud closed the mirrored connection")
                    incoming_buffer.extend(incoming)
                    self._consume_incoming(incoming_buffer)

                if connection in _exceptional:
                    raise ConnectionError("cloud mirror socket exception")
            except (ConnectionError, OSError, ValueError) as error:
                logging.warning("cloud mirror disconnected: %s", error)
                self._close_connection(connection, incoming_buffer)
                connection = None
                if (
                    connected_at is not None
                    and time.monotonic() - connected_at >= 60.0
                ):
                    reconnect_delay = 0.0
                connected_at = None
                # A partially transmitted TCP frame is retried whole after
                # reconnect. A generated reply belongs to the old cloud session
                # and is instead discarded.
                if pending_generated_purpose is not None:
                    pending_frame = None
                    pending_generated_purpose = None
                pending_offset = 0
                self._generated_replies.clear()
                self._scheduled_replies.clear()
                with self._state_lock:
                    self._shadow_registers.clear()
                    self._shadow_expires_at = None
                    self._pending_shadow_confirmations.clear()
                next_connect_at = time.monotonic() + reconnect_delay
                reconnect_delay = (
                    0.1
                    if reconnect_delay == 0.0
                    else min(reconnect_delay * 2.0, MAX_RECONNECT_DELAY_SECONDS)
                )

        self._close_connection(connection, incoming_buffer)
