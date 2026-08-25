#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Rickard Dahlstedt

set -euo pipefail

if (( EUID != 0 )); then
    echo "Run this installer as root: sudo ./install.sh" >&2
    exit 1
fi

source_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
install_directory=/opt/solinteg-modbus-broker
config_file=/etc/default/solinteg-modbus-broker
rules_directory=/etc/solinteg-modbus-broker
rules_file="$rules_directory/modbus_rules.json"
systemd_directory=/etc/systemd/system
map_file="$install_directory/solinteg_modbus_map.py"
map_url=https://raw.githubusercontent.com/sixuniform/YOUEMS/main/solinteg-cloud-simulator/solinteg_modbus_map.py

install -d -m 0755 "$install_directory"
install -d -m 0755 "$rules_directory"
install -m 0755 "$source_directory/solinteg-modbus-broker.py" "$install_directory/solinteg-modbus-broker.py"
install -m 0644 "$source_directory/requirements.txt" "$install_directory/requirements.txt"
install -m 0644 "$source_directory/solinteg-modbus-broker.service" "$systemd_directory/solinteg-modbus-broker.service"

if [[ ! -e "$config_file" ]]; then
    install -m 0644 "$source_directory/solinteg-modbus-broker.conf" "$config_file"
    echo "Installed configuration: $config_file"
else
    echo "Preserved existing configuration: $config_file"
fi

if [[ ! -e "$rules_file" ]]; then
    install -m 0644 "$source_directory/modbus_rules.json" "$rules_file"
    echo "Installed empty rules file: $rules_file"
else
    echo "Preserved existing rules: $rules_file"
fi

# Keep one canonical registry map in Git. In a full YOUEMS clone the broker
# directory contains a symlink to the simulator's canonical map, so use it.
# For a stand-alone broker download, fetch that same canonical source directly.
source_map="$source_directory/solinteg_modbus_map.py"
if [[ -f "$source_map" ]] && grep -q '^REGISTER_METADATA = {' "$source_map"; then
    install -m 0644 "$(readlink -f "$source_map")" "$map_file"
    echo "Installed canonical register map from the YOUEMS checkout/local file."
elif command -v curl >/dev/null 2>&1; then
    curl -fsSL "$map_url" -o "$map_file"
    chmod 0644 "$map_file"
    echo "Downloaded canonical register map from GitHub."
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$map_file" "$map_url"
    chmod 0644 "$map_file"
    echo "Downloaded canonical register map from GitHub."
else
    echo "Cannot obtain the canonical register map: install curl/wget or use a full YOUEMS clone." >&2
    exit 1
fi

if ! python3 -m venv "$install_directory/venv" 2>/dev/null; then
    echo "Could not create a Python venv. Install python3-venv and run this installer again." >&2
    exit 1
fi

"$install_directory/venv/bin/python" -m pip install --upgrade pip
"$install_directory/venv/bin/python" -m pip install -r "$install_directory/requirements.txt"

# Verify both the broker and shared map parse before touching the service.
"$install_directory/venv/bin/python" -m py_compile \
    "$install_directory/solinteg-modbus-broker.py" \
    "$map_file"

systemctl daemon-reload
systemctl enable solinteg-modbus-broker.service
systemctl restart solinteg-modbus-broker.service

echo
echo "solinteg-modbus-broker is installed and running."
echo "Configuration: $config_file"
echo "Rules:         $rules_file"
echo "Register map:  $map_file"
echo "Logs:          journalctl -u solinteg-modbus-broker -f"
