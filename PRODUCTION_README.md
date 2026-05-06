# Production-план проекта

Документ описывает, как перевести текущий MVP Telegram-бота в полноценный production-сервис с хранением пользователей, кредитов и очередью задач на монтаж видео.

Важное продуктовое решение: исходные и готовые видео не храним постоянно. Файлы нужны только на время монтажа, после отправки результата пользователю они удаляются из временной папки worker-процесса.

## Целевая архитектура

```text
Telegram
   |
   v
Bot app
   |
   |-- PostgreSQL: пользователи, кредиты, заказы, статусы, история
   |-- Redis: очередь задач, locks, быстрые статусы
   |
   v
Worker app
   |
   |-- FFmpeg: монтаж видео
   |-- OpenAI API: транскрибация
   |-- tmp: временные файлы только на время обработки
```

Роли сервисов:

- `bot` принимает сообщения, валидирует пользователя, проверяет кредиты, создает задачу и отвечает пользователю.
- `worker` забирает задачи из очереди, скачивает файлы из Telegram во временную папку, делает транскрибацию и монтаж, отправляет пользователю готовое видео и удаляет файлы.
- `postgres` хранит надежное состояние: пользователи, баланс кредитов, задачи, списания, история.
- `redis` хранит очередь и краткоживущие технические данные.

## Что где хранить

### PostgreSQL

PostgreSQL должен быть главным источником правды.

Храним:

- пользователей Telegram;
- баланс кредитов;
- историю начислений и списаний;
- видео-заказы;
- параметры монтажа;
- статусы обработки;
- ошибки обработки;
- Telegram file_id для скачивания файлов на время обработки;
- названия файлов и технические metadata без самих видео;
- платежи, когда появится монетизация.

### Redis

Redis не должен быть единственным местом хранения важных бизнес-данных.

Используем Redis для:

- очереди задач на монтаж;
- ограничения параллельной обработки;
- rate limit;
- временных locks;
- быстрых progress/status updates.

Если Redis очистится, мы должны уметь восстановить незавершенные задачи из PostgreSQL.

### Файлы видео

Постоянно файлы не храним.

Храним временно:

- исходное видео;
- рекламный баннер;
- извлеченное аудио;
- `.ass` субтитры;
- готовый MP4.

Где храним:

- только в локальной временной папке worker-процесса;
- на Railway это может быть обычная ephemeral filesystem внутри контейнера;
- volume не обязателен, если мы готовы терять in-progress файл при рестарте worker.

После успешной отправки результата пользователю worker удаляет все файлы задачи.

Если worker упал во время обработки, временные файлы могут потеряться. Это допустимо для текущей продуктовой логики: задача переводится в `failed`, кредит возвращается, пользователь отправляет видео повторно.

Object Storage можно добавить позже, если появится потребность:

- повторно отправлять готовое видео;
- хранить историю результатов;
- делать ручной debug по исходникам;
- восстанавливать задачи после падения worker без повторной отправки пользователем.

## Базовая схема БД

### users

```sql
id uuid primary key
telegram_user_id bigint unique not null
telegram_username text
first_name text
last_name text
status text not null default 'active'
created_at timestamptz not null default now()
updated_at timestamptz not null default now()
```

`status`: `active`, `blocked`, `admin`, `test`.

### credit_accounts

```sql
id uuid primary key
user_id uuid not null references users(id)
balance integer not null default 0
created_at timestamptz not null default now()
updated_at timestamptz not null default now()
```

Один кредит можно трактовать как один готовый монтаж или как условную единицу, зависящую от длительности/качества.

### credit_transactions

```sql
id uuid primary key
user_id uuid not null references users(id)
amount integer not null
reason text not null
source text not null
related_job_id uuid
created_at timestamptz not null default now()
```

Примеры:

- `amount = 10`, `reason = purchase`;
- `amount = -1`, `reason = render_started`;
- `amount = 1`, `reason = render_failed_refund`.

Важно: кредиты списываем транзакционно. Если задача упала по нашей причине, делаем refund отдельной транзакцией.

### video_jobs

```sql
id uuid primary key
user_id uuid not null references users(id)
status text not null
telegram_chat_id bigint not null
telegram_message_id bigint
telegram_video_file_id text
telegram_video_file_unique_id text
ad_content_type text not null default 'none'
ad_text text
ad_banner_file_id text
ad_banner_file_unique_id text
ad_banner_name text
error_message text
credits_charged integer not null default 0
created_at timestamptz not null default now()
queued_at timestamptz
started_at timestamptz
finished_at timestamptz
updated_at timestamptz not null default now()
```

`status`:

- `draft`;
- `queued`;
- `processing`;
- `completed`;
- `failed`;
- `cancelled`.

`ad_content_type`: `none`, `text`, `banner`.

`telegram_video_file_id` и `ad_banner_file_id` нужны worker-процессу, чтобы скачать файлы из Telegram перед монтажом. Готовое видео после отправки пользователю не сохраняется.

### video_job_settings

```sql
id uuid primary key
job_id uuid unique not null references video_jobs(id)
video_count integer not null default 1
video_format text not null default '9:16'
fill_color text not null default 'black'
subtitle_font text not null default 'DejaVu Sans'
subtitle_color text not null default 'white'
video_speed numeric(3, 2) not null default 1.00
mirror boolean not null default false
strip_metadata boolean not null default true
created_at timestamptz not null default now()
updated_at timestamptz not null default now()
```

Для MVP `video_count = 1`, но поле сразу готовит нас к нескольким видео в одном заказе.

### payments

```sql
id uuid primary key
user_id uuid not null references users(id)
provider text not null
provider_payment_id text
status text not null
amount_cents integer not null
currency text not null
credits_amount integer not null
created_at timestamptz not null default now()
paid_at timestamptz
```

Понадобится, когда добавим оплату.

## Очередь задач

Рекомендуемый вариант для Python-проекта:

- Redis + `arq`, если хотим async-native worker;
- Redis + `rq`, если хотим простую синхронную очередь;
- Celery, если ожидается сложная инфраструктура, retries, routing, periodic tasks.

Для текущего проекта лучше начать с `arq` или `rq`.

### Почему не обрабатывать в polling-процессе

FFmpeg и транскрибация тяжелые и долгие. Если они выполняются прямо в процессе бота:

- бот хуже отвечает на новые сообщения;
- сложно контролировать параллельную нагрузку;
- при рестарте теряется состояние задач;
- нельзя удобно показывать очередь и retries;
- один сбой монтажа может повлиять на пользовательский UX.

### Жизненный цикл задачи

```text
1. Пользователь отправляет видео.
2. Bot сохраняет Telegram `file_id` в задаче.
3. Bot создает `video_jobs` со статусом `draft`.
4. Пользователь выбирает рекламный контент и настройки.
5. Bot проверяет кредиты.
6. Bot в транзакции:
   - списывает кредит;
   - создает credit_transactions;
   - переводит job в queued;
   - кладет job_id в Redis-очередь.
7. Worker берет job_id.
8. Worker переводит job в processing.
9. Worker скачивает исходное видео и баннер из Telegram во временную папку.
10. Worker монтирует видео.
11. Worker отправляет готовое видео пользователю.
12. Worker удаляет временные файлы.
13. Worker переводит job в completed.
```

Если worker падает:

- задача остается в `processing`;
- отдельный recovery-процесс находит зависшие задачи;
- переводит их в `failed`;
- возвращает кредит отдельной транзакцией;
- просит пользователя отправить видео заново.

Мы не переводим такую задачу обратно в `queued`, потому что временные файлы могли быть потеряны при рестарте. Повторный запуск возможен только если Telegram `file_id` еще доступен и скачивание проходит успешно, но базовая надежная логика для старта: `failed + refund`.

## Кредиты

На старте проще всего:

- 1 готовое видео = 1 кредит;
- кредит списывается при нажатии `Отправить в монтаж`;
- если ошибка произошла до успешного результата по вине сервиса, кредит возвращается;
- если пользователь отправил неподходящий файл и обработка невозможна из-за входных данных, решение по refund лучше прописать отдельно.

Важно не хранить баланс только числом без истории. Нужны обе сущности:

- `credit_accounts.balance` для быстрого чтения;
- `credit_transactions` для аудита и восстановления.

Списание должно выполняться в одной DB-транзакции:

```text
lock credit_accounts row
check balance
decrease balance
insert credit_transactions
update video_jobs
enqueue job
commit
```

Если очередь недоступна, транзакцию не коммитим.

## Изменения в коде

### Новые модули

```text
app/
├── db.py                 # подключение к PostgreSQL
├── models.py             # ORM-модели или SQL-запросы
├── repositories/
│   ├── users.py
│   ├── credits.py
│   └── video_jobs.py
├── services/
│   ├── credits.py        # списание, начисление, refund
│   ├── telegram_files.py # скачивание файлов Telegram во временную папку
│   ├── cleanup.py        # уборка временных файлов
│   └── video_jobs.py     # бизнес-логика задач
├── queue.py              # enqueue/dequeue
└── worker.py             # обработчик очереди
```

### Переменные окружения

```env
DATABASE_URL=postgresql+asyncpg://user:password@postgres:5432/video_bot
REDIS_URL=redis://redis:6379/0
TMP_DIR=/app/tmp
MAX_CONCURRENT_RENDERS=2
RENDER_JOB_TIMEOUT_SECONDS=900
```

### Docker Compose для production-like окружения

```yaml
services:
  bot:
    build: .
    command: python -m app.bot
    env_file:
      - .env
    depends_on:
      - postgres
      - redis

  worker:
    build: .
    command: python -m app.worker
    env_file:
      - .env
    depends_on:
      - postgres
      - redis
    volumes:
      - worker_tmp:/app/tmp

  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: video_bot
      POSTGRES_USER: video_bot
      POSTGRES_PASSWORD: video_bot
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  redis_data:
  worker_tmp:
```

Для Railway volume под `worker_tmp` можно не подключать на первом этапе. Без volume файлы будут жить только внутри контейнера и исчезать при рестарте, что совпадает с решением не хранить видео. Volume имеет смысл подключить, если понадобится переживать краткие рестарты worker и дочищать временные файлы после старта.

## План внедрения

### Уже сделано

- Добавлены production-зависимости: SQLAlchemy, asyncpg, Alembic, Redis/arq.
- Расширен конфиг приложения: `DATABASE_URL`, `REDIS_URL`, `TMP_DIR`, лимиты worker.
- Добавлены SQLAlchemy-модели для пользователей, кредитов, задач, настроек и платежей.
- Добавлена первая Alembic-миграция `0001_initial_production_schema`.
- Добавлен Redis queue helper и заготовка `app.worker`.
- `docker-compose.yml` подготовлен для bot + worker + PostgreSQL + Redis.

### Этап 1. Подготовить БД

- Добавить PostgreSQL в `docker-compose.yml`.
- Выбрать миграции: Alembic.
- Добавить модели/таблицы `users`, `credit_accounts`, `credit_transactions`, `video_jobs`, `video_job_settings`.
- При первом сообщении пользователя создавать или обновлять запись в `users`.

### Этап 2. Перенести pending-состояние из памяти в БД

- Сейчас `pending_videos` хранится в памяти.
- Нужно заменить его на `video_jobs` со статусом `draft`.
- Все настройки монтажа сохранять в `video_job_settings`.
- После рестарта бот должен продолжить видеть незавершенную задачу пользователя.

### Этап 3. Добавить кредиты

- Создать кредитный аккаунт при регистрации пользователя.
- Добавить ручное начисление кредитов для теста.
- На `Отправить в монтаж` проверять баланс.
- Списывать кредит транзакционно.
- Делать refund при технической ошибке обработки.

### Этап 4. Добавить очередь и worker

- Добавить Redis.
- Добавить `app.worker`.
- Bot только создает задачу и кладет `job_id` в очередь.
- Worker скачивает файлы из Telegram во временный `tmp`.
- Worker выполняет текущий `process_video`.
- Worker отправляет результат пользователю и удаляет временные файлы.
- Ограничить параллельность через `MAX_CONCURRENT_RENDERS`.

### Этап 5. Добавить cleanup и recovery

- Удалять файлы задачи в `finally`.
- При старте worker очищать старые временные папки.
- Добавить recovery зависших `processing` задач.
- При техническом `failed` возвращать кредит.
- Сообщать пользователю, что файл нужно отправить заново.

### Этап 6. Production-надежность

- Healthchecks для bot, worker, postgres, redis.
- Structured logs с `job_id`, `telegram_user_id`.
- Sentry или другой error tracking.
- Recovery зависших задач.
- Cleanup policy для временных файлов.
- Backups PostgreSQL.
- Мониторинг диска, CPU, RAM, времени очереди и ошибок FFmpeg.

## Минимальный production-ready вариант

Для первого прод-запуска достаточно:

- PostgreSQL для пользователей, кредитов и задач.
- Redis + один worker для очереди.
- Локальный `tmp` только как временное рабочее место без постоянного хранения видео.
- Списание кредитов при старте монтажа.
- Refund при технической ошибке.
- Ограничение параллельных FFmpeg-задач.
- Гарантированная очистка временных файлов после обработки.
- Логи с `job_id`.

После этого можно добавлять оплату, админку, несколько workers и более сложные тарифы.

## Railway-вариант

На Railway стартуем без Storage Bucket.

Сервисы:

- `bot`: long-running service, команда `python -m app.bot`;
- `worker`: long-running service, команда `python -m app.worker`;
- `postgres`: Railway PostgreSQL;
- `redis`: Railway Redis.

Файлы:

- bot сохраняет в БД Telegram `file_id`;
- worker скачивает видео и баннер из Telegram в `/app/tmp/<job_id>/`;
- worker монтирует видео;
- worker отправляет MP4 пользователю;
- worker удаляет `/app/tmp/<job_id>/`.

Главный компромисс Railway без постоянного файлового хранилища: если worker перезапустился во время обработки, файл теряется. Для нас это нормально, если мы автоматически возвращаем кредит и просим пользователя отправить видео еще раз.

### Порядок деплоя на Railway

1. Создать Railway project из GitHub-репозитория.
2. Добавить PostgreSQL service.
3. Добавить Redis service.
4. Создать два app service из одного репозитория:
   - `bot` со start command `python -m app.bot`;
   - `worker` со start command `python -m app.worker`.
5. В оба app service добавить переменные:
   - `BOT_TOKEN`;
   - `OPENAI_API_KEY`;
   - `ALLOWED_TELEGRAM_USERNAMES`;
   - `TMP_DIR=/app/tmp`;
   - `MAX_CONCURRENT_RENDERS=1`;
   - `RENDER_JOB_TIMEOUT_SECONDS=900`;
   - `DATABASE_URL` из PostgreSQL service;
   - `REDIS_URL` из Redis service.
6. Перед первым запуском выполнить миграции:

```bash
alembic upgrade head
```

На Railway миграции можно запускать вручную через shell/CLI или отдельной одноразовой service/job. Для первого запуска проще выполнить их вручную после подключения PostgreSQL.
