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

# Defaults keep an existing configuration file working after an upgrade. The
# installer deliberately preserves /etc/default/solinteg-cloud-simulator.
TECH_ADDRESS="${TECH_ADDRESS:-8.209.105.201}"
TECH_LISTEN_PORT="${TECH_LISTEN_PORT:-5744}"
REMOTE_PORT=5743

required_variables=(
    LAN_INTERFACE
    INVERTER_ADDRESS
    CLOUD_ADDRESS
    LISTEN_PORT
    TECH_ADDRESS
    TECH_LISTEN_PORT
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

if [[ ! "$TECH_LISTEN_PORT" =~ ^[0-9]+$ ]] ||
   (( TECH_LISTEN_PORT < 1 || TECH_LISTEN_PORT > 65535 )); then
    echo "Invalid TECH_LISTEN_PORT: $TECH_LISTEN_PORT" >&2
    exit 1
fi

if [[ "$TECH_LISTEN_PORT" == "$LISTEN_PORT" ]]; then
    echo "TECH_LISTEN_PORT must differ from LISTEN_PORT" >&2
    exit 1
fi

IPTABLES_NFT="${IPTABLES_NFT:-$(command -v iptables-nft || true)}"
if [[ -z "$IPTABLES_NFT" || ! -x "$IPTABLES_NFT" ]]; then
    echo "iptables-nft was not found" >&2
    exit 1
fi

inverter_cidr="${INVERTER_ADDRESS%/32}/32"
cloud_cidr="${CLOUD_ADDRESS%/32}/32"
tech_cidr="${TECH_ADDRESS%/32}/32"

cloud_redirect_rule=(
    -i "$LAN_INTERFACE"
    -s "$inverter_cidr"
    -d "$cloud_cidr"
    -p tcp
    --dport "$REMOTE_PORT"
    -m comment
    --comment SOLINTEG_SIMULATOR
    -j REDIRECT
    --to-ports "$LISTEN_PORT"
)

tech_redirect_rule=(
    -i "$LAN_INTERFACE"
    -s "$inverter_cidr"
    -d "$tech_cidr"
    -p tcp
    --dport "$REMOTE_PORT"
    -m comment
    --comment SOLINTEG_SIMULATOR_TECH
    -j REDIRECT
    --to-ports "$TECH_LISTEN_PORT"
)

remove_rule()
{
    while "$IPTABLES_NFT" -w 5 -t nat -C PREROUTING "$@" 2>/dev/null; do
        "$IPTABLES_NFT" -w 5 -t nat -D PREROUTING "$@"
    done
}

remove_managed_rules()
{
    remove_rule "${cloud_redirect_rule[@]}"
    remove_rule "${tech_redirect_rule[@]}"
}

case "${1:-}" in
    add)
        remove_managed_rules
        "$IPTABLES_NFT" -w 5 -t nat -I PREROUTING 1 \
            "${cloud_redirect_rule[@]}"
        "$IPTABLES_NFT" -w 5 -t nat -I PREROUTING 2 \
            "${tech_redirect_rule[@]}"
        ;;
    remove)
        remove_managed_rules
        ;;
    status)
        status=0
        if "$IPTABLES_NFT" -w 5 -t nat -C PREROUTING \
            "${cloud_redirect_rule[@]}" 2>/dev/null; then
            echo "Solinteg cloud redirect is installed: $CLOUD_ADDRESS:$REMOTE_PORT -> local $LISTEN_PORT"
        else
            echo "Solinteg cloud redirect is not installed"
            status=1
        fi
        if "$IPTABLES_NFT" -w 5 -t nat -C PREROUTING \
            "${tech_redirect_rule[@]}" 2>/dev/null; then
            echo "Solinteg tech redirect is installed: $TECH_ADDRESS:$REMOTE_PORT -> local $TECH_LISTEN_PORT"
        else
            echo "Solinteg tech redirect is not installed"
            status=1
        fi
        if (( status != 0 )); then
            exit 1
        fi
        ;;
    *)
        echo "Usage: $0 {add|remove|status}" >&2
        exit 2
        ;;
esac
