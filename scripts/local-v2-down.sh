#!/bin/sh
set -eu

repo_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
state_dir="${LOCAL_V2_STATE_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/klikk-v2-local}"
runtime_env="${state_dir}/runtime.env"

case "${state_dir}" in
    */klikk-v2-local) ;;
    *)
        echo "Refusing cleanup for unexpected state directory: ${state_dir}" >&2
        exit 1
        ;;
esac

if [ ! -r "${runtime_env}" ]; then
    echo "Missing ${runtime_env}; refusing an ambiguous Compose cleanup." >&2
    exit 1
fi

docker compose \
    --project-name klikk-v2-local \
    --env-file "${runtime_env}" \
    --file "${repo_dir}/docker-compose.local-v2.yml" \
    --profile local-v2 \
    down --volumes --remove-orphans

if [ "${1:-}" = "--purge-state" ]; then
    rm -f -- "${runtime_env}"
    rmdir -- "${state_dir}" 2>/dev/null || true
    echo "Removed the restricted local runtime configuration."
fi

