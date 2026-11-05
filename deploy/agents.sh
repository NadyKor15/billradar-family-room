#!/usr/bin/env bash
# Start/stop the two agents as separate processes. Each reads only its own state file.
#   reader       runs in the engine's venv (it talks to BillRadar over MCP stdio)
#   coordinator  runs on plain Python, stdlib only, with its own API key and nothing else
# Stop acts only on the pids this script wrote.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
export FAMILY_ROOM_STATE="${FAMILY_ROOM_STATE:-$ROOT/state}"
ENGINE_PY="${ENGINE_PY:-$ROOT/.venv-engine/bin/python}"
PLAIN_PY="${PLAIN_PY:-$(command -v python3.12 || command -v python3)}"

start_one() {  # name python
  if [ -f "logs/$1.pid" ] && kill -0 "$(cat "logs/$1.pid")" 2>/dev/null; then echo "$1 already running"; return; fi
  nohup "$2" -u -m "family_room.$1" > "logs/$1.log" 2>&1 < /dev/null &
  echo $! > "logs/$1.pid"
  echo "$1 started (pid $(cat "logs/$1.pid"))"
}
stop_one() {
  if [ -f "logs/$1.pid" ]; then
    pid="$(cat "logs/$1.pid")"
    kill "$pid" 2>/dev/null && echo "$1 stopped (pid $pid)" || echo "$1 not running"
    rm -f "logs/$1.pid"
  fi
}
case "${1:-}" in
  start) start_one reader "$ENGINE_PY"; start_one coordinator "$PLAIN_PY" ;;
  stop)  stop_one reader; stop_one coordinator ;;
  restart) "$0" stop; sleep 1; "$0" start ;;
  status) for n in reader coordinator; do
            if [ -f "logs/$n.pid" ] && kill -0 "$(cat "logs/$n.pid")" 2>/dev/null; then echo "$n up"; else echo "$n down"; fi
          done ;;
  *) echo "usage: $0 start|stop|restart|status" >&2; exit 2 ;;
esac
