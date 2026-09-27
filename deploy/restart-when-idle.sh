#!/bin/bash
# Restart claude-control for an update without cutting off a running Telegram task.
# 1) For up to 3 minutes the bot finishes running tasks and starts no new ones (flag file); queued messages
#    then start right after the restart - on the new version.
# 2) If a long task is still running after that, the queue is released (other topics must not wait for it)
#    and the restart happens at the first moment when no task runs (checked every 5 s, for up to 12 hours).
# Run it outside the service, with a full path:
#   systemd-run --user --unit=claude-control-restart --collect bash ~/claude-control/deploy/restart-when-idle.sh
set -u
cd "$(dirname "$0")/.."
flag=data/restart-pending
running() {
  .venv/bin/python -c "import sqlite3; c = sqlite3.connect('data/claude-control.db'); \
print(c.execute(\"select count(*) from turns where status = 'running'\").fetchone()[0])"
}
touch "$flag"
for i in $(seq 1 8640); do   # every 5 s: 36 checks = 3 minutes of holding the queue, 12 hours in total
  if [ "$(running)" = "0" ]; then
    sleep 3   # let the last answer reach Telegram
    systemctl --user restart claude-control   # the new version removes the flag and starts the queue
    echo "claude-control restarted"
    exit 0
  fi
  if [ "$i" = "36" ] && [ -e "$flag" ]; then
    rm -f "$flag"
    echo "a long task is running: queue released, restarting at the first idle moment"
  fi
  sleep 5
done
rm -f "$flag"
echo "tasks kept running for 12 hours - not restarted"
exit 1
