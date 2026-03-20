#!/bin/bash
# lil_worker bot process manager
# Usage: ./run.sh {start|stop|restart|status|logs}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_SCRIPT="$SCRIPT_DIR/bot.py"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
PID_FILE="$SCRIPT_DIR/lil_worker.pid"
LOG_FILE="$SCRIPT_DIR/lil_worker.log"

case "$1" in
  start)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Already running (PID $(cat "$PID_FILE"))"
      exit 1
    fi
    nohup env PYTHONUNBUFFERED=1 "$VENV_PYTHON" "$BOT_SCRIPT" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Started (PID $!)"
    ;;

  stop)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      kill "$(cat "$PID_FILE")"
      rm -f "$PID_FILE"
      echo "Stopped."
    else
      echo "Not running."
    fi
    ;;

  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;

  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Running (PID $(cat "$PID_FILE"))"
    else
      echo "Not running."
    fi
    ;;

  logs)
    tail -f "$LOG_FILE"
    ;;

  *)
    echo "Usage: $0 {start|stop|restart|status|logs}"
    exit 1
    ;;
esac
