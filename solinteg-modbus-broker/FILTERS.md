# Runtime filters and overrides

The broker watches `modbus_rules.json` and reloads it every 10 seconds. With the systemd installation the live file is `/etc/solinteg-modbus-broker/modbus_rules.json`.

The file contains three independent sections. Register numbers are the Solinteg register numbers shown in broker logs and in the canonical `solinteg_modbus_map.py` file.

## `FORBIDDEN_REGISTERS`

Blocks writes on the read/write listener before they reach the inverter.

```json
{
  "FORBIDDEN_REGISTERS": [50000, 50208],
  "WRITE_CONVERSIONS": {},
  "READ_OVERRIDES": {}
}
```

A blocked write is logged as `WRITE_BLOCKED`. The read-only listener on TCP/502 blocks all writes regardless of this list.

## `WRITE_CONVERSIONS`

Rewrites selected incoming register values before forwarding them upstream.

```json
{
  "FORBIDDEN_REGISTERS": [],
  "WRITE_CONVERSIONS": {
    "52503": {
      "100": 50
    }
  },
  "READ_OVERRIDES": {}
}
```

Here an incoming raw value `100` for register `52503` is forwarded as raw value `50`. The broker logs `WRITE_CONVERTED` and then, after the inverter acknowledges it, `WRITE`.

Keys are JSON strings because JSON object keys are strings; the broker converts register numbers and value keys to integers when the file is loaded.

## `READ_OVERRIDES`

Returns a locally substituted value to downstream clients after performing the live inverter read.

```json
{
  "FORBIDDEN_REGISTERS": [],
  "WRITE_CONVERSIONS": {},
  "READ_OVERRIDES": {
    "50208": {
      "value": 0,
      "target": "RW"
    }
  }
}
```

`target` may be:

- `RO` — only TCP/502 clients see the override.
- `RW` — only TCP/503 clients see the override.
- `BOTH` — both listeners see it. This is also the default if `target` is omitted.

The override value is the raw Modbus value, not the scaled engineering value printed in logs. For a mapped 32-bit register, set the override on the first register address and provide the complete 32-bit raw integer; the broker splits it into the two 16-bit words.

The broker logs the replacement as `READ_CONVERTED`, followed by the resulting `READ` rows.

## Hot reload and failure behavior

Changes are detected from the file modification time every 10 seconds. A malformed JSON update is ignored and the previously loaded rules remain active. If the file disappears, the broker clears all dynamic filters and continues with empty defaults.

Rules are policy transformations only. Real inverter read/write failures are never replaced by cached or fake success data: upstream failures are returned to the Modbus client as server failures and logged as `READ_FAIL` or `WRITE_FAIL`.
