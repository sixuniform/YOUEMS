#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Rickard Dahlstedt

set -euo pipefail

CONFIG_FILE="${CONFIG_FILE:-/etc/default/solinteg-cloud-simulator}"

if [[ ! -r "$CONFIG_FILE" ]]; then
    echo "Cannot read configuration: $CONFIG_FILE" >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"

required_variables=(
    LAN_INTERFACE
    INVERTER_ADDRESS
    CLOUD_ADDRESS
    LISTEN_PORT
)

for variable_name in "${required_variables[@]}"; do
    if [[ -z "${!variable_name:-}" ]]; then
        echo "Missing $variable_name in $CONFIG_FILE" >&2
        exit 1
    fi
done

if [[ ! "$LISTEN_PORT" =~ ^[0-9]+$ ]] ||
   (( LISTEN_PORT < 1 || LISTEN_PORT > 65535 )); then
    echo "Invalid LISTEN_PORT: $LISTEN_PORT" >&2
    exit 1
fi

IPTABLES_NFT="${IPTABLES_NFT:-$(command -v iptables-nft || true)}"
if [[ -z "$IPTABLES_NFT" || ! -x "$IPTABLES_NFT" ]]; then
    echo "iptables-nft was not found" >&2
    exit 1
fi

inverter_cidr="${INVERTER_ADDRESS%/32}/32"
cloud_cidr="${CLOUD_ADDRESS%/32}/32"

redirect_rule=(
    -i "$LAN_INTERFACE"
    -s "$inverter_cidr"
    -d "$cloud_cidr"
    -p tcp
    --dport "$LISTEN_PORT"
    -m comment
    --comment SOLINTEG_SIMULATOR
    -j REDIRECT
    --to-ports "$LISTEN_PORT"
)

remove_managed_rules()
{
    while "$IPTABLES_NFT" -w 5 -t nat -C PREROUTING \
        "${redirect_rule[@]}" 2>/dev/null; do
        "$IPTABLES_NFT" -w 5 -t nat -D PREROUTING \
            "${redirect_rule[@]}"
    done
}

case "${1:-}" in
    add)
        remove_managed_rules
        "$IPTABLES_NFT" -w 5 -t nat -I PREROUTING 1 \
            "${redirect_rule[@]}"
        ;;
    remove)
        remove_managed_rules
        ;;
    status)
        if "$IPTABLES_NFT" -w 5 -t nat -C PREROUTING \
            "${redirect_rule[@]}" 2>/dev/null; then
            echo "Solinteg simulator redirect is installed"
        else
            echo "Solinteg simulator redirect is not installed"
            exit 1
        fi
        ;;
    *)
        echo "Usage: $0 {add|remove|status}" >&2
        exit 2
        ;;
esac
