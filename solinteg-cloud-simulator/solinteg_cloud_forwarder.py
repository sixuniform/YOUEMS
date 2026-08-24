#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Rickard Dahlstedt
"""Asynchronous SOCKS5 cloud mirror for solinteg-cloud-simulator.

All network and file operations in this module run in a dedicated daemon
thread.  The simulator's inverter-facing request handler only performs a
bounded, non-blocking queue insertion after it has sent the local reply.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, NamedTuple, Optional


MAGIC: Final = b"ST"
MAX_FRAME_LENGTH: Final = 1024 * 1024
DEFAULT_QUEUE_SIZE: Final = 256
CONNECT_TIMEOUT_SECONDS: Final = 10.0
SELECT_TIMEOUT_SECONDS: Final = 0.25
MAX_RECONNECT_DELAY_SECONDS: Final = 30.0


class Endpoint(NamedTuple):
    host: str
    port: int

    def __str__(self) -> str:
        if ":" in self.host:
            return f"[{self.host}]:{self.port}"
        return f"{self.host}:{self.port}"


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
    ) -> None:
        self.proxy = proxy
        self.target = target
        self.incoming_log_file = incoming_log_file
        self.username = username
        self.password = password
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=queue_size)
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

    def _append_incoming(self, kind: str, payload: bytes) -> None:
        record = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "direction": "cloud_to_inverter",
            "action": "ignored",
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
            logging.error("could not save ignored cloud input: %s", error)
            return

        if kind == "st_frame":
            logging.info(
                "cloud frame ignored and logged: type=%s length=%d sha256=%s",
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
            self._append_incoming("st_frame", frame)

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
        activated = False
        reconnect_delay = 0.0
        next_connect_at = 0.0
        reported_drops = 0
        connected_at: Optional[float] = None

        while not self._stop_event.is_set():
            if pending_frame is None:
                try:
                    pending_frame = self._queue.get_nowait()
                    pending_offset = 0
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
                        logging.info(
                            "cloud mirror forwarded frame: type=%s length=%d",
                            pending_frame[6:8].hex(":")
                            if len(pending_frame) >= 8
                            else "unknown",
                            len(pending_frame),
                        )
                        pending_frame = None
                        pending_offset = 0

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
                # reconnect.  The real endpoint already tolerates retransmission.
                pending_offset = 0
                next_connect_at = time.monotonic() + reconnect_delay
                reconnect_delay = (
                    0.1
                    if reconnect_delay == 0.0
                    else min(reconnect_delay * 2.0, MAX_RECONNECT_DELAY_SECONDS)
                )

        self._close_connection(connection, incoming_buffer)
