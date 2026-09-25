#!/bin/bash
# Install or update Claude Control for the CURRENT Linux user (no root needed).
# Safe to re-run: keeps .env and data/, refreshes the venv and the unit, restarts the service.
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$(pwd)" = "$HOME/claude-control" ] || { echo "The project must live in $HOME/claude-control"; exit 1; }

command -v claude >/dev/null || { echo "Claude Code (claude) is not installed"; exit 1; }
claude auth status 2>/dev/null | grep -q '"loggedIn": true' || {
  echo "Claude Code is not logged in for $USER: run 'claude' once and log in, then re-run this script"; exit 1; }

[ -f .env ] || { echo "Missing .env - copy .env.example to .env and fill it in"; exit 1; }
for key in TELEGRAM_BOT_TOKEN OWNER_IDS; do
  grep -q "^$key=." .env || { echo ".env: $key is empty"; exit 1; }
done
chmod 600 .env
mkdir -p data && chmod 700 data

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

mkdir -p ~/.config/systemd/user
cp deploy/claude-control.service ~/.config/systemd/user/claude-control.service

# Keep this user's services running after SSH logout and start them at boot.
loginctl enable-linger "$USER"

systemctl --user daemon-reload
systemctl --user enable claude-control.service
systemctl --user restart claude-control.service
sleep 5
systemctl --user --no-pager status claude-control.service | head -n 8
journalctl --user -u claude-control --no-pager -n 5 -o cat
