#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Rickard Dahlstedt
"""Read-only probe for tentative Solinteg/M-TEC service registers.

The candidate meanings come from controlled observations reported by M-TEC
Energy Butler owners. They are not official Solinteg definitions. This tool
only calls read_holding_registers; it contains no Modbus write operation.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence


@dataclass(frozen=True)
class Probe:
    name: str
    address: int
    count: int
    purpose: str


# Keep multiword values and strings in a single Modbus request. Some Solinteg
# firmware rejects partial reads of packed or multi-register fields.
PROBES = (
    Probe("rtc_reference", 10100, 3, "documented inverter RTC reference"),
    Probe(
        "energy_reference",
        11020,
        4,
        "known total-generation energy and generation-hours references",
    ),
    Probe(
        "service_parameters",
        20000,
        16,
        "RTC, energy/hour duplicates, and opaque communication settings",
    ),
    Probe(
        "endpoint_strings",
        20016,
        60,
        "normal-cloud and technical-service endpoint strings",
    ),
    Probe(
        "service_tail",
        20076,
        24,
        "opaque service values and three credential-like ASCII fields",
    ),
    Probe(
        "network_reserved",
        20100,
        40,
        "unknown block reported as zero on one M-TEC firmware",
    ),
    Probe(
        "network_configuration",
        20140,
        11,
        "network-mode and IPv4 configuration candidates",
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read tentative Solinteg service/network registers without writing "
            "anything to the inverter."
        )
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--slave", type=int, default=255)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="pause between register blocks in seconds (default: 0.1)",
    )
    parser.add_argument(
        "--show-sensitive",
        action="store_true",
        help="print the three credential-like ASCII fields instead of redacting them",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not 0 <= args.slave <= 255:
        parser.error("--slave must be between 0 and 255")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    return args


def read_holding_registers(
    client: Any,
    address: int,
    count: int,
    slave: int,
) -> object:
    """Call the installed pymodbus version with its supported unit keyword."""

    method = client.read_holding_registers
    parameters = inspect.signature(method).parameters
    kwargs = {"address": address, "count": count}
    if "device_id" in parameters:
        kwargs["device_id"] = slave
    elif "slave" in parameters:
        kwargs["slave"] = slave
    elif "unit" in parameters:
        kwargs["unit"] = slave
    else:
        raise RuntimeError("unsupported pymodbus read_holding_registers signature")
    return method(**kwargs)


def dump_words(address: int, values: Sequence[int]) -> None:
    for offset in range(0, len(values), 4):
        words = values[offset:offset + 4]
        print(
            "  "
            + "  ".join(
                f"{address + offset + index}={value:5d}/0x{value:04X}"
                for index, value in enumerate(words)
            )
        )


def get_words(
    register_values: dict[int, int],
    address: int,
    count: int,
) -> Optional[tuple[int, ...]]:
    try:
        return tuple(register_values[address + offset] for offset in range(count))
    except KeyError:
        return None


def words_to_bytes(words: Sequence[int]) -> bytes:
    return b"".join(word.to_bytes(2, "big") for word in words)


def decode_ascii(words: Sequence[int]) -> str:
    raw = words_to_bytes(words).rstrip(b"\x00\xff ")
    return raw.decode("ascii", errors="replace")


def decode_rtc(words: Sequence[int]) -> str:
    raw = words_to_bytes(words)
    if len(raw) != 6:
        return raw.hex()
    year, month, day, hour, minute, second = raw
    return (
        f"20{year:02d}-{month:02d}-{day:02d} "
        f"{hour:02d}:{minute:02d}:{second:02d}"
    )


def decode_u32(words: Sequence[int]) -> int:
    return (words[0] << 16) | words[1]


def decode_u32_wordswapped(words: Sequence[int]) -> int:
    return (words[1] << 16) | words[0]


def decode_ipv4(words: Sequence[int]) -> str:
    return ".".join(str(value) for value in words_to_bytes(words))


def decode_ipv4_wordswapped(words: Sequence[int]) -> str:
    return decode_ipv4(tuple(reversed(words)))


def print_interpretations(
    registers: dict[int, int],
    slave: int,
    show_sensitive: bool,
) -> None:
    print("\n=== Tentative interpretations ===")
    print("Labels below are forum hypotheses unless explicitly called a reference.")

    rtc_reference = get_words(registers, 10100, 3)
    rtc_service = get_words(registers, 20000, 3)
    if rtc_reference is not None:
        print(f"10100..10102 known RTC reference: {decode_rtc(rtc_reference)}")
    if rtc_service is not None:
        print(f"20000..20002 service RTC candidate: {decode_rtc(rtc_service)}")
    if rtc_reference is not None and rtc_service is not None:
        print(f"  RTC blocks identical: {rtc_reference == rtc_service}")

    energy_reference = get_words(registers, 11020, 2)
    energy_candidate = get_words(registers, 20007, 2)
    if energy_reference is not None:
        reference_raw = decode_u32(energy_reference)
        print(
            f"11020..11021 known generation total: raw={reference_raw}, "
            f"scaled={reference_raw * 0.1:.1f} kWh"
        )
    if energy_candidate is not None:
        candidate_raw = decode_u32(energy_candidate)
        print(
            f"20007..20008 generation-total candidate: raw={candidate_raw}, "
            f"scaled={candidate_raw * 0.1:.1f} kWh, "
            f"word-swapped-raw={decode_u32_wordswapped(energy_candidate)}"
        )
    if energy_reference is not None and energy_candidate is not None:
        print(
            "  Raw energy values identical: "
            f"{decode_u32(energy_reference) == decode_u32(energy_candidate)}"
        )

    hours_reference = get_words(registers, 11022, 2)
    hours_candidate = get_words(registers, 20010, 1)
    if hours_reference is not None:
        print(
            "11022..11023 known generation hours: "
            f"{decode_u32(hours_reference)} h"
        )
    if hours_candidate is not None:
        print(f"20010 work-hours candidate: {hours_candidate[0]} h")

    serial_candidate = get_words(registers, 20011, 1)
    if serial_candidate is not None:
        value = serial_candidate[0]
        print(
            "20011 serial-format candidate (M-TEC speculation only): "
            f"{value} / 0x{value:04X} / bits={value:016b}"
        )

    communication_parameter = get_words(registers, 20012, 1)
    if communication_parameter is not None:
        value = communication_parameter[0]
        print(
            "20012 opaque communication parameter: "
            f"{value}; request unit={slave}; this is not the Modbus TCP unit ID"
        )

    for address, label in (
        (20016, "normal cloud endpoint"),
        (20046, "technical endpoint"),
    ):
        words = get_words(registers, address, 30)
        if words is not None:
            print(f"{address} {label}: {decode_ascii(words)!r}")

    for address in (20076, 20078, 20080, 20082, 20086):
        words = get_words(registers, address, 2)
        if words is not None:
            print(
                f"{address}..{address + 1} opaque U32 candidate: "
                f"BE={decode_u32(words)} swapped={decode_u32_wordswapped(words)}"
            )
    for address in (20084, 20085):
        words = get_words(registers, address, 1)
        if words is not None:
            print(f"{address} opaque U16 candidate: {words[0]} / 0x{words[0]:04X}")

    for address in (20088, 20092, 20096):
        words = get_words(registers, address, 4)
        if words is None:
            continue
        raw = words_to_bytes(words).rstrip(b"\x00\xff ")
        if show_sensitive:
            summary = repr(raw.decode("ascii", errors="replace"))
        else:
            digest = hashlib.sha256(raw).hexdigest()[:16]
            summary = f"<redacted: {len(raw)} byte(s), sha256-prefix={digest}>"
        print(f"{address} credential-like ASCII field: {summary}")

    reserved = get_words(registers, 20100, 40)
    if reserved is not None:
        nonzero = [
            (20100 + offset, value)
            for offset, value in enumerate(reserved)
            if value != 0
        ]
        print(
            "20100..20139 unknown network/service block: "
            f"{len(nonzero)} nonzero word(s)"
        )
        if nonzero:
            print(
                "  nonzero="
                + ", ".join(
                    f"{address}=0x{value:04X}" for address, value in nonzero
                )
            )

    network_mode = get_words(registers, 20140, 1)
    if network_mode is not None:
        print(
            "20140 network-mode candidate: "
            f"{network_mode[0]} / 0x{network_mode[0]:04X}"
        )
    for address, label in (
        (20141, "non-DHCP/static IP candidate"),
        (20143, "gateway candidate"),
        (20145, "network mask candidate"),
        (20147, "DNS server candidate"),
        (20149, "DHCP-provided IP candidate"),
    ):
        words = get_words(registers, address, 2)
        if words is not None:
            print(
                f"{address}..{address + 1} {label}: "
                f"BE={decode_ipv4(words)} word-swapped={decode_ipv4_wordswapped(words)}"
            )


def main() -> int:
    args = parse_args()
    try:
        from pymodbus.client import ModbusTcpClient
    except ImportError:
        print(
            "pymodbus is required; run this on the Modbus Broker host or "
            "install the matching pymodbus package",
            file=sys.stderr,
        )
        return 2
    print(
        f"Read-only Modbus probe: {args.host}:{args.port}, unit={args.slave}. "
        "No writes will be performed."
    )
    client = ModbusTcpClient(args.host, port=args.port, timeout=args.timeout)
    if not client.connect():
        print("Could not connect to Modbus TCP endpoint", file=sys.stderr)
        return 2

    registers: dict[int, int] = {}
    failures = 0
    try:
        for probe_number, probe in enumerate(PROBES):
            print(
                f"\n[{probe.name}] {probe.address}.."
                f"{probe.address + probe.count - 1}: {probe.purpose}"
            )
            try:
                result = read_holding_registers(
                    client,
                    probe.address,
                    probe.count,
                    args.slave,
                )
            except Exception as error:  # Preserve later probes after one failure.
                failures += 1
                print(f"  ERROR: {type(error).__name__}: {error}")
                continue
            if result.isError():
                failures += 1
                print(f"  MODBUS ERROR: {result}")
                continue
            values = tuple(int(value) for value in result.registers)
            if len(values) != probe.count:
                failures += 1
                print(
                    f"  ERROR: received {len(values)} register(s), "
                    f"expected {probe.count}"
                )
                continue
            dump_words(probe.address, values)
            registers.update(
                (probe.address + offset, value)
                for offset, value in enumerate(values)
            )
            if probe_number + 1 < len(PROBES) and args.delay:
                time.sleep(args.delay)
    finally:
        client.close()

    print_interpretations(registers, args.slave, args.show_sensitive)
    print(f"\nCompleted with {failures} failed block(s). No writes were performed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
