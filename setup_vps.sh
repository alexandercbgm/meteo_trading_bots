#!/bin/bash
# Запуск от root, из папки, где лежат weather_bot_NN.zip и остальные файлы этого набора:
#   bash setup_vps.sh weather_bot_113.zip
set -e
ZIP="$1"
[ -f "$ZIP" ] || { echo "Использование: bash setup_vps.sh weather_bot_NN.zip"; exit 1; }
HERE="$(pwd)"

bash "$HERE/check_geoblock.sh" | tee /tmp/geo.txt
grep -q '"blocked":false' /tmp/geo.txt || { echo "IP заблокирован, установка остановлена"; exit 1; }

apt-get update -y
apt-get install -y python3-venv python3-pip unzip curl

# swap 1 ГБ (если ещё нет)
if ! swapon --show | grep -q swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

id bot >/dev/null 2>&1 || useradd -m -s /bin/bash bot
mkdir -p /home/bot/weather_bot
unzip -o "$ZIP" -d /home/bot/weather_bot
cp "$HERE/start_bot.py" /home/bot/weather_bot/start_bot.py

sudo -u bot python3 -m venv /home/bot/venv
sudo -u bot /home/bot/venv/bin/pip install --upgrade pip
sudo -u bot /home/bot/venv/bin/pip install -r /home/bot/weather_bot/requirements.txt

if [ ! -f /home/bot/.weather_bot_env ]; then
  cat > /home/bot/.weather_bot_env <<'EOT'
POLYMARKET_WALLET_ADDRESS=
POLYMARKET_PRIVATE_KEY=
EOT
fi
chown -R bot:bot /home/bot
chmod 600 /home/bot/.weather_bot_env

cp "$HERE/weather-bot.service" /etc/systemd/system/weather-bot.service
systemctl daemon-reload
systemctl enable weather-bot

echo
echo "Готово. Дальше:"
echo "  1) nano /home/bot/.weather_bot_env   (вписать кошелёк и приватный ключ)"
echo "  2) systemctl start weather-bot"
echo "  3) journalctl -u weather-bot -f"
