#!/bin/bash
# Restart claude-control as soon as no Telegram task is running or queued.
# Needed when the restart is requested from inside a Telegram session (a plain restart would cut
# that session off mid-answer). Run it outside the service, e.g.:
#   systemd-run --user --unit=claude-control-restart --collect bash ~/claude-control/deploy/restart-when-idle.sh
set -u
cd "$(dirname "$0")/.."
for _ in $(seq 1 720); do   # check every 10 s for up to 2 hours
  busy=$(.venv/bin/python -c "import sqlite3; c = sqlite3.connect('data/claude-control.db'); \
print(c.execute(\"select count(*) from turns where status in ('running', 'queued')\").fetchone()[0])")
  if [ "$busy" = "0" ]; then
    sleep 5   # let the last answer reach Telegram
    systemctl --user restart claude-control
    echo "claude-control restarted"
    exit 0
  fi
  sleep 10
done
echo "still busy after 2 hours - not restarted"
exit 1
