#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
stack_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
certificate_dir="$stack_dir/letsencrypt/conf"

if ! command -v openssl >/dev/null 2>&1; then
    printf 'ERROR: openssl is required for certificate checks.\n' >&2
    exit 1
fi
if [ ! -d "$certificate_dir" ] || [ ! -r "$certificate_dir" ] || [ ! -x "$certificate_dir" ]; then
    printf 'ERROR: Cannot read certificate storage: %s\n' "$certificate_dir" >&2
    exit 1
fi

live_dir="$certificate_dir/live"
if { [ -e "$live_dir" ] || [ -L "$live_dir" ]; } && { [ ! -d "$live_dir" ] || [ ! -r "$live_dir" ] || [ ! -x "$live_dir" ]; }; then
    printf 'ERROR: Cannot read certificate lineages: %s\n' "$live_dir" >&2
    exit 1
fi

status=0
count=0
for lineage in "$live_dir"/*; do
    if [ -L "$lineage" ] && [ ! -d "$lineage" ]; then
        printf 'ERROR: Broken certificate lineage: %s\n' "$lineage" >&2
        status=1
        count=$((count + 1))
        continue
    fi
    [ -d "$lineage" ] || continue
    count=$((count + 1))
    certificate="$lineage/fullchain.pem"

    if ! openssl x509 -in "$certificate" -noout >/dev/null 2>&1; then
        printf 'ERROR: Missing, unreadable, or invalid certificate: %s\n' "$certificate" >&2
        status=1
    elif ! openssl x509 -in "$certificate" -noout -checkend 1209600 >/dev/null 2>&1; then
        printf 'ERROR: Certificate is expired or expires within 14 days: %s\n' "$certificate" >&2
        openssl x509 -in "$certificate" -noout -enddate >&2
        status=1
    fi
done

if [ "$count" -eq 0 ]; then
    printf 'NOTICE: No managed certificates found; expected only before initial issuance.\n' >&2
fi
exit "$status"
