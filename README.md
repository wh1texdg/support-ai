# SupportAI

[![checks](https://github.com/wh1texdg/support-ai/actions/workflows/tests.yml/badge.svg)](https://github.com/wh1texdg/support-ai/actions/workflows/tests.yml)

AI-поддержка интернет-магазина с RAG и передачей диалога живому оператору.
Telegram — клиент; бизнес-логика, история и контроль доступа находятся в FastAPI.
Проект рассчитан на портфолио и демонстрацию коммерческого MVP.

**Что реализовано:** каталог и FAQ, pgvector retrieval, история последних 10 сообщений,
структурированные ответы LLM с источниками, отказ при недостатке данных,
операторская панель, feedback, rate limit, миграции и надёжная очередь ответов оператора.

Все seed-данные вымышлены: **20 товаров, 24 FAQ, 4 документа**. После reindex —
48 документов, включая автоматически сформированные карточки каталога и FAQ.
Цены, характеристики и правила демо-магазина нельзя использовать как реальные условия продаж.

## Архитектура

```mermaid
flowchart LR
    User[Пользователь Telegram] --> Bot[aiogram 3 / polling]
    Bot -->|REST + service token| API[FastAPI]
    API --> Chat[Chat service]
    Chat --> Redis[(Redis: rate limit / cache)]
    Chat --> RAG[RAG service]
    RAG --> Embeddings[EmbeddingService]
    Embeddings --> OpenAI[OpenAI API]
    RAG --> PG[(PostgreSQL + pgvector)]
    Chat --> LLM[LLMProvider]
    LLM --> OpenAI
    Chat --> PG
    Operator[Панель оператора] -->|REST + operator token| API
    API --> Outbox[(Transactional outbox)]
    Outbox --> Worker[Delivery worker]
    Worker -->|Telegram Bot API| User
```

Один backend, отдельные процессы бота и worker. Панель — HTML/CSS/JavaScript без сборщика.
В MVP нет отдельной CRM, брокера задач или микросервисов для каждой сущности.

### Поток сообщения

1. Бот принимает личное текстовое сообщение и вызывает `POST /api/chat` с ID события.
2. Backend проверяет service token, блокирует обработку параллельных сообщений пользователя
   через PostgreSQL advisory lock и проверяет идемпотентность события.
3. Находит активный диалог или создаёт новый; для AI проверяет лимит Redis.
4. Берёт до 10 последних сообщений. Последние два вопроса включаются в retrieval query,
   чтобы фраза «сколько он стоит?» могла найти ранее упомянутую модель.
5. Получает embedding, ищет top-5 фрагментов, отбрасывает score ниже порога.
6. Передаёт модели system prompt, найденные знания, ограниченную историю и текущий вопрос.
7. Проверяет structured output: `answer`, `insufficient`, `source_ids`. Неизвестные или пустые
   источники приводят к безопасному отказу.
8. Сохраняет вопрос, ответ, источники и результат события в одной транзакции; бот показывает ответ.

### RAG

- Chunking: около 800 символов, overlap 120, разрыв предпочтительно по границе слова.
- Embeddings: `text-embedding-3-small`, **1536 измерений**, абстракция `EmbeddingService`.
- Search: cosine distance `<=>`, HNSW `vector_cosine_ops`; similarity = `1 - distance`.
- Порог по умолчанию **0.35**, top-k = 5. Это эвристика релевантности, не вероятность правильного ответа.
- Кеш embeddings запросов: Redis, SHA-256 ключ с именем модели, TTL 1 час.
- При CRUD товара/FAQ документ и embeddings обновляются атомарно. Сбой провайдера откатывает изменение.
- Удаление оригинала удаляет его документ и chunks. Ручные документы используют свой `source`.
- Все мутации индекса сериализуются транзакционным advisory lock.
- Переиндексация атомарна: до commit читатели видят старый индекс. Для MVP выполняется синхронно;
  для большого каталога нужен фоновый job с прогрессом и переключением версии индекса.
- Имя embedding-модели сохранено у каждого chunk: retrieval не смешивает разные модели.
  При смене модели выполните reindex; смена размерности требует новой миграции.

Prompt запрещает придумывать магазинные факты и выполнять инструкции из документов.
Structured Outputs гарантирует форму ответа, **но не истинность его содержания**.
Источники проверяются по ID; доказательство каждого утверждения автоматически не выполняется.
Для реального магазина нужны ручная оценка retrieval и набор вопросов для проверки качества.

### Передача человеку

`/operator`, кнопка или явная просьба создают одно обращение на диалог и переводят его
в `waiting_operator`. Недостаточные знания, низкая похожесть, сбой LLM и два отрицательных
отзыва предлагают кнопку передачи. Без нажатия оператор автоматически не вызывается.

Оператор своим ключом берёт обращение: `waiting → assigned`, диалог становится `operator`.
Другой оператор не может отвечать или закрывать его. Все новые вопросы сохраняются без вызова AI.
Ответ и outbox-задание записываются в одной транзакции. Worker доставляет ответ через Telegram API,
с backoff, обработкой RetryAfter и максимумом 8 попыток. Постоянные ошибки имеют статус `failed`.
Очередь сохраняет порядок pending-сообщений одного пользователя; другие пользователи не блокируются.
Закрытие переводит диалог в `closed`; следующий вопрос создаёт новый диалог.

Доставка outbox имеет семантику **at-least-once**: если Telegram принял ответ, а процесс упал до commit,
возможен повтор. Bot API не предоставляет idempotency key для sendMessage. Обычные AI-ответы бот
отправляет напрямую; при аварии polling-процесса между приёмом и ответом пользователь может
потребоваться повторить вопрос. Durable inbox/webhook — следующий шаг для production.

## Стек и структура

Python 3.12+, FastAPI, SQLAlchemy 2 async, asyncpg, Alembic, PostgreSQL 16, pgvector,
Redis 7, aiogram 3, OpenAI SDK, httpx, pytest. Docker использует зафиксированные runtime-версии
из `requirements.lock`; допустимые диапазоны разработки указаны в `pyproject.toml`.

```text
app/
  api/           auth, schemas, REST routes
  core/          settings, JSON logging
  database/      models, session, repositories
  services/      chat, support, rag, embeddings, llm, limits, runtime
  scripts/       seed, reindex
  static/        operator panel
bot/main.py      Telegram handlers, buttons, resilient Redis FSM
worker/main.py   PostgreSQL outbox delivery
alembic/         versioned, frozen initial schema
tests/          API/service tests and real pgvector integration test
docker/         non-root Docker image
.github/        CI with PostgreSQL/pgvector, migrations, lint, tests and image build
```

Таблицы: `users`, `conversations`, `messages`, `products`, `faq`, `knowledge_documents`,
`knowledge_chunks`, `support_requests`, `feedback`, `incoming_events`, `outbox`.
Partial unique index допускает только один незакрытый диалог пользователя; уникальные ключи
защищают от дубликатов событий, feedback и обращений. FK с CASCADE удаляет chunks документа.

## Быстрый запуск

Нужны Docker Engine/Desktop с Compose v2, токен бота от BotFather и OpenAI API key с доступом
к выбранным моделям. Без токенов backend и панель запускаются, AI отвечает fallback,
а процессы Telegram ожидают конфигурации.

```bash
python -m app.scripts.configure
```

Команда создаёт `.env` со случайными независимыми ключами и паролем БД, не перезаписывая существующий файл.
Она использует только стандартную библиотеку Python. Альтернатива — `cp .env.example .env`
(PowerShell: `Copy-Item .env.example .env`) с ручной заменой всех примеров ключей.

Заполните `.env`:

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | Telegram bot token |
| `OPENAI_API_KEY` | Ключ OpenAI API |
| `ADMIN_API_KEY` | Отдельный случайный ключ администратора |
| `BOT_API_KEY` | Отдельный случайный ключ backend для бота |
| `OPERATOR_KEYS` | JSON, например `{"случайный-длинный-ключ":"alice"}` |
| `POSTGRES_PASSWORD` | Пароль БД; для Compose используйте URL-safe значение |
| `LLM_MODEL` | По умолчанию `gpt-4.1-mini`, можно заменить совместимой моделью |
| `EMBEDDING_MODEL` | По умолчанию `text-embedding-3-small` |
| `SIMILARITY_THRESHOLD` | Начальный порог `0.35`, настраивается по вашим данным |

Сгенерировать ключ: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Пустые API-ключи не авторизуют запросы. Примеры ключей в `.env.example` обязательно замените.

```bash
docker compose up -d --build
docker compose exec backend python -m app.scripts.seed
docker compose exec backend python -m app.scripts.reindex
```

Миграции выполняет одноразовый сервис `migrate` до запуска backend. Seed не требует OpenAI;
reindex вызывает платный embeddings API. После этого напишите боту `/start`.

- Панель: [localhost:8000](http://localhost:8000) — введите ключ из `OPERATOR_KEYS`.
- Swagger: [localhost:8000/docs](http://localhost:8000/docs) — кнопка Authorize принимает нужный Bearer token.
- Liveness: `/health/live`; readiness БД и Redis: `/health/ready`.

```bash
docker compose logs -f backend bot worker
docker compose ps
docker compose down
```

Обычный `down` сохраняет тома. При смене ключей перезапустите процессы:
`docker compose up -d --force-recreate backend bot worker`.
Для ручного применения миграций: `docker compose run --rm migrate alembic upgrade head`.

## Локальная разработка

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.lock
pip install -e '.[test]'
```

Используйте доступные локально PostgreSQL с extension vector и Redis. В `.env` укажите
`DATABASE_URL=postgresql+asyncpg://...@localhost:5432/...`, `REDIS_URL=redis://localhost:6379/0`,
`BACKEND_URL=http://localhost:8000`. Compose намеренно не публикует порты БД/Redis;
для локальной разработки с контейнерными БД добавьте отдельный compose override с localhost-портами.

```bash
alembic upgrade head
python -m app.scripts.seed
python -m app.scripts.reindex
uvicorn app.main:app --reload
# Два других терминала:
python -m bot.main
python -m worker.main
```

## API

Все пути ниже начинаются с `/api`. JSON bodies описаны в OpenAPI; списки поддерживают
`offset` и `limit` (максимум 100).

| Доступ | Endpoint | Назначение |
|---|---|---|
| Admin | GET/POST `/products` | Список, создание и индексирование |
| Admin | GET/PATCH/DELETE `/products/{id}` | Карточка, изменение, удаление |
| Admin | GET/POST `/faq` | FAQ |
| Admin | GET/PATCH/DELETE `/faq/{id}` | Чтение и изменение FAQ |
| Admin | GET/POST `/knowledge` | Ручные документы |
| Admin | DELETE `/knowledge/{id}` | Удаление ручного документа |
| Admin | POST `/knowledge/reindex` | Полный атомарный reindex |
| Staff | GET `/conversations` | Диалоги |
| Staff | GET `/conversations/{id}` | История: `after_id`, `limit`, `next_after_id` |
| Staff | GET `/support/requests` | Обращения и имена пользователей |
| Staff | GET `/support/{id}` | Актуальное состояние отдельного обращения |
| Operator | POST `/support/{id}/take` | Взять обращение |
| Operator | POST `/support/{id}/message` | `{"text":"Ответ"}` |
| Operator | POST `/support/{id}/close` | Закрыть |
| Staff | GET `/support/delivery` | Состояние очереди |
| Admin | POST `/support/delivery/{id}/retry` | Повтор failed-доставки |
| Bot | POST `/chat` | Вопрос, `telegram_id`, уникальный `event_id` |
| Bot | POST `/feedback` | `message_id`, `telegram_id`, `helpful` |
| Admin | POST `/debug/search` | `{"text":"доставка в Москву"}`, chunks и scores |

Staff = admin или оператор. Admin не подменяет операторскую личность при take/message/close.
Операторы видят общую очередь и историю, но отвечают только в назначенные им обращения.
Значения `product:` и `faq:` в source зарезервированы; редактируйте исходную сущность.

Пример запроса (подставьте свой service token):

```bash
curl http://localhost:8000/api/chat \
  -H 'Authorization: Bearer YOUR_BOT_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"telegram_id":123456,"event_id":"manual:1","text":"Есть доставка в Москву?"}'
```

Для проверки реальной доставки операторского ответа используйте свой Telegram ID после первого
сообщения боту. Случайный ID годится только для API-проверки.

## Примеры диалогов

**Пользователь:** Есть доставка в Москву?  
**Ожидаемое поведение:** ответ о доступной доставке и расчёте стоимости при оформлении,
с источником «Есть ли доставка в Москву?» или «Условия доставки».

**Пользователь:** Можно доставить вертолётом?  
**Ожидаемое поведение:** недостаточно информации и кнопки «Передать оператору» / «Продолжить диалог».
Похожесть на документ о доставке сама по себе не подтверждает такую услугу.

**Пользователь:** Позови человека.  
**Ответ backend:** «Я передал ваш вопрос оператору. Он подключится к диалогу, когда будет доступен».

**Пользователь:** Когда приедет мой заказ?  
**Ожидаемое поведение:** объяснить отсутствие доступа к системе заказов и предложить оператора.
Интеграции с заказами и платёжными данными здесь нет.

## Ошибки и эксплуатация

- AI-запросы: максимум 10 за 60 секунд на пользователя, атомарный Lua INCR/EXPIRE.
  Ответ 429 содержит Retry-After. Явная передача оператору обходит AI-лимит.
- Redis недоступен: платные вызовы fail-closed (503); операторский сценарий продолжает работать.
  FSM хранит лишь необязательную UI-подсказку; её сбой не заменяет состояние PostgreSQL.
- LLM timeout/refusal/невалидный structured output: сохранённый fallback и предложение оператора.
- Ошибка SQL: rollback и 503; конфликт ограничения — 409. Ошибка embeddings CRUD — rollback/503.
- JSON-логи содержат event names, ID диалога, длину сообщения, статус и длительность запроса.
  Тексты диалогов и секреты не логируются; исключения провайдеров не печатаются целиком.
- Панель выводит сообщения через textContent, применяет CSP и не сохраняет ключ в localStorage.
- Backend слушает только `127.0.0.1` на хосте. Для внешнего доступа настройте HTTPS reverse proxy,
  индивидуальные ключи, резервное копирование, политику хранения диалогов и мониторинг failed-outbox.
- Только один polling-бот на token; backend и worker можно масштабировать. На MVP сетевые вызовы
  удерживают транзакцию/блокировку диалога; при росте нагрузки потребуется job orchestration.

## Проверки

```bash
pytest -q
ruff check .
ruff format --check .
alembic upgrade head --sql
```

Большинство тестов работают без внешних сервисов: SQLite с FK для repositories/API,
fakeredis для лимитов, mock LLM/embeddings. SQLite **не проверяет pgvector и PostgreSQL locks**.
Если задан `TEST_DATABASE_URL`, весь набор API/repository-тестов использует настоящую PostgreSQL
в отдельных случайных схемах. Дополнительно проверяются cosine search и конкуренция за диалог/обращение:

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://support:support@localhost:5432/support_test
pytest -q
```

В PowerShell: `$env:TEST_DATABASE_URL='postgresql+asyncpg://...'`.
Тест создаёт и удаляет свою случайную схему; используйте отдельную тестовую БД и разрешение CREATE EXTENSION.
GitHub Actions дополнительно применяет миграцию, проверяет drift через Alembic, собирает Docker image
и запускает весь набор на PostgreSQL с pgvector. Затем Compose поднимается целиком:
smoke-сценарий проверяет readiness, seed-каталог, обращение, ответ оператора, outbox и закрытие.
Smoke использует искусственного пользователя и предназначен только для тестового окружения.

## Следующие улучшения

Retrieval eval-набор и hybrid search; фоновая переиндексация; durable Telegram inbox/webhook;
SSO операторов и audit trail; интеграция с заказами по проверенной личности;
retention/удаление персональных данных; метрики latency, handoff rate и delivery failures.

## Документация провайдера

Адаптер использует [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
и [Embeddings API](https://developers.openai.com/api/docs/guides/embeddings).
Модели задаются через environment; доступность зависит от вашего API-аккаунта.

### Polza AI

Для Polza AI укажите в `.env` переменную `POLZA_AI_API_KEY`,
`LLM_MODEL=openai/gpt-4.1-mini` и
`EMBEDDING_MODEL=openai/text-embedding-3-small`.
При наличии ключа Polza приложение использует `https://api.polza.ai/api/v1`
для ответов и эмбеддингов; ключ OpenAI этому сервису не передаётся.
После смены модели эмбеддингов выполните повторную индексацию базы знаний.
Без ключа Polza сохраняется прямое подключение к OpenAI.

### Возврат к AI

Команды `/start`, `/bot` и `/cancel` закрывают текущее обращение оператору и
начинают новый диалог с AI. В режиме ожидания доступна кнопка «Вернуться к AI».
Кнопка вызова оператора появляется при нехватке информации; вручную вызвать
человека можно командой `/operator`.
Адрес Polza при необходимости задаётся переменной `POLZA_BASE_URL`.

## Путь покупателя

Бот содержит описание профиля, меню команд и разделы магазина. Каталог, карточки,
доставка, оплата и возврат доступны без обращения к AI. После первого «Не помогло»
можно выбрать оператора или сохранить текущую переписку с ботом.
Сценарии и критерии проверки: [USER_STORIES.md](docs/USER_STORIES.md).

Инструкция по установке и обновлению на сервере: [DEPLOYMENT.md](docs/DEPLOYMENT.md).
