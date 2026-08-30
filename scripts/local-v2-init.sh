#!/bin/sh
set -eu

state_dir="${LOCAL_V2_STATE_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/klikk-v2-local}"
runtime_env="${state_dir}/runtime.env"

mkdir -p "${state_dir}"
chmod 700 "${state_dir}"

if [ -e "${runtime_env}" ]; then
    echo "Local V2 runtime environment already exists at ${runtime_env}."
    exit 0
fi

umask 077
db_password="$(openssl rand -hex 24)"
django_secret="$(openssl rand -hex 48)"
synthetic_password="$(openssl rand -base64 24 | tr -d '\n')"

{
    printf 'COMPOSE_PROJECT_NAME=klikk-v2-local\n'
    printf 'DB_NAME=klikk_v2_local\n'
    printf 'DB_USER=klikk_v2_local\n'
    printf 'DB_PASSWORD=%s\n' "${db_password}"
    printf 'LOCAL_V2_DJANGO_SECRET_KEY=%s\n' "${django_secret}"
    printf 'LOCAL_V2_SYNTHETIC_USERNAME=local-v2-reader\n'
    printf 'LOCAL_V2_SYNTHETIC_PASSWORD=%s\n' "${synthetic_password}"
} > "${runtime_env}"
chmod 600 "${runtime_env}"

echo "Created restricted local runtime configuration at ${runtime_env}."
echo "No value was written inside the Git worktree."

