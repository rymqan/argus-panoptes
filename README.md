# Odyssey watch

Каждые 15 минут проверяет [расписание «Одиссеи» в Астане](https://kz.kinoafisha.info/astana/movies/8379477/)
и шлёт сообщение в Telegram, когда появляются сеансы на **пн 3 авг** или **вт 4 авг 2026**
в **Kinopark 8 IMAX Saryarka** или **Kinopark 7 IMAX Keruen**.

- `check.py` — парсер (stdlib, без зависимостей) + отправка в Telegram
- `state.json` — уже отправленные уведомления (дедуп), коммитится воркфлоу
- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

Тест: `gh workflow run watch.yml -f test=true`
