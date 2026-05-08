# Fast Promo Videos Bot

Telegram-бот для автоматической подготовки коротких вертикальных рекламных видео.

Бот принимает видео, приводит его к формату `9:16`, добавляет рекламный текст или баннер, автоматически создает субтитры из речи и возвращает готовый MP4-файл в Telegram.

## Что умеет

- Принимает Telegram video до `20 МБ` и до `60 секунд`.
- Приводит видео к вертикальному формату `1080x1920`.
- Поддерживает режимы формата: `9:16`, `9:16_soft_zoom`, `9:16_cover`.
- Добавляет рекламный контент сверху: текст или баннер.
- Поддерживает баннеры как Telegram photo или image document: `jpg`, `jpeg`, `png`, `webp`.
- Для PNG с прозрачностью лучше отправлять баннер как файл без сжатия.
- Автоматически распознает речь через OpenAI transcription API и добавляет субтитры.
- Дает пользователю inline-экран настроек перед монтажом.
- Списывает `1 видео` с баланса за один монтаж.
- При технической ошибке монтажа возвращает списанное видео на баланс.
- Хранит пользователей, баланс, платежи и историю задач в PostgreSQL.
- Использует Redis/arq и отдельный worker для тяжелого монтажа.
- Принимает оплату через Robokassa.
- Передает номенклатуру для чеков через `Receipt`.
- Использует Robokassa `Робочеки СМЗ` для формирования кассовых чеков самозанятого.

## Ограничения

- Исходные и готовые видео не хранятся постоянно на сервере.
- Видео и баннеры временно скачиваются worker-процессом только на время монтажа.
- После отправки результата пользователю временные файлы удаляются.
- Если пользователь удалил чат/сообщение/файл в Telegram, бот не сможет восстановить готовое видео.
- В памяти бота остается только активный pending-сценарий пользователя до отправки задачи в очередь; после рестарта его нужно начать заново.
- Для файлов больше `20 МБ` нужен локальный Telegram Bot API server.
- Если `OPENAI_API_KEY` не задан или транскрибация упала, видео может быть смонтировано без субтитров.

## Тарифы

Бот продает пакеты видео:

| Пакет | Цена | Номенклатура в чеке |
| --- | ---: | --- |
| 10 видео | 99 ₽ | Пакет 10 видео Fast Promo Videos Bot |
| 25 видео | 229 ₽ | Пакет 25 видео Fast Promo Videos Bot |
| 50 видео | 399 ₽ | Пакет 50 видео Fast Promo Videos Bot |
| 100 видео | 699 ₽ | Пакет 100 видео Fast Promo Videos Bot |

Одно видео в рамках пакета дает право обработать одно видео длительностью до `60 секунд`.

## Пользовательский сценарий

1. Пользователь запускает `/start`.
2. Бот показывает описание и начисляет вводный бонус, если он еще не был выдан.
3. Пользователь отправляет видео до `20 МБ` и `60 секунд`.
4. Бот просит рекламный текст, баннер или предлагает выбрать `Без контента`.
5. Пользователь выбирает настройки монтажа.
6. При нажатии `Отправить в монтаж` бот списывает `1 видео` с баланса и ставит задачу в очередь.
7. Worker скачивает файлы из Telegram, создает субтитры, монтирует MP4 и отправляет результат.
8. Worker удаляет временные файлы.

## Оплата

Команда `/buy` или кнопка `Пополнить счет` открывает список пакетов.

После выбора пакета бот редактирует сообщение с тарифами и показывает счет:

- кнопка `Оплатить` ведет на страницу Robokassa;
- кнопка `Оферта и условия` ведет на Telegraph-оферту;
- кнопка `Назад` возвращает к выбору пакета.

После успешной оплаты:

- Robokassa вызывает `ResultURL`;
- backend проверяет подпись и сумму;
- платеж становится `paid`;
- видео начисляются на баланс;
- сообщение со счетом удаляется;
- бот отправляет подтверждение оплаты.

Текст подтверждения:

```text
Оплата прошла ✅
Начислено: N видео
Баланс: N видео

На email может прийти письмо от Robokassa с подтверждением оплаты.

Кассовый чек придет отдельным письмом на тот же email.
Срок формирования чека — до 24 часов.
```

Email для чека пользователь вводит на странице Robokassa.

## Архитектура

```text
Telegram
   |
   v
bot service
   |
   |-- PostgreSQL: пользователи, баланс, задачи, платежи
   |-- Redis/arq: очередь монтажа
   |
   v
worker service
   |
   |-- Telegram file download
   |-- OpenAI transcription API
   |-- FFmpeg
   |-- tmp/<job_id> только на время обработки

Robokassa
   |
   v
web service
   |
   |-- /health
   |-- /robokassa/pay/{invoice_id}
   |-- /robokassa/result
   |-- /robokassa/success
   |-- /robokassa/fail
```

## Структура проекта

```text
.
├── app/
│   ├── billing/
│   │   ├── packages.py      # тарифы и номенклатура
│   │   └── robokassa.py     # формы оплаты, Receipt, подписи
│   ├── repositories/
│   │   ├── credits.py
│   │   ├── payments.py
│   │   ├── users.py
│   │   └── video_jobs.py
│   ├── bot.py               # Telegram-сценарии
│   ├── config.py            # переменные окружения
│   ├── db.py                # SQLAlchemy engine/session
│   ├── models.py            # ORM-модели
│   ├── queue.py             # Redis/arq enqueue
│   ├── subtitles.py         # ASS-субтитры
│   ├── transcriber.py       # FFmpeg audio + OpenAI transcription
│   ├── video_processor.py   # FFmpeg-монтаж
│   ├── web.py               # Robokassa callbacks
│   └── worker.py            # arq worker
├── migrations/
├── tmp/
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── invoice.md
├── PRODUCTION_README.md
└── README.md
```

## Переменные окружения

Минимально нужны:

```env
BOT_TOKEN=
OPENAI_API_KEY=
DATABASE_URL=
REDIS_URL=
TMP_DIR=/app/tmp
ADMIN_NOTIFY_CHAT_ID=
PUBLIC_BASE_URL=https://your-web-service.up.railway.app
```

Для Robokassa:

```env
ROBOKASSA_MERCHANT_LOGIN=@fast_promo_videos_bot
ROBOKASSA_PASSWORD1=
ROBOKASSA_PASSWORD2=
ROBOKASSA_TEST_PASSWORD1=
ROBOKASSA_TEST_PASSWORD2=
ROBOKASSA_TEST_MODE=false
ROBOKASSA_HASH_ALGORITHM=md5
ROBOKASSA_OFFER_URL=https://telegra.ph/Oferta-usloviya-okazaniya-uslug-i-politika-obrabotki-personalnyh-dannyh-05-07
```

Секреты хранятся в Railway Variables или локальном `.env`; в git их не коммитим.

## Локальный запуск

1. Создать `.env` по `.env.example`.
2. Запустить инфраструктуру и сервисы:

```bash
docker compose up --build
```

3. Применить миграции, если база новая:

```bash
alembic upgrade head
```

Локально без Docker нужен Python 3.11, FFmpeg, PostgreSQL и Redis.

## Railway

В production используются отдельные сервисы:

- `fast_promo_videos_bot`: `python -m app.bot`;
- `fast_promo_videos_worker`: `python -m app.worker`;
- `fast_promo_videos_web`: `python -m app.web`;
- PostgreSQL;
- Redis.

Для Robokassa в технических настройках магазина:

- `Result URL`: `https://<PUBLIC_BASE_URL>/robokassa/result`, метод `POST`;
- `Success URL`: `https://<PUBLIC_BASE_URL>/robokassa/success`, метод `GET`;
- `Fail URL`: `https://<PUBLIC_BASE_URL>/robokassa/fail`, метод `GET`;
- алгоритм подписи: `MD5`.

`PUBLIC_BASE_URL` должен быть публичным Railway-доменом web-сервиса без слеша в конце.
