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
    # Clean up any ghost processes before starting
    pkill -f "$VENV_PYTHON $BOT_SCRIPT" 2>/dev/null
    sleep 0.3
    pkill -9 -f "$VENV_PYTHON $BOT_SCRIPT" 2>/dev/null
    nohup env PYTHONUNBUFFERED=1 "$VENV_PYTHON" "$BOT_SCRIPT" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Started (PID $!)"
    # Auto-start watchdog if available and not running
    WATCHDOG="$SCRIPT_DIR/watchdog.sh"
    if [ -x "$WATCHDOG" ]; then
      "$WATCHDOG" start 2>/dev/null
    fi
    ;;

  stop)
    STOPPED=false
    # Kill by PID file
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      kill "$(cat "$PID_FILE")"
      STOPPED=true
    fi
    rm -f "$PID_FILE"
    # Kill any remaining instances (prevents ghost processes)
    pkill -f "$VENV_PYTHON $BOT_SCRIPT" 2>/dev/null && STOPPED=true
    sleep 0.5
    # Force kill if still alive
    pkill -9 -f "$VENV_PYTHON $BOT_SCRIPT" 2>/dev/null
    if $STOPPED; then
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
      # Fallback: check for running process even without PID file
      LIVE_PID=$(pgrep -f "$VENV_PYTHON $BOT_SCRIPT" 2>/dev/null | head -1)
      if [ -n "$LIVE_PID" ]; then
        echo "$LIVE_PID" > "$PID_FILE"
        echo "Running (PID $LIVE_PID, PID file recovered)"
      else
        echo "Not running."
      fi
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
