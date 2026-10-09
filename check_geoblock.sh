#!/bin/bash
# Проверка: разрешена ли торговля с IP этого сервера
R=$(curl -s https://polymarket.com/api/geoblock)
echo "$R"
echo "$R" | grep -q '"blocked":false' && echo "OK: торговля разрешена" || echo "ВНИМАНИЕ: IP заблокирован"
