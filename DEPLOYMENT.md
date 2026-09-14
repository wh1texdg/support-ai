# Развёртывание на VPS

Один Compose-проект использует свои контейнеры, сеть и тома. PostgreSQL и Redis наружу
не публикуются. Панель слушает localhost; порт задаётся `BACKEND_PORT` в `.env`.

## Первый запуск

```bash
git clone https://github.com/wh1texdg/support-ai.git /opt/support-ai
cd /opt/support-ai
python3 -m app.scripts.configure
chmod 600 .env
```

Укажите свободный `BACKEND_PORT` (например, 8010) в `.env`, затем:

```bash
docker compose -f docker-compose.yml -f compose.production.yml build backend
docker compose -f docker-compose.yml -f compose.production.yml up -d --wait
docker compose -f docker-compose.yml -f compose.production.yml exec -T backend python -m app.scripts.seed
```

Production override ограничивает память и объём Docker-логов. Сумма лимитов не является
зарезервированной памятью: перед запуском Telegram-профиля проверьте свободную RAM.
При недостатке ресурсов увеличьте тариф VPS; не меняйте лимиты соседних проектов.

## Telegram и AI

Заполните `BOT_TOKEN` и `OPENAI_API_KEY` в `.env`, затем:

```bash
docker compose -f docker-compose.yml -f compose.production.yml --profile telegram up -d --force-recreate backend bot worker
docker compose -f docker-compose.yml -f compose.production.yml exec -T backend python -m app.scripts.reindex
```

Переиндексация обращается к платному API embeddings. Не запускайте второй polling-процесс
с тем же токеном. Без ключей backend/панель работают, но генерация ответов и Telegram отключены.

## Панель без домена

В терминале своего компьютера откройте SSH-туннель (подставьте пользователя и IP своего VPS):

```bash
ssh -N -L 8010:127.0.0.1:8010 USER@SERVER_IP
```

Пока терминал открыт, панель доступна на `http://localhost:8010`.
Для входа используйте ключ из `OPERATOR_KEYS` в `.env`.
HTTP идёт через зашифрованный SSH-туннель; публичный порт панели открывать не требуется.
Для публичного HTTPS-адреса понадобится доменное имя и отдельный reverse proxy.

## Обновление и проверка

```bash
cd /opt/support-ai
git pull --ff-only
docker compose -f docker-compose.yml -f compose.production.yml build backend
docker compose -f docker-compose.yml -f compose.production.yml --profile telegram up -d --wait
docker compose -f docker-compose.yml -f compose.production.yml ps
curl --fail http://127.0.0.1:8010/health/ready
```

Если Telegram ещё не настроен, опустите `--profile telegram`.
Все сервисы используют `restart: unless-stopped`; они поднимаются после перезапуска Docker/VPS.
Миграция выполняется перед backend. Не запускайте `docker system prune` или команды других
Compose-проектов для обслуживания этого приложения.
