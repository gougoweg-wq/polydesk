#!/bin/zsh
# polydesk: engine + terminal on http://localhost:8747
cd "$(dirname "$0")"
mkdir -p data logs
if pgrep -f "polydesk.api:app" > /dev/null; then echo "already running: http://localhost:${DASHBOARD_PORT:-8747}"; exit 0; fi
nohup caffeinate -is .venv/bin/python -m polydesk run > logs/polydesk.log 2>&1 &
echo "polydesk: http://localhost:${DASHBOARD_PORT:-8747}   (log: logs/polydesk.log)"
