#!/usr/bin/env bash
# Start/stop the two agents as separate processes. Each reads only its own state file.
#   reader       runs in the engine's venv (it talks to BillRadar over MCP stdio)
#   coordinator  runs on plain Python, stdlib only, with its own API key and nothing else
# Stop acts only on the pids this script wrote.
#
# Live mode is off unless LIVE=1 is set by whoever runs this script. Then each agent gets only its own part:
#   both         LIVE, MODEL_API_KEY
#   reader       LIVE_SPEND_CAP_USD, LIVE_READ_TIMEOUT_S and the engine's settings below
#   coordinator  CHOOSER_MAX_CALLS, CHOOSER_TIMEOUT_S
# Without LIVE=1 neither agent gets any of them. Values pass through the environment, never on a command line.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
export FAMILY_ROOM_STATE="${FAMILY_ROOM_STATE:-$ROOT/state}"
ENGINE_PY="${ENGINE_PY:-$ROOT/.venv-engine/bin/python}"
PLAIN_PY="${PLAIN_PY:-$(command -v python3.12 || command -v python3)}"

READER_ONLY="LIVE_SPEND_CAP_USD LIVE_READ_TIMEOUT_S META_SPEND_CAP_USD BILLRADAR_SECOND_READ BILLRADAR_WONDER_REVIEW BILLRADAR_LLAMA_SERVER BILLRADAR_GLIMMER_DIR BILLRADAR_GLIMMER_PORT"
COORDINATOR_ONLY="CHOOSER_MAX_CALLS CHOOSER_TIMEOUT_S"

scope_env() {  # name: leave in this shell's environment only what this agent may have
  if [ "${LIVE:-}" != "1" ]; then
    # shellcheck disable=SC2086
    unset LIVE MODEL_API_KEY $READER_ONLY $COORDINATOR_ONLY
  elif [ "$1" = reader ]; then
    # shellcheck disable=SC2086
    unset $COORDINATOR_ONLY
  else
    # shellcheck disable=SC2086
    unset $READER_ONLY
  fi
}

start_one() {  # name python
  if [ -f "logs/$1.pid" ] && kill -0 "$(cat "logs/$1.pid")" 2>/dev/null; then echo "$1 already running"; return; fi
  (
    scope_env "$1"
    nohup "$2" -u -m "family_room.$1" > "logs/$1.log" 2>&1 < /dev/null &
    echo $! > "logs/$1.pid"
  )
  echo "$1 started (pid $(cat "logs/$1.pid"))"
}
need_key() {
  if [ "${LIVE:-}" = "1" ] && [ -z "${MODEL_API_KEY:-}" ]; then
    echo "LIVE=1 needs MODEL_API_KEY in the environment; nothing started" >&2
    exit 2
  fi
}
stop_one() {
  if [ -f "logs/$1.pid" ]; then
    pid="$(cat "logs/$1.pid")"
    kill "$pid" 2>/dev/null && echo "$1 stopped (pid $pid)" || echo "$1 not running"
    rm -f "logs/$1.pid"
  fi
}
case "${1:-}" in
  start) need_key; start_one reader "$ENGINE_PY"; start_one coordinator "$PLAIN_PY" ;;
  stop)  stop_one reader; stop_one coordinator ;;
  restart) need_key; "$0" stop; sleep 1; "$0" start ;;
  status) for n in reader coordinator; do
            if [ -f "logs/$n.pid" ] && kill -0 "$(cat "logs/$n.pid")" 2>/dev/null; then echo "$n up"; else echo "$n down"; fi
          done ;;
  *) echo "usage: $0 start|stop|restart|status" >&2; exit 2 ;;
esac
