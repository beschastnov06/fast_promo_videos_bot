# Telegram Video Bot MVP

MVP Telegram-бота на Python, который принимает видео, приводит его через FFmpeg к вертикальному формату `1080x1920`, добавляет верхний текст `Смотри до конца` и нижнюю рекламную плашку `Реклама: @example`, затем возвращает готовый MP4.

## Возможности

- Принимает Telegram video до `100 МБ`.
- Скачивает видео во временную папку.
- Приводит видео к `9:16`, `1080x1920`, с масштабированием и обрезкой по центру.
- Добавляет белый текст на полупрозрачном черном фоне сверху и снизу.
- Сохраняет оригинальный звук, если он есть.
- Отправляет результат как Telegram video.
- Ограничивает обработку одним таймаутом `5 минут`.
- Удаляет временные файлы после обработки.

## Как получить BOT_TOKEN

1. Откройте Telegram и найдите бота `@BotFather`.
2. Отправьте команду `/newbot`.
3. Введите название бота.
4. Введите username бота, он должен заканчиваться на `bot`, например `my_video_mvp_bot`.
5. BotFather пришлет токен вида `123456789:AA...`.
6. Скопируйте токен в файл `.env`.

## Настройка

Создайте `.env` из примера:

```bash
cp .env.example .env
```

Откройте `.env` и укажите токен:

```env
BOT_TOKEN=your_real_bot_token_here
```

По умолчанию бот работает через официальный Telegram Bot API. У него есть важные ограничения: `getFile` скачивает файлы до `20 МБ`, а `sendVideo` загружает новое видео до `50 МБ`. Лимит MVP в коде выставлен `100 МБ`, но для реальной обработки таких файлов нужен локальный Telegram Bot API сервер.

Если используете локальный Bot API сервер, добавьте:

```env
TELEGRAM_API_BASE=http://localhost:8081
TELEGRAM_API_IS_LOCAL=true
```

## Запуск локально

Нужны Python `3.11`, установленный FFmpeg и шрифт `DejaVu Sans` или совместимый системный sans-serif шрифт. В Docker все это устанавливается автоматически.

Проверить FFmpeg:

```bash
ffmpeg -version
```

Установить зависимости и запустить бота:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.bot
```

## Запуск через Docker

Docker-образ уже устанавливает FFmpeg.

```bash
docker compose up --build
```

Запустить в фоне:

```bash
docker compose up --build -d
```

Посмотреть, работает ли контейнер:

```bash
docker compose ps
```

Смотреть логи:

```bash
docker compose logs -f
```

Остановить бота, чтобы он не нагружал компьютер:

```bash
docker compose down
```

Перезапустить после изменений в коде:

```bash
docker compose up --build -d
```

Если нужно только временно остановить без удаления контейнера:

```bash
docker compose stop
```

Потом снова запустить:

```bash
docker compose start
```

## Структура проекта

```text
.
├── app
│   ├── __init__.py
│   ├── bot.py
│   ├── config.py
│   └── video_processor.py
├── tmp
│   └── .gitkeep
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## Ограничения MVP

- Нет очередей задач.
- Нет базы данных.
- Нет админки.
- Нет платежей.
- Нет пользовательской настройки текста.
- Обработка выполняется прямо в процессе бота.
