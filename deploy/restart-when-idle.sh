#!/bin/bash
# Restart claude-control for an update without cutting off a running Telegram task.
# The flag file makes the bot finish its running tasks and start no new ones; queued messages stay in the
# queue and start right after the restart - on the new version. Run it outside the service, with a full path:
#   systemd-run --user --unit=claude-control-restart --collect bash ~/claude-control/deploy/restart-when-idle.sh
set -u
cd "$(dirname "$0")/.."
flag=data/restart-pending
touch "$flag"
for _ in $(seq 1 720); do   # check every 5 s for up to 1 hour
  busy=$(.venv/bin/python -c "import sqlite3; c = sqlite3.connect('data/claude-control.db'); \
print(c.execute(\"select count(*) from turns where status = 'running'\").fetchone()[0])")
  if [ "$busy" = "0" ]; then
    sleep 3   # let the last answer reach Telegram
    systemctl --user restart claude-control   # the new version removes the flag and starts the queue
    echo "claude-control restarted"
    exit 0
  fi
  sleep 5
done
rm -f "$flag"
echo "a task is still running after 1 hour - not restarted"
exit 1
