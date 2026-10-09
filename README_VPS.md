# Установка бота на VPS (Vultr, Стокгольм)

1. Остановите бота на PythonAnywhere (Tasks → удалить/отключить) и на ноутбуке.
2. Загрузите на сервер (PowerShell или WinSCP):
   scp weather_bot_113.zip start_bot.py weather-bot.service check_geoblock.sh setup_vps.sh root@IP_СЕРВЕРА:/root/
3. На сервере:
   cd /root && bash setup_vps.sh weather_bot_113.zip
4. Впишите ключи:  nano /home/bot/.weather_bot_env
   POLYMARKET_WALLET_ADDRESS=0x...
   POLYMARKET_PRIVATE_KEY=...
5. Запуск:  systemctl start weather-bot
6. Логи:    journalctl -u weather-bot -f     (или /home/bot/weather_bot/logs/)

Полезно:
  systemctl status weather-bot      состояние
  systemctl restart weather-bot     перезапуск после правки config.yaml
  systemctl stop weather-bot        остановка
  bash /root/check_geoblock.sh      проверка IP
Состояние (data_mining/trading: open_positions.json, pyramid_state.json, ...) при желании
перенесите со старого места, чтобы не сбросились серии пирамиды.
