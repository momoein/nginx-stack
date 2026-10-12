#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
stack_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$stack_dir"

docker compose --env-file .env -f compose.yml exec -T nginx nginx -t
docker compose --env-file .env -f compose.yml exec -T nginx nginx -s reload
