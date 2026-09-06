#!/usr/bin/env bash
# One-shot server setup for the slip bot. Run from the project root on a fresh
# Ubuntu droplet as root:   bash deploy/setup.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
echo ">> project: $ROOT"

echo ">> installing system packages ..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip fonts-dejavu-core

if ! swapon --show | grep -q '/swapfile'; then
  echo ">> creating 1 GB swap ..."
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo ">> python environment ..."
python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements.txt

[ -f .env ] || cp .env.example .env

echo ">> installing systemd service ..."
sed "s|/root/telegram-slip-bot|$ROOT|g" deploy/slipbot.service > /etc/systemd/system/slipbot.service
systemctl daemon-reload
systemctl enable slipbot >/dev/null 2>&1 || true

cat <<EOF

============================================================
 Setup done. One step left: put your keys in the .env file.

   nano $ROOT/.env

 Fill in TELEGRAM_BOT_TOKEN, OPENAI_API_KEY and the COMPANY_*
 lines. Save with  Ctrl+O  Enter  Ctrl+X , then start it:

   systemctl restart slipbot
   systemctl status slipbot        # should say: active (running)
   journalctl -u slipbot -f        # live logs
============================================================
EOF
