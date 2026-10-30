# Avito Telegram monitor

Бот принимает ссылки поиска Avito (включая `m.avito.ru`), сохраняет URL целиком вместе с фильтрами, периодически открывает страницу через Playwright и отправляет новые карточки в Telegram.

## Запуск

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # заполнить BOT_TOKEN
python bot.py
```

`TELEGRAM_API_ROOT` можно направить на URL Cloudflare Worker, проксирующий Telegram Bot API. `AVITO_PROXY` задаёт прокси, а `PROXY_ROTATE_URL` — endpoint смены IP. Без них используется IP машины запуска.

Для сотен ссылок проверки ограничены `MAX_CONCURRENT_CHECKS`; подберите значение по лимитам прокси и ресурсу. Для production рекомендуется PostgreSQL/Redis и несколько worker-процессов вместо SQLite.
