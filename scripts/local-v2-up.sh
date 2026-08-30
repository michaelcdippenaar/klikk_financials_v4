#!/bin/sh
set -eu

repo_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
state_dir="${LOCAL_V2_STATE_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/klikk-v2-local}"
runtime_env="${state_dir}/runtime.env"

if [ ! -r "${runtime_env}" ]; then
    echo "Run scripts/local-v2-init.sh first." >&2
    exit 1
fi

docker compose \
    --project-name klikk-v2-local \
    --env-file "${runtime_env}" \
    --file "${repo_dir}/docker-compose.local-v2.yml" \
    --profile local-v2 \
    up --build --detach

echo "Local V2 proxy: http://127.0.0.1:18080"
echo "Direct backend health: http://127.0.0.1:18001/health/local-v2/"
echo "Synthetic username and generated password are in ${runtime_env}."

