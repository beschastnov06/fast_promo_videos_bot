# Production README

Документ фиксирует текущую production-архитектуру Fast Promo Videos Bot и ближайшие технические задачи.

Ключевое продуктовое решение: исходные и готовые видео не храним постоянно. Файлы нужны только на время монтажа, после отправки результата пользователю они удаляются из временной папки worker-процесса.

## Текущая архитектура

```text
Telegram
   |
   v
bot service
   |
   |-- PostgreSQL: пользователи, балансы, задачи, платежи
   |-- Redis/arq: очередь задач на монтаж
   |
   v
worker service
   |
   |-- Telegram file download
   |-- OpenAI transcription API
   |-- FFmpeg
   |-- tmp/<job_id>

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

Роли сервисов:

- `bot` принимает сообщения, ведет UX, проверяет баланс, создает платежи и задачи.
- `worker` забирает задачи из Redis/arq, скачивает файлы из Telegram, делает транскрибацию и монтаж, отправляет результат и удаляет временные файлы.
- `web` обслуживает Robokassa: страницу перехода к оплате, `ResultURL`, `SuccessURL`, `FailURL`, healthcheck.
- `postgres` хранит надежное состояние.
- `redis` хранит очередь и технические данные arq.

## Что где хранится

### PostgreSQL

Храним:

- пользователей Telegram;
- факт выдачи вводного бонуса;
- баланс видео;
- историю начислений и списаний;
- задачи на монтаж;
- настройки монтажа;
- статусы и ошибки обработки;
- Telegram `file_id` для скачивания файлов на время обработки;
- платежи Robokassa;
- сырой payload `ResultURL` для диагностики.

### Redis

Используем Redis для очереди задач arq и технических данных очереди. Redis не является источником правды для баланса, платежей и задач.

### Видео-файлы

Постоянно файлы не храним.

Worker временно хранит в `TMP_DIR/<job_id>/`:

- исходное видео;
- рекламный баннер;
- извлеченное аудио;
- `.ass` субтитры;
- готовый MP4.

После успешной отправки или ошибки worker удаляет папку задачи.

## База данных

Миграции лежат в `migrations/versions`.

Основные таблицы:

- `users`;
- `credit_accounts`;
- `credit_transactions`;
- `video_jobs`;
- `video_job_settings`;
- `payments`.

Актуальная модель платежа включает:

- `invoice_id` для `InvId`;
- `telegram_chat_id`;
- `telegram_invoice_message_id` для удаления сообщения со счетом после оплаты;
- `provider = robokassa`;
- `status`;
- `amount_cents`;
- `currency = RUB`;
- `credits_amount`;
- `package_code`;
- `buyer_email`;
- `receipt_status`;
- `receipt_url`;
- `raw_provider_payload`;
- `paid_at`.

Изменение статуса платежа и начисление видео выполняются в одной транзакции. Повторный `ResultURL` не начисляет баланс второй раз.

## Платежи и чеки

Оплата идет через Robokassa.

| Пакет | Цена | Начисление |
| --- | ---: | ---: |
| 10 видео | 99 ₽ | +10 |
| 25 видео | 229 ₽ | +25 |
| 50 видео | 399 ₽ | +50 |
| 100 видео | 699 ₽ | +100 |

Номенклатура передается через `Receipt`.

Важно:

- `Receipt` включается в подпись платежа;
- форма оплаты отправляется методом `POST`;
- `Receipt` передается URL-кодированным;
- `sno` не передается, режим фискализации берется из настроек Robokassa;
- `tax = none`;
- `payment_object = service`;
- email для чека пользователь вводит на странице Robokassa.

`Робочеки СМЗ` формируют кассовый чек и отправляют его покупателю отдельным письмом. По ответу поддержки Robokassa чек может формироваться до 24 часов.

## Docker Compose

Локальное production-like окружение описано в `docker-compose.yml` и включает:

- `bot`: `python -m app.bot`;
- `worker`: `python -m app.worker`;
- `web`: `python -m app.web`;
- `postgres`;
- `redis`.

## Railway

Сервисы:

- `fast_promo_videos_bot`: `python -m app.bot`;
- `fast_promo_videos_worker`: `python -m app.worker`;
- `fast_promo_videos_web`: `python -m app.web`;
- Railway PostgreSQL;
- Railway Redis.

Обязательные переменные для app-сервисов:

```env
BOT_TOKEN=
OPENAI_API_KEY=
DATABASE_URL=
REDIS_URL=
TMP_DIR=/app/tmp
MAX_CONCURRENT_RENDERS=1
RENDER_JOB_TIMEOUT_SECONDS=900
TELEGRAM_REQUEST_TIMEOUT_SECONDS=600
ADMIN_NOTIFY_CHAT_ID=
PUBLIC_BASE_URL=https://<web-service>.up.railway.app
```

Robokassa:

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

Технические настройки магазина Robokassa:

- `Result URL`: `https://<PUBLIC_BASE_URL>/robokassa/result`, метод `POST`;
- `Success URL`: `https://<PUBLIC_BASE_URL>/robokassa/success`, метод `GET`;
- `Fail URL`: `https://<PUBLIC_BASE_URL>/robokassa/fail`, метод `GET`;
- алгоритм подписи: `MD5`.

## Миграции

Перед первым запуском или после добавления миграций:

```bash
alembic upgrade head
```

На Railway миграции можно запускать вручную, отдельной job/service или в start command одного из сервисов. Важно, чтобы миграции применились до кода, который использует новые колонки.

## Что уже сделано

- PostgreSQL-модели и Alembic-миграции.
- Redis/arq очередь и worker.
- Списание видео при постановке задачи в очередь.
- Refund при технической ошибке worker.
- Robokassa payment flow.
- Robokassa `ResultURL` с проверкой подписи.
- Удаление сообщения со счетом после оплаты.
- Передача фискальной номенклатуры.
- Боевая оплата проверена.
- Номенклатура на странице Robokassa проверена после удаления `sno`.

## Ближайшие production-задачи

1. Добавить тесты для Robokassa signatures и `Receipt`.
2. Добавить тест идемпотентности `ResultURL`.
3. Добавить recovery зависших `processing` задач.
4. Добавить cleanup старых временных папок при старте worker.
5. Добавить админские команды для диагностики платежа и баланса.
6. Добавить мониторинг ошибок worker/web.
7. Проверить с Robokassa, можно ли отключить письмо-подтверждение оплаты, оставив кассовый чек.
8. Позже решить вопрос с уведомлением РКН/локализацией персональных данных, если проект пойдет в полноценный запуск.
