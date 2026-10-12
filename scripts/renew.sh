#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
stack_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$stack_dir"

# Some certificates may renew successfully even when another renewal fails.
# Capture failures explicitly so set -e cannot skip the validated reload.
renewal_status=0
docker compose --env-file .env -f compose.yml run -T --rm --no-deps certbot renew --quiet || renewal_status=$?

reload_status=0
"$script_dir/reload.sh" || reload_status=$?

if [ "$renewal_status" -ne 0 ]; then
    printf 'ERROR: Certbot renewal failed (exit %s). Inspect the renewal log.\n' "$renewal_status" >&2
fi
if [ "$reload_status" -ne 0 ]; then
    printf 'ERROR: Nginx validation or reload failed (exit %s).\n' "$reload_status" >&2
fi
if [ "$renewal_status" -ne 0 ] || [ "$reload_status" -ne 0 ]; then
    exit 1
fi
