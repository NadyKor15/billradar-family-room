#!/usr/bin/env bash
# BillRadar Family Room: local Switch 0.31.0 stand (bundled Mattermost).
# Usage: deploy/stand.sh init-env | up | down | ps | logs [svc] | wipe
# Everything runs under one compose project so it never touches another stack.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$HERE/switch-0.31.0/standalone-docker-compose.yml"
ENV_FILE="$HERE/.env"
PROJECT="${NADFLIP_PROJECT:-nadflip}"
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
# Bare-key variables in the upstream compose file are read from the shell, so
# the privacy-relevant ones are exported here as well as written to .env:
# no usage telemetry, no third-party avatar services seeing agent names.
export TELEMETRY_ENABLED=false THIRD_PARTY_AVATARS_ENABLED=false

dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" --profile collab --profile gateway "$@"; }

case "${1:-}" in
  init-env)
    if [ -e "$ENV_FILE" ]; then echo ".env exists, refusing to overwrite" >&2; exit 1; fi
    umask 077
    rnd() { openssl rand -hex 24; }
    cat > "$ENV_FILE" <<EOF
SWITCH_REGISTRY=ghcr.io
SWITCH_IMAGE_NAMESPACE=sandbox-quantum
SWITCH_VERSION=0.31.0
GATEWAY_HOST_PORT=${GATEWAY_HOST_PORT:-38030}
API_HOST_PORT=${API_HOST_PORT:-38000}
MATTERMOST_HOST_PORT=${MATTERMOST_HOST_PORT:-38065}
POSTGRES_HOST_PORT=${POSTGRES_HOST_PORT:-38432}
SWITCH_BIND_ADDR=127.0.0.1
DB_USER=switch_app
DB_PASSWORD=$(rnd)
DB_OWNER_USER=postgres
DB_OWNER_PASSWORD=$(rnd)
DB_NAME=switch
ID_SERVER_NAME=localhost
AGENT_REGISTRATION_TOKEN=$(rnd)
CONTROLLER_TOKEN_SECRET=$(rnd)
FEATURE_FLAGS_ENABLED=
SECRET_KEYS=local:$(openssl rand -hex 32)
JWT_SECRET_KEY=
GATEWAY_ADMIN_EMAIL=admin@switch.local
GATEWAY_ADMIN_PASSWORD=$(rnd)
FRONTEND_BASE_URL=http://localhost:5173
MATTERMOST_ADMIN_USER=admin
MATTERMOST_ADMIN_PASSWORD=$(rnd)
MATTERMOST_TEAM_NAME=switch
MATTERMOST_USER=user
MATTERMOST_USER_PASSWORD=$(rnd)
OUTBOUND_ALLOWED_PRIVATE_HOSTS=localhost
TELEMETRY_ENABLED=false
THIRD_PARTY_AVATARS_ENABLED=false
EOF
    echo "wrote $ENV_FILE (secrets not printed)"
    ;;
  up)   dc up -d ;;
  down) dc down ;;
  ps)   dc ps ;;
  logs) shift; dc logs --no-color --tail "${TAIL:-200}" "$@" ;;
  wipe) dc down -v ;;
  *) echo "usage: $0 init-env|up|down|ps|logs [svc]|wipe" >&2; exit 2 ;;
esac
