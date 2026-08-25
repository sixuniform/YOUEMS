#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Rickard Dahlstedt

import asyncio
import logging
import socket
import struct
import json
import os
import sys
import importlib.util
from pathlib import Path
from datetime import datetime
from time import monotonic
from threading import Lock
from pymodbus.server import StartAsyncTcpServer
from pymodbus.client import ModbusTcpClient
from pymodbus.datastore import (
    ModbusServerContext,
    ModbusSlaveContext,
    ModbusSparseDataBlock
)

def _env_int(name, default):
    return int(os.environ.get(name, str(default)))


def _env_float(name, default):
    return float(os.environ.get(name, str(default)))


VERSION = "5.17"

BROKER_IP      = os.environ.get("BROKER_IP", "0.0.0.0")
READONLY_PORT  = _env_int("READONLY_PORT", 502)
READWRITE_PORT = _env_int("READWRITE_PORT", 503)

INVERTER_IP    = os.environ.get("INVERTER_IP", "192.168.10.152")
INVERTER_PORT  = _env_int("INVERTER_PORT", 502)
UPSTREAM_TIMEOUT_SECONDS = _env_float("UPSTREAM_TIMEOUT_SECONDS", 10.0)

# Reconnect backoff:
#   first retry immediately, then 0.1, 0.2, 0.4, ... seconds, capped at 5 s.
# The backoff only resets after the upstream connection has remained healthy
# continuously for 60 seconds.
RECONNECT_BACKOFF_INITIAL_SECONDS = _env_float("RECONNECT_BACKOFF_INITIAL_SECONDS", 0.1)
RECONNECT_BACKOFF_MAX_SECONDS = _env_float("RECONNECT_BACKOFF_MAX_SECONDS", 5.0)
RECONNECT_BACKOFF_RESET_SECONDS = _env_float("RECONNECT_BACKOFF_RESET_SECONDS", 60.0)

# STRICT UNIT ID ENFORCEMENT
UNIT_ID        = _env_int("UNIT_ID", 255)

# --- GLOBAL DYNAMIC CONFIGURATION STATE ---
FORBIDDEN_REGISTERS = set()
WRITE_CONVERSIONS   = {}
READ_OVERRIDES      = {}

CONFIG_PATH         = os.environ.get("MODBUS_RULES_PATH", "modbus_rules.json")
_LAST_CONFIG_MTIME  = 0
_CONFIG_WAS_MISSING = False  # Track state to avoid log spamming

# --- CANONICAL SOLINTEG REGISTER MAP ---
# There must be exactly one maintained register translation source:
#   sixuniform/YOUEMS/solinteg-cloud-simulator/solinteg_modbus_map.py
#
# Search order allows the broker to run from the YOUEMS repository root,
# alongside the canonical file, or from another installation path explicitly
# configured with SOLINTEG_MODBUS_MAP_PATH.
def _load_canonical_solinteg_map():
    broker_dir = Path(__file__).resolve().parent

    candidates = []
    configured = os.environ.get("SOLINTEG_MODBUS_MAP_PATH")
    if configured:
        candidates.append(Path(configured).expanduser())

    # Prefer the canonical file in the YOUEMS repository tree; do not keep a
    # second broker-local mapping copy.
    cwd = Path.cwd().resolve()
    candidates.extend(
        [
            broker_dir / "solinteg-cloud-simulator" / "solinteg_modbus_map.py",
            broker_dir / "YOUEMS" / "solinteg-cloud-simulator" / "solinteg_modbus_map.py",
            cwd / "solinteg-cloud-simulator" / "solinteg_modbus_map.py",
            cwd / "YOUEMS" / "solinteg-cloud-simulator" / "solinteg_modbus_map.py",

            # Also allow the canonical map file itself to be placed directly
            # in the broker's/current working directory.
            broker_dir / "solinteg_modbus_map.py",
            cwd / "solinteg_modbus_map.py",
        ]
    )

    checked = []
    for candidate in candidates:
        candidate = candidate.resolve()
        checked.append(str(candidate))
        if not candidate.is_file():
            continue

        spec = importlib.util.spec_from_file_location(
            "youems_solinteg_modbus_map",
            candidate,
        )
        if spec is None or spec.loader is None:
            continue

        module = importlib.util.module_from_spec(spec)
        # Register the module while executing it so Python features that inspect
        # __module__ work normally.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        metadata = getattr(module, "REGISTER_METADATA", None)
        decoder = getattr(module, "decode_register_range", None)
        if not isinstance(metadata, dict) or not callable(decoder):
            raise RuntimeError(
                f"Canonical Solinteg map {candidate} does not expose "
                "REGISTER_METADATA and decode_register_range()"
            )

        return candidate, metadata, decoder

    raise RuntimeError(
        "Canonical Solinteg register map not found. Checked: "
        + ", ".join(checked)
        + ". Set SOLINTEG_MODBUS_MAP_PATH to "
          "sixuniform/YOUEMS/solinteg-cloud-simulator/solinteg_modbus_map.py "
          "if the YOUEMS repository is elsewhere."
    )


SOLINTEG_MODBUS_MAP_PATH, REGISTER_METADATA, decode_register_range = (
    _load_canonical_solinteg_map()
)


logging.basicConfig(level=logging.CRITICAL)

# Keep expected proxy/upstream failures concise.
# The broker logs READ_FAIL / WRITE_FAIL itself. Pymodbus otherwise emits
# a full Python traceback ("Datastore unable to fulfill request") and a
# second "Exception Response(... SlaveFailure)" line for the same event.
for _logger_name in (
    "pymodbus.server.async_io",
    "pymodbus.pdu",
):
    logging.getLogger(_logger_name).setLevel(logging.CRITICAL)

# Socket patch for immediate reuse
_original_socket = socket.socket
def _patched_socket(*args, **kwargs):
    sock = _original_socket(*args, **kwargs)
    if args and args[0] in (socket.AF_INET, socket.AF_INET6):
        if len(args) > 1 and args[1] == socket.SOCK_STREAM:
            try: sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except Exception: pass
    return sock
socket.socket = _patched_socket



def decode_and_log(action, start_address, values, tag="-", elapsed=None):
    """Log a register range using the canonical YOUEMS Solinteg decoder."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timing = f"[{elapsed:.2f}s]" if elapsed is not None else ""

    if not isinstance(values, list):
        values = [values]

    try:
        decoded_values = decode_register_range(start_address, values)
        for decoded in decoded_values:
            raw_words = list(decoded.raw_words)
            if decoded.name == "Raw Field":
                raw_value = raw_words[0] if raw_words else ""
                print(
                    f"{ts}{timing},{tag},{action},Reg:{decoded.address} "
                    f"[Raw Field],Value:[{raw_value}]",
                    flush=True,
                )
            else:
                print(
                    f"{ts}{timing},{tag},{action},Reg:{decoded.address} "
                    f"[{decoded.name}],Raw:{raw_words} -> {decoded.value}",
                    flush=True,
                )
    except Exception as exc:
        # Logging must never be able to break the Modbus transaction path.
        print(
            f"{ts}{timing},{tag},{action},Reg:{start_address} "
            f"[DECODE_ERROR],Raw:{values!r},"
            f"Error:{type(exc).__name__}: {exc}",
            flush=True,
        )



def log_request_failure(action, address, error, tag="-", elapsed=None):
    """Log an upstream failure without inventing or returning a register value."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timing = f"[{elapsed:.2f}s]" if elapsed is not None else ""
    meta = REGISTER_METADATA.get(address)
    reg_label = f" [{meta['name']}]" if meta else " [Raw Field]"
    err_text = str(error).replace("\r", " ").replace("\n", " ")
    print(f"{ts}{timing},{tag},{action},Reg:{address}{reg_label},Error:{type(error).__name__}: {err_text}", flush=True)


# --- CONFIGURATION FILE ASYNC ENGINE RULES WATCHER ---
async def watch_rules_config():
    """Asynchronously monitors the config file. Falls back gracefully if missing."""
    global FORBIDDEN_REGISTERS, WRITE_CONVERSIONS, READ_OVERRIDES 
    global _LAST_CONFIG_MTIME, _CONFIG_WAS_MISSING
    
    while True:
        if os.path.exists(CONFIG_PATH):
            try:
                mtime = os.path.getmtime(CONFIG_PATH)
                if mtime != _LAST_CONFIG_MTIME:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    
                    # Parse configurations, safely ignoring the lazy "__END__" fields
                    new_forbidden = set(int(r) for r in data.get("FORBIDDEN_REGISTERS", []))
                    
                    new_conversions = {
                        int(reg): {int(val): int(target) for val, target in rules.items() if val != "__END__"}
                        for reg, rules in data.get("WRITE_CONVERSIONS", {}).items()
                        if reg != "__END__"
                    }
                    
                    new_overrides = {
                        int(reg): rule for reg, rule in data.get("READ_OVERRIDES", {}).items()
                        if reg != "__END__"
                    }
                    
                    # Swap references atomically
                    FORBIDDEN_REGISTERS = new_forbidden
                    WRITE_CONVERSIONS   = new_conversions
                    READ_OVERRIDES      = new_overrides
                    
                    _LAST_CONFIG_MTIME  = mtime
                    if _CONFIG_WAS_MISSING:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] [GLOBAL-CONFIG] Configuration file recovered successfully.")
                        _CONFIG_WAS_MISSING = False
                    else:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] [GLOBAL-CONFIG] Hot reload successful.")
                        
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [GLOBAL-CONFIG ERROR] Malformed content skipped: {e}")
        else:
            # File is completely missing
            if not _CONFIG_WAS_MISSING:
                # Flush to empty defaults immediately so active blocks disappear
                FORBIDDEN_REGISTERS.clear()
                WRITE_CONVERSIONS.clear()
                READ_OVERRIDES.clear()
                _LAST_CONFIG_MTIME = 0
                _CONFIG_WAS_MISSING = True
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [GLOBAL-CONFIG WARNING] '{CONFIG_PATH}' missing. Running with empty defaults.")
        
        await asyncio.sleep(10.0)


class ReconnectBackoffActive(ConnectionError):
    """Request arrived before the next upstream reconnect attempt is allowed."""
    pass


class InverterLink:
    def __init__(self):
        # Do not keep/reuse a pymodbus client after an upstream transport
        # failure. A timed-out TCP/framer instance may contain stale state.
        self.client = None
        self.cache = {}
        self.mutex = Lock()

        # Exponential reconnect state. 0.0 means the next reconnect attempt is
        # allowed immediately. After each unsuccessful recovery cycle the next
        # delay grows: 0.1, 0.2, 0.4, ... up to 5.0 seconds.
        self.reconnect_not_before = 0.0
        self.reconnect_backoff_next = 0.0
        self.connected_since = None

    def _new_client(self):
        return ModbusTcpClient(
            INVERTER_IP,
            port=INVERTER_PORT,
            timeout=UPSTREAM_TIMEOUT_SECONDS,
        )

    def _reset_reconnect_backoff_if_stable(self):
        """Reset recovery backoff after 60 seconds of continuous healthy use."""
        if self.connected_since is None:
            return

        if monotonic() - self.connected_since >= RECONNECT_BACKOFF_RESET_SECONDS:
            self.reconnect_backoff_next = 0.0

    def _schedule_reconnect_after_failure(self):
        """Schedule the next reconnect attempt using exponential backoff.

        Sequence:
            immediate -> 0.1 -> 0.2 -> 0.4 -> 0.8 -> 1.6 -> 3.2 -> 5.0 s

        reconnect_backoff_next contains the delay to apply *for this failure*.
        It is advanced only after scheduling, and is not reset merely because
        connect() succeeds. A connection must stay healthy for 60 seconds.
        """
        now = monotonic()

        # If the current connection had already proven itself healthy for the
        # reset interval, start a fresh recovery sequence with an immediate try.
        self._reset_reconnect_backoff_if_stable()

        delay = self.reconnect_backoff_next
        self.reconnect_not_before = now + delay

        if delay <= 0.0:
            self.reconnect_backoff_next = RECONNECT_BACKOFF_INITIAL_SECONDS
        else:
            self.reconnect_backoff_next = min(
                delay * 2.0,
                RECONNECT_BACKOFF_MAX_SECONDS,
            )

    def _mark_upstream_failure(self):
        """Discard the failed upstream client and schedule recovery."""
        failed_client = self.client
        self.client = None

        if failed_client is not None:
            try:
                failed_client.close()
            except Exception:
                pass

        self._schedule_reconnect_after_failure()
        self.connected_since = None

    def _ensure_upstream_connected(self):
        """Ensure a clean upstream TCP client exists when backoff permits.

        This method never advances the backoff itself. Real upstream failures
        are scheduled exactly once by read_registers()/write_registers().
        Requests merely arriving during an active backoff are rejected without
        altering the retry schedule.
        """

        # A long-lived healthy connection resets the recovery history.
        self._reset_reconnect_backoff_if_stable()

        remaining = self.reconnect_not_before - monotonic()
        if remaining > 0:
            raise ReconnectBackoffActive(
                f"Inverter reconnect backoff active ({remaining:.3f}s remaining)"
            )

        # Reuse a healthy persistent client.
        if self.client is not None:
            try:
                if self.client.connected:
                    return
            except Exception:
                pass

            # The old client is no longer usable. Discard it and immediately
            # attempt a fresh connection in this same request; if that attempt
            # fails, the caller will advance the backoff exactly once.
            stale_client = self.client
            self.client = None
            try:
                stale_client.close()
            except Exception:
                pass
            self.connected_since = None

        candidate = self._new_client()
        try:
            connected = candidate.connect()
        except Exception as exc:
            try:
                candidate.close()
            except Exception:
                pass
            self.client = None
            self.connected_since = None
            raise ConnectionError(f"Inverter connection failed: {exc}") from exc

        if not connected:
            try:
                candidate.close()
            except Exception:
                pass
            self.client = None
            self.connected_since = None
            raise ConnectionError("Inverter connection failed")

        self.client = candidate
        self.connected_since = monotonic()
        self.reconnect_not_before = 0.0

    def _is_data_sane(self, start_address, values, tag="-", elapsed=None):
        """Validates live telemetry power registers (S32, U32) and logs details on failure."""
        
        # Telemetry/Read-only real-time power streams ONLY
        S32_POWER_REGS = (10994, 10996, 10998, 11000, 11016, 30204, 30214, 30224, 30230, 30236, 30242, 30248, 30258)
        U32_POWER_REGS = (11028, 11062, 11064, 11066, 11068, 11070, 11072)

        for i in range(len(values)):
            addr = start_address + i
            
            # Explicitly leave control/EMS target registers (50000+) completely untouched
            if addr >= 50000:
                continue

            # 1. Handle 32-Bit Signed Registers (S32)
            if addr in S32_POWER_REGS:
                if i + 1 < len(values):
                    combined = (values[i] << 16) | values[i + 1]
                    raw_signed = struct.unpack('i', struct.pack('I', combined))[0]
                    
                    # Scale is uniform: 1000 raw units per 1 kW across all telemetry components
                    kw_val = abs(raw_signed) / 1000.0
                    
                    if kw_val > 100.0:
                        decode_and_log("SANITY_REJECTED", addr, [values[i], values[i+1]], 
                                       tag=f"{tag} | Reason: S32 Power Spike ({kw_val:.2f} kW)", elapsed=elapsed)
                        return False

            # 2. Handle 32-Bit Unsigned Registers (U32)
            elif addr in U32_POWER_REGS:
                if i + 1 < len(values):
                    raw_unsigned = (values[i] << 16) | values[i + 1]
                    kw_val = raw_unsigned / 1000.0
                    
                    if kw_val > 100.0:
                        decode_and_log("SANITY_REJECTED", addr, [values[i], values[i+1]], 
                                       tag=f"{tag} | Reason: U32 Power Spike ({kw_val:.2f} kW)", elapsed=elapsed)
                        return False
                        
        return True

    def read_registers(self, address, count=1, tag="-"):
        with self.mutex:
            inverter_address = address - 1
            display_address = address - 1

            request_started = monotonic()
            try:
                self._ensure_upstream_connected()
                client = self.client
                if client is None:
                    raise ConnectionError("Inverter client unavailable after connect")

                rr = client.read_holding_registers(address=inverter_address, count=count, slave=UNIT_ID)
                request_elapsed = monotonic() - request_started

                if rr.isError():
                    raise Exception(f"Modbus protocol exception: {rr}")

                values = rr.registers

                # --- VALIDATE LIVE TELEMETRY POWER STREAMS ---
                if not self._is_data_sane(display_address, values, tag=tag, elapsed=request_elapsed):
                    raise ValueError(f"Insane power reading rejected from inverter frame at address {display_address}")

                # --- SOCKET-AWARE PROXY INTERCEPTOR ---
                current_context = "RO" if tag.startswith("RO") else ("RW" if tag.startswith("RW") else "BOTH")

                for i in range(count):
                    current_addr = display_address + i
                    if current_addr in READ_OVERRIDES:
                        rule = READ_OVERRIDES[current_addr]
                        target_context = rule.get("target", "BOTH")

                        if target_context == "BOTH" or target_context == current_context:
                            simulated_value = rule["value"]
                            decode_and_log("READ_CONVERTED", display_address + i, simulated_value, tag=tag, elapsed=request_elapsed)

                            if current_addr in REGISTER_METADATA and REGISTER_METADATA[current_addr]["words"] == 2:
                                if i + 1 < count:
                                    values[i]     = (simulated_value >> 16) & 0xFFFF
                                    values[i + 1] = simulated_value & 0xFFFF
                            else:
                                if current_addr - 1 in READ_OVERRIDES and current_addr - 1 in REGISTER_METADATA and REGISTER_METADATA[current_addr - 1]["words"] == 2:
                                    continue
                                values[i] = simulated_value & 0xFFFF
                # --- END SOCKET-AWARE PROXY INTERCEPTOR ---

                self.cache[display_address] = values.copy()
                decode_and_log("READ", display_address, values, tag=tag, elapsed=request_elapsed)
                return values
            except Exception as e:
                # Do NOT hide an upstream failure behind stale/zero cache data.
                # A request rejected only because backoff is already active must
                # NOT extend/advance that backoff. Real upstream failures do.
                if not isinstance(e, ReconnectBackoffActive):
                    self._mark_upstream_failure()
                log_request_failure("READ_FAIL", display_address, e, tag=tag, elapsed=monotonic() - request_started)
                raise

    def write_registers(self, address, values, tag="-"):
        with self.mutex:
            inverter_address = address - 1
            display_address = address - 1

            if not isinstance(values, list):
                values = [values]
            send_values = values.copy()
            
            if display_address == 52503 and send_values[0] == 100:
                send_values[0] = 50

            request_started = monotonic()
            try:
                self._ensure_upstream_connected()
                client = self.client
                if client is None:
                    raise ConnectionError("Inverter client unavailable after connect")

                # Preserve normal Modbus write semantics upstream:
                # one register -> FC06, multiple registers -> FC16.
                if len(send_values) == 1:
                    rq = client.write_register(
                        address=inverter_address,
                        value=send_values[0],
                        slave=UNIT_ID,
                    )
                else:
                    rq = client.write_registers(
                        address=inverter_address,
                        values=send_values,
                        slave=UNIT_ID,
                    )

                request_elapsed = monotonic() - request_started

                if rq.isError():
                    raise Exception(f"Write error response: {rq}")

                self.cache[display_address] = values.copy()
                decode_and_log("WRITE", display_address, values, tag=tag, elapsed=request_elapsed)
            except Exception as e:
                # A failed write must never be cached as if it succeeded.
                # Backoff-only rejections do not alter the recovery schedule;
                # actual upstream failures advance the exponential backoff.
                if not isinstance(e, ReconnectBackoffActive):
                    self._mark_upstream_failure()
                log_request_failure("WRITE_FAIL", display_address, e, tag=tag, elapsed=monotonic() - request_started)
                raise


class ReadOnlyDataBlock(ModbusSparseDataBlock):
    def __init__(self, link: InverterLink):
        super().__init__({})
        self.link = link
    def validate(self, address, count=1): return True
    def getValues(self, address, count=1):
        return self.link.read_registers(address, count, tag=f"RO:{READONLY_PORT}")
    def setValues(self, address, values):
        display_address = int(address) - 1
        decode_and_log("WRITE_BLOCKED", display_address, values, tag=f"RO:{READONLY_PORT}")


class ReadWriteDataBlock(ModbusSparseDataBlock):
    def __init__(self, link: InverterLink):
        super().__init__({})
        self.link = link
    def validate(self, address, count=1): return True
    def getValues(self, address, count=1):
        return self.link.read_registers(address, count, tag=f"RW:{READWRITE_PORT}")
    def setValues(self, address, values):
        # Force conversion to integer to properly align incoming socket keys with parsed config keys
        display_address = int(address) - 1

        # Dynamic mapping write interception via pure global dict maps
        if display_address in WRITE_CONVERSIONS:
            current_value = values[0]
            try:
                lookup_val = int(current_value)
                if lookup_val in WRITE_CONVERSIONS[display_address]:
                    converted_value = WRITE_CONVERSIONS[display_address][lookup_val]
                    decode_and_log("WRITE_CONVERTED", display_address, current_value, tag=f"RW:{READWRITE_PORT}")
                    values[0] = converted_value
            except (ValueError, TypeError):
                pass

        if display_address in FORBIDDEN_REGISTERS:
            decode_and_log("WRITE_BLOCKED", display_address, values, tag=f"RW:{READWRITE_PORT}")
            return

        self.link.write_registers(address, values, tag=f"RW:{READWRITE_PORT}")


link = InverterLink()
readonly_context = ModbusServerContext(slaves={UNIT_ID: ModbusSlaveContext(hr=ReadOnlyDataBlock(link))}, single=False)
readwrite_context = ModbusServerContext(slaves={UNIT_ID: ModbusSlaveContext(hr=ReadWriteDataBlock(link))}, single=False)

async def main():
    print("\n==============================================")
    print(f" Solinteg Modbus Broker v{VERSION}")
    print(f" Read-only listener:  port {READONLY_PORT}")
    print(f" Read/write listener: port {READWRITE_PORT}")
    print(f" Forwarding to inverter: {INVERTER_IP}:{INVERTER_PORT} (UNIT ID: {UNIT_ID})")
    print(f" Canonical register map: {SOLINTEG_MODBUS_MAP_PATH}")
    print(f" Canonical mapped registers: {len(REGISTER_METADATA)}")
    print(f" Upstream request timeout: {UPSTREAM_TIMEOUT_SECONDS:.1f} s")
    print(" Reconnect backoff: immediate -> 0.1 -> 0.2 -> 0.4 -> 0.8 -> 1.6 -> 3.2 -> 5.0 s")
    print(f" Backoff reset after healthy connection: {RECONNECT_BACKOFF_RESET_SECONDS:.0f} s")
    print("==============================================")

    # Co-exist config monitoring loops and server context tasks concurrently
    await asyncio.gather(
        watch_rules_config(),
        StartAsyncTcpServer(context=readonly_context, address=(BROKER_IP, READONLY_PORT)),
        StartAsyncTcpServer(context=readwrite_context, address=(BROKER_IP, READWRITE_PORT)),
    )

if __name__ == "__main__":
    asyncio.run(main())
