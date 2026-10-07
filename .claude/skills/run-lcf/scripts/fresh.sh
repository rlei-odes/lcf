#!/bin/sh
# Launch lcf as an isolated installation, so the repo's own .env and database
# are never touched. Wipes the installation first unless --keep is given, which
# is what makes the setup wizard start from nothing.
#
#   fresh.sh [--port N] [--dir PATH] [--keep] [--stop]
#
# Prints the URL and the log path, and returns once the server is answering.
set -eu

PORT=8099
DIR=${TMPDIR:-/tmp}/lcf-fresh
KEEP=0
STOP=0

while [ $# -gt 0 ]; do
    case $1 in
        --port) PORT=$2; shift 2 ;;
        --dir)  DIR=$2;  shift 2 ;;
        --keep) KEEP=1;  shift ;;
        --stop) STOP=1;  shift ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

ROOT=$(cd "$(dirname "$0")/../../../.." && pwd)
if [ ! -f "$ROOT/pyproject.toml" ]; then
    echo "cannot find the lcf checkout (looked in $ROOT)" >&2
    exit 1
fi

# Refuse to run against the checkout itself. The whole point is isolation, and
# --dir is easy to mistype into something that holds real work.
case $(cd "$DIR" 2>/dev/null && pwd || echo "$DIR") in
    "$ROOT"|"$ROOT"/) echo "refusing to use the checkout as the install dir" >&2; exit 1 ;;
esac

PIDFILE=$DIR/server.pid

# Stop whatever this script started last time. By recorded pid, not by name:
# `pkill -f lcf` matches this script's own command line and kills the shell
# running it, and none of fuser/lsof/ss can be relied on to be installed.
# The server is started with setsid, so the pid is also its process group —
# killing the group takes uvicorn's reloader and its worker with it.
if [ -f "$PIDFILE" ]; then
    OLD=$(cat "$PIDFILE")
    if kill -0 "$OLD" 2>/dev/null; then
        kill -TERM "-$OLD" 2>/dev/null || kill -TERM "$OLD" 2>/dev/null || true
        WAITED=0
        while kill -0 "$OLD" 2>/dev/null; do
            WAITED=$((WAITED + 1))
            [ "$WAITED" -gt 20 ] && { kill -KILL "-$OLD" 2>/dev/null || true; break; }
            sleep 0.5
        done
    fi
    rm -f "$PIDFILE"
fi

[ "$STOP" -eq 1 ] && { echo "stopped"; exit 0; }

# Anything else on the port is not ours to kill — say so rather than guess.
if python3 -c "
import socket, sys
s = socket.socket()
s.settimeout(0.5)
sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT)) == 0 else 1)
"; then
    echo "port $PORT is in use by something this script did not start." >&2
    echo "stop it yourself, or pass --port with a free one." >&2
    exit 1
fi

if [ "$KEEP" -eq 0 ]; then
    rm -rf "$DIR"
fi
mkdir -p "$DIR"

# Only these two. Every other LCF_* variable would outrank the config file and
# the wizard would refuse to save that setting — see SKILL.md.
LCF_ENV_FILE="$DIR/.env" LCF_DATA_DIR="$DIR" \
    setsid uv run --project "$ROOT" lcf serve --port "$PORT" --reload \
    > "$DIR/server.log" 2>&1 &
echo $! > "$PIDFILE"

WAITED=0
until grep -q "Application startup complete" "$DIR/server.log" 2>/dev/null; do
    if grep -q "Traceback" "$DIR/server.log" 2>/dev/null; then
        echo "failed to start:" >&2
        tail -20 "$DIR/server.log" >&2
        exit 1
    fi
    WAITED=$((WAITED + 1))
    [ "$WAITED" -gt 60 ] && { echo "gave up waiting; see $DIR/server.log" >&2; exit 1; }
    sleep 1
done

if [ "$KEEP" -eq 0 ]; then
    echo "fresh installation — the setup wizard starts from nothing"
else
    echo "installation kept as it was"
fi
echo "  http://localhost:$PORT/setup"
echo "  install: $DIR"
echo "  log:     $DIR/server.log"
