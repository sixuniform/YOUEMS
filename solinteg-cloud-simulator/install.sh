#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Rickard Dahlstedt

set -euo pipefail

if (( EUID != 0 )); then
    echo "Run this installer as root: sudo ./install.sh" >&2
    exit 1
fi

source_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
install_directory=/opt/solinteg-cloud-simulator
config_file=/etc/default/solinteg-cloud-simulator
systemd_directory=/etc/systemd/system

install -d -m 0755 "$install_directory"
install -m 0755 \
    "$source_directory/solinteg-cloud-simulator.py" \
    "$install_directory/solinteg-cloud-simulator.py"
install -m 0755 \
    "$source_directory/solinteg-cloud-simulator-firewall.sh" \
    "$install_directory/solinteg-cloud-simulator-firewall.sh"
install -m 0644 \
    "$source_directory/solinteg-cloud-simulator.service" \
    "$systemd_directory/solinteg-cloud-simulator.service"
install -m 0644 \
    "$source_directory/solinteg-cloud-simulator-firewall.service" \
    "$systemd_directory/solinteg-cloud-simulator-firewall.service"

if [[ ! -e "$config_file" ]]; then
    install -m 0644 \
        "$source_directory/solinteg-cloud-simulator.conf" \
        "$config_file"
    echo "Installed configuration: $config_file"
else
    echo "Preserved existing configuration: $config_file"
fi

/usr/bin/python3 "$install_directory/solinteg-cloud-simulator.py" --self-test
systemctl daemon-reload
systemctl enable solinteg-cloud-simulator.service
systemctl restart solinteg-cloud-simulator.service

echo
echo "solinteg-cloud-simulator is installed and running."
echo "Configuration: $config_file"
echo "Logs: journalctl -u solinteg-cloud-simulator -f"
