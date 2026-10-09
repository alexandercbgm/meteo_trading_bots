#!/bin/bash
# Запуск от root.
#   Из клона репозитория:   bash vps_setup/setup_vps.sh
#   Из zip-архива бота:     bash vps_setup/setup_vps.sh weather_bot_NN.zip
set -e
ZIP="$1"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
if [ -n "$ZIP" ]; then
  [ -f "$ZIP" ] || { echo "Файл не найден: $ZIP"; exit 1; }
else
  [ -d "$REPO/weather_bot/src" ] || { echo "Использование: bash vps_setup/setup_vps.sh [weather_bot_NN.zip]"; exit 1; }
fi

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
if [ -n "$ZIP" ]; then
  unzip -o "$ZIP" -d /home/bot/weather_bot
else
  # config.yaml в репозитории — с плейсхолдерами, уже заполненный на сервере не затираем
  CFG=/home/bot/weather_bot/config/config.yaml
  rm -f /tmp/config.yaml.keep
  [ -f "$CFG" ] && cp "$CFG" /tmp/config.yaml.keep
  cp -r "$REPO/weather_bot/." /home/bot/weather_bot/
  [ -f /tmp/config.yaml.keep ] && mv /tmp/config.yaml.keep "$CFG"
fi
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
echo "  2) nano /home/bot/weather_bot/config/config.yaml   (токен Telegram, chat_id, ключ Wunderground)"
echo "  3) systemctl start weather-bot"
echo "  4) journalctl -u weather-bot -f"
