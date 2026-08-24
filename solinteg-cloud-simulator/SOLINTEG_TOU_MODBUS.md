<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Rickard Dahlstedt -->

# Writing Solinteg Intelligent/ToU schedules through Modbus

This document describes an experimentally discovered Modbus interface for
loading the 24-slot-per-bank **Intelligent/Time of Use (ToU)** schedule used by
the Solinteg app. The interface was reconstructed from controlled cloud writes
and genuine inverter replies. A complete captured upload consisted of 51
writes, and the inverter returned success for every one:

1. select ToU working mode;
2. write all 24 records in schedule bank 1;
3. commit bank 1;
4. write all 24 records in schedule bank 2;
5. commit bank 2.

The complete sequence was captured through the communication module's `01:10`
cloud-write channel, which carries a target holding-register range and its
16-bit values and produced genuine inverter write-result frames. Individual
raw Modbus access to these registers has also been tested, but the complete
51-write sequence has not yet been independently replayed end-to-end through
a general-purpose Modbus client. The direct example below is therefore a
faithful function-16 reconstruction of the proven cloud sequence, with that
remaining validation boundary stated explicitly.

This is not an official Solinteg register definition. It has been verified on
one installation and may be model- or firmware-dependent. A bad schedule can
command grid charging, battery discharge, or export at the wrong time or
power. Validate every raw value, stay within the limits of the inverter,
battery, grid connection, and local regulations, and keep a tested way to
return the inverter to a safe working mode.

## Important distinctions

- This guide concerns the newer Intelligent/ToU staging interface at
  `53070–53084`.
- The older six-period ToU table at `53006–53048` is a different interface and
  must not be mixed with this one.
- The schedule contains 24 **record positions** per bank. A position is not an
  hour of the day: every enabled record contains its own start and stop time.
- The app exposes separate **Today** and **Tomorrow** schedules. Their captured
  bank values are `1` for Today and `2` for Tomorrow.
- `53071–53084` is a write staging window, not a directly readable 24-record
  table.

The tested Ethernet communication module uses Modbus unit ID `255`. The value
`247`, sometimes mentioned in third-party material and observed in another
configuration register, is not the correct unit ID for this interface. A
direct RS485 connection or a different communications module may behave
differently.

All addresses below are raw Modbus PDU addresses as used by `pymodbus`, for
example `address=53071`. Software that displays one-based register numbers may
show an address one higher. Do not add a `40001` holding-register prefix.

## Register map

### Working mode and commit strobe

| Address | Purpose | Confirmed value |
|---:|---|---:|
| `50000` | Inverter working-mode selection | `0x0400` (`1024`) = Intelligent/ToU |
| `53070` | Commit/apply the bank just staged | write `1` after all 24 records |

The captured cloud transaction selected `50000 = 0x0400` before sending any
schedule records. This exact order is known to work, but it also means that an
old ToU schedule may become active while the replacement is still being
uploaded. Perform the operation under conditions where an unexpected charge,
discharge, or export command cannot create a hazard.

Register `53070` behaved as a command strobe. Its observed readback was
`0xFFFF`, not the last written value. Do not treat that readback as failure.

### One staged slot: registers `53071–53084`

Write all 14 words together with Modbus function 16, **Write Multiple
Registers**:

| Address | Offset | Meaning | Encoding |
|---:|---:|---|---|
| `53071` | 0 | Schedule day | `1` = Today, `2` = Tomorrow |
| `53072` | 1 | Slot index | zero-based `0–23` |
| `53073` | 2 | Enabled | `1` enabled, `0` disabled |
| `53074` | 3 | Start time | packed `0xHHMM` |
| `53075` | 4 | Stop time | packed `0xHHMM` |
| `53076` | 5 | ToU action/mode | see the mode table below |
| `53077` | 6 | Mode option/source/priority | `0` in confirmed captures; exact semantics incomplete |
| `53078` | 7 | Maximum AC power / app “power to grid” field | watts, unscaled |
| `53079` | 8 | Charge or discharge power | `0.1 kW` per count |
| `53080` | 9 | Target or minimum SOC | `0.1%` per count |
| `53081` | 10 | Unknown/reserved | `0` in all captures |
| `53082` | 11 | Unknown/reserved | `0` in all captures |
| `53083` | 12 | Unknown/reserved | `0` in all captures |
| `53084` | 13 | Unknown/reserved | `0` in all captures |

Time is not decimal HHMM. The hour is stored in the high byte and the minute
in the low byte:

```text
encoded_time = (hour << 8) | minute
```

Examples:

| Time | Hex | Decimal |
|---:|---:|---:|
| `00:00` | `0x0000` | `0` |
| `06:30` | `0x061E` | `1566` |
| `13:00` | `0x0D00` | `3328` |
| `24:00` | `0x1800` | `6144` |

`24:00` was accepted as the stop time of the final record. The app offers
15-minute schedule boundaries; the byte layout can represent other minute
values, but their acceptance has not been systematically tested.

### Confirmed ToU action values

| `53076` value | Mode | Meaning of `53079` | Meaning of `53080` | Status |
|---:|---|---|---|---|
| `0x0401` (`1025`) | General | unused/zero | unused/zero | Confirmed |
| `0x0402` (`1026`) | Battery Charge | charge limit, `0.1 kW` | target SOC, `0.1%` | Confirmed |
| `0x0403` (`1027`) | PV Charging | charge limit, `0.1 kW` | target SOC, `0.1%` | Confirmed |
| `0x0404` (`1028`) | Peak Shifting | unknown | unknown | Inferred legacy mode; not captured |
| `0x0405` (`1029`) | Feed-In | not yet resolved | not yet resolved | Mode confirmed; both captured SOC controls were zero |
| `0x0406` (`1030`) | Battery Discharge | discharge limit, `0.1 kW` | minimum/floor SOC, `0.1%` | Confirmed |

The `0x04xx` values are ToU-specific slot actions. They are not interchangeable
with the inverter-wide mode values in `50000`. For example, `0x0402` means
Battery Charge inside a ToU record, while `0x0102` in `50000` selects the
inverter's Economic working mode.

For `53078`, a raw value of `4000` represents 4.0 kW. For `53079`, a raw value
of `15` represents 1.5 kW. For `53080`, a raw value of `600` represents 60.0%.

Only raw value `0` has been capture-confirmed at `53077`; one controlled test
described that selection as PV priority. Do not assume other values until they
have been captured and verified.

The Feed-In app exposes two SOC settings, but the identifying capture used
zero for both. Consequently, their register locations and scaling are still
unknown. Do not use non-zero Feed-In SOC controls through this interface until
that mapping is confirmed.

## Required upload sequence

Use a single Modbus writer and serialize every request. Do not edit the same
schedule from the cloud app during an upload. If a Modbus broker is already in
use, write through that broker instead of opening a second connection directly
to the inverter.

1. Build a complete list of 24 records for bank 1, **Today**.
2. Build a complete list of 24 records for bank 2, **Tomorrow**. Upload both
   days so a stale Tomorrow schedule does not become active at midnight.
3. Validate that every enabled period has sensible times and limits, periods
   do not overlap, and the intended day is covered.
4. Write `50000 = 0x0400` using function 16, even though it is one word.
5. Wait for a successful Modbus response.
6. For bank 1, write slots `0` through `23` in order. Each operation must be
   one function-16 write of all 14 words at `53071–53084`.
7. Explicitly clear unused positions. For a disabled slot, retain its bank and
   index, set `53073 = 0`, and write zero to `53074–53084`.
8. After slot 23 succeeds, write `53070 = 1` with function 16 and wait for its
   response.
9. Repeat steps 6–8 for bank 2.
10. Confirm that `50000` reads as `1024`, then monitor the inverter's actual
    mode, power, SOC limits, and grid exchange.

Do not commit a partially written bank after an error. Stop, correct the
problem, and upload all 24 records in that bank again before writing the
commit strobe.

The captured cloud implementation waited for each inverter response and sent
roughly one record every 0.3–1.0 seconds. It is not known whether that spacing
is required. A conservative direct implementation should wait for every reply
and may add a short delay; it must never pipeline or overlap writes.

## Dry-run-first Python/pymodbus example

The example is intentionally in dry-run mode and uses generic demonstration
limits. Review every value for the actual installation before setting
`DRY_RUN = False`. It uses function 16 for the single-word mode and commit
writes as well as for each 14-word slot record.

This is documentation, not a claim that the register interface is safe on
every Solinteg model or firmware.

```python
import time
from pymodbus.client import ModbusTcpClient

HOST = "127.0.0.1"       # Example: local Modbus broker
PORT = 502
UNIT_ID = 255
DRY_RUN = True           # Change only after reviewing the generated records
WRITE_DELAY = 0.25

TOU_MODE = 0x0400
GENERAL = 0x0401
BATTERY_CHARGE = 0x0402
PV_CHARGING = 0x0403
PEAK_SHIFTING_UNCONFIRMED = 0x0404
FEED_IN = 0x0405
BATTERY_DISCHARGE = 0x0406


def packed_time(hour, minute):
    if hour == 24 and minute == 0:
        return 0x1800
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"invalid time {hour:02d}:{minute:02d}")
    return (hour << 8) | minute


def enabled_slot(bank, index, start, stop, mode, max_ac_w,
                 power_tenths_kw=0, soc_tenths_pct=0, option=0):
    return [
        bank,
        index,
        1,
        packed_time(*start),
        packed_time(*stop),
        mode,
        option,
        max_ac_w,
        power_tenths_kw,
        soc_tenths_pct,
        0, 0, 0, 0,
    ]


def disabled_slot(bank, index):
    return [bank, index, 0] + [0] * 11


def complete_bank(bank, enabled_records):
    if any(len(record) != 14 for record in enabled_records):
        raise ValueError("every enabled slot must contain 14 words")
    if any(record[0] != bank for record in enabled_records):
        raise ValueError(f"all records must belong to bank {bank}")
    records = {record[1]: record for record in enabled_records}
    if len(records) != len(enabled_records):
        raise ValueError("duplicate slot index")
    if any(index not in range(24) for index in records):
        raise ValueError("slot indexes must be 0..23")
    return [records.get(index, disabled_slot(bank, index)) for index in range(24)]


# Generic example only: General -> Battery Charge -> General.
# Raw power and SOC values must be chosen for the real installation.
today_template = [
    enabled_slot(1, 0, (0, 0), (6, 0), GENERAL,
                 max_ac_w=4000),
    enabled_slot(1, 1, (6, 0), (8, 0), BATTERY_CHARGE,
                 max_ac_w=4000, power_tenths_kw=15,
                 soc_tenths_pct=600, option=0),
    enabled_slot(1, 2, (8, 0), (24, 0), GENERAL,
                 max_ac_w=4000),
]

tomorrow_template = [
    [2 if word_index == 0 else word for word_index, word in enumerate(record)]
    for record in today_template
]

today = complete_bank(1, today_template)
tomorrow = complete_bank(2, tomorrow_template)


def write_words(client, address, values):
    print(f"FC16 address={address} count={len(values)} values={values}")
    if DRY_RUN:
        return
    result = client.write_registers(
        address=address,
        values=values,
        slave=UNIT_ID,
    )
    if result.isError():
        raise RuntimeError(f"write failed at {address}: {result}")
    time.sleep(WRITE_DELAY)


client = None
try:
    if not DRY_RUN:
        client = ModbusTcpClient(HOST, port=PORT, timeout=10)
        if not client.connect():
            raise RuntimeError("could not connect to Modbus TCP endpoint")

    write_words(client, 50000, [TOU_MODE])

    for day in (today, tomorrow):
        for record in day:
            if len(record) != 14:
                raise ValueError("every slot record must contain 14 words")
            write_words(client, 53071, record)
        write_words(client, 53070, [1])
finally:
    if client is not None:
        client.close()
```

Older or newer `pymodbus` releases may name the unit-ID keyword differently.
The example uses `slave=255`, matching the version used during this
investigation. The important on-wire unit ID is `255`.

## Readback and verification limitations

Ordinary Modbus reads do not currently provide schedule readback:

- After a complete upload, `53071–53084` contained only the last staged
  record: bank 2, slot 23, disabled.
- Writing a desired slot number to `53072` and reading the window again did
  not load that stored slot.
- `53070` read as `0xFFFF` after the commit.
- Scanning the nearby `53000–53069` area found only unrelated legacy ToU
  fields, zeroes, or `0xFFFF`; it did not expose the Intelligent schedule.

The persistent schedule may live inside the communication module or require
an as-yet unknown query command. Until such a command is identified, retain
the source schedule used for each upload and verify the result from actual
inverter operation or the vendor UI. Reading the staging window is not a
backup.

## Known unknowns

The following should remain explicitly marked as unresolved:

- a Modbus command for reading stored schedule records back;
- the non-zero values and full semantics of `53077`;
- the two Feed-In SOC fields;
- the fields used by Peak Shifting (`0x0404`), whose mode assignment is
  inferred from an older app version rather than captured;
- whether every model and firmware accepts this interface;
- whether spacing between writes is required beyond strict request/response
  serialization.

## Disclaimer

Use this information entirely at your own risk. It is provided **as is**,
without warranty of correctness, safety, compatibility, or fitness for any
purpose. You are responsible for validating the register map and limits on
your own equipment, monitoring the result, and maintaining a safe rollback
method. The author and contributors are not liable for equipment damage,
unsafe operation, battery degradation, grid-code violations, energy costs,
data loss, loss of cloud access, warranty consequences, or any other loss or
injury arising from its use.

This work is independent and is not affiliated with or endorsed by Solinteg.
