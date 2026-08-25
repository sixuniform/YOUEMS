# Solinteg Modbus Broker

A small Modbus/TCP broker/proxy for Solinteg hybrid inverters. It lets multiple downstream applications use one serialized upstream inverter connection, provides separate read-only and read/write listeners, adds human-readable register logging, and supports hot-reloaded write filters and read overrides.

The broker was built for the YOUEMS/Home Assistant environment but is intentionally usable as a stand-alone Linux service.

## What it does

Default topology:

```text
read-only clients  ---> TCP 502 --\
                               Solinteg Modbus Broker ---> inverter TCP 502
read/write clients ---> TCP 503 --/
```

Defaults:

- Broker bind address: `0.0.0.0`
- Read-only listener: TCP `502`
- Read/write listener: TCP `503`
- Inverter: `192.168.10.152:502`
- Solinteg unit/device ID: `255`
- Upstream timeout: `10 s`

All upstream Modbus operations are serialized through one shared connection. Single-register writes are forwarded with FC06; multi-register writes use FC16. The read-only listener silently blocks writes at the broker.

## Register map: one canonical source

There is intentionally no second maintained register table in this directory.

The canonical map is:

```text
YOUEMS/solinteg-cloud-simulator/solinteg_modbus_map.py
```

Inside the Git repository, `solinteg-modbus-broker/solinteg_modbus_map.py` is a **Git symlink** to that canonical file. A full clone therefore uses exactly one real source file.

For a stand-alone installation, `install.sh` copies the symlink target when a full YOUEMS checkout is available. If only the broker files were downloaded, it fetches the same canonical map from the `main` branch and installs it locally beside the broker. That installed copy is runtime material, not a second maintained GitHub mapping file.

The broker also accepts `SOLINTEG_MODBUS_MAP_PATH=/path/to/solinteg_modbus_map.py` and checks its own/current directory for the map.

## Files

| File | Purpose |
| --- | --- |
| `solinteg-modbus-broker.py` | Broker/proxy itself |
| `solinteg_modbus_map.py` | Git symlink to the canonical map in `solinteg-cloud-simulator` |
| `modbus_rules.json` | Empty/safe default runtime filter set |
| `modbus_rules.example.json` | Examples of blocking, write conversion and read overrides |
| `FILTERS.md` | Detailed rule syntax and behavior |
| `solinteg-modbus-broker.conf` | Example `/etc/default/` environment configuration |
| `solinteg-modbus-broker.service` | Hardened systemd unit |
| `install.sh` | Stand-alone/systemd installer |
| `requirements.txt` | Python dependency pin used by the installer |

## Quick stand-alone install

On Debian/Ubuntu/Raspberry Pi OS, make sure Python venv support and either `curl` or `wget` are installed:

```bash
sudo apt update
sudo apt install python3 python3-venv curl
```

Download the files in this directory or clone YOUEMS, then run:

```bash
cd solinteg-modbus-broker
sudo ./install.sh
```

The installer creates:

```text
/opt/solinteg-modbus-broker/
    solinteg-modbus-broker.py
    solinteg_modbus_map.py
    requirements.txt
    venv/

/etc/default/solinteg-modbus-broker
/etc/solinteg-modbus-broker/modbus_rules.json
/etc/systemd/system/solinteg-modbus-broker.service
```

It preserves existing `/etc/default/solinteg-modbus-broker` and rules files on subsequent installs.

Follow logs with:

```bash
journalctl -u solinteg-modbus-broker -f
```

Check status with:

```bash
systemctl status solinteg-modbus-broker
```

Restart after changing `/etc/default/solinteg-modbus-broker`:

```bash
sudo systemctl restart solinteg-modbus-broker
```

Changes to `modbus_rules.json` do **not** require a restart; they are hot-reloaded.

## Configuration

`solinteg-modbus-broker.conf` documents all systemd environment settings. The main ones are:

```text
BROKER_IP=0.0.0.0
READONLY_PORT=502
READWRITE_PORT=503
INVERTER_IP=192.168.10.152
INVERTER_PORT=502
UNIT_ID=255
UPSTREAM_TIMEOUT_SECONDS=10.0
```

Reconnect settings:

```text
RECONNECT_BACKOFF_INITIAL_SECONDS=0.1
RECONNECT_BACKOFF_MAX_SECONDS=5.0
RECONNECT_BACKOFF_RESET_SECONDS=60.0
```

Recovery starts with an immediate reconnect attempt, then backs off approximately `0.1 -> 0.2 -> 0.4 -> 0.8 -> 1.6 -> 3.2 -> 5.0 s`, capped at 5 seconds. The recovery history resets only after the upstream connection has remained healthy for 60 seconds.

## Runtime rules / filters

The default rules file is deliberately empty:

```json
{
  "FORBIDDEN_REGISTERS": [],
  "WRITE_CONVERSIONS": {},
  "READ_OVERRIDES": {}
}
```

The three filter types are:

- `FORBIDDEN_REGISTERS` — block selected writes on the RW listener.
- `WRITE_CONVERSIONS` — rewrite selected raw values before sending them to the inverter.
- `READ_OVERRIDES` — replace selected downstream read values, optionally only on RO or RW.

See `FILTERS.md` and `modbus_rules.example.json` for complete examples.

## Logging

Each mapped register gets a human-readable row with the upstream transaction duration immediately after the timestamp:

```text
2026-08-23 15:57:28[0.02s],RW:503,WRITE,Reg:50209 [EMS BattCtrl Max Grid Import],Raw:[0] -> 0.0 kW
2026-08-23 15:57:29[0.01s],RW:503,READ,Reg:11028 [PV Input Power Total],Raw:[0, 6236] -> 6236 W
```

A slow but successful inverter response remains a normal `READ`/`WRITE`, for example `[6.42s]`. Upstream failures are concise and do not print full pymodbus tracebacks:

```text
2026-08-23 15:57:40[10.01s],RW:503,READ_FAIL,Reg:11028 [PV Input Power Total],Error:...
```

The broker does not return cached/zero data after a failed live read and does not report a failed upstream write as successful.

## Important behavior

- Both downstream ports share one mutex and one persistent upstream Modbus/TCP client. This prevents concurrent clients from colliding on the inverter connection.
- Solinteg inverters can occasionally take several seconds to answer one request while surrounding requests complete in tens of milliseconds. The default upstream timeout is therefore 10 seconds.
- After a real upstream failure the failed pymodbus client/socket is discarded. Reconnect attempts use exponential backoff.
- A connection that remains healthy for 60 seconds resets the backoff history.
- The broker uses Solinteg's unit ID `255` by default.
- Port 502 requires `CAP_NET_BIND_SERVICE`; the supplied systemd unit grants only that capability to a `DynamicUser` service.

## Manual run without systemd

Place `solinteg-modbus-broker.py` and the canonical `solinteg_modbus_map.py` in the same directory, install the dependency, and run:

```bash
python3 -m pip install pymodbus==3.6.9
python3 solinteg-modbus-broker.py
```

Environment variables can override the defaults, for example:

```bash
INVERTER_IP=192.168.1.50 READONLY_PORT=1502 READWRITE_PORT=1503 python3 solinteg-modbus-broker.py
```

Using ports above 1024 is convenient for an unprivileged manual test.

## License

Apache-2.0. See the SPDX headers in the source and the YOUEMS repository licensing files.
