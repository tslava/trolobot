# trolobot

Телеграм-бот-персонаж «Отец Фёдор» для одного группового чата. Читает переписку,
изредка отвечает и вмешивается в разговор в характере персонажа. Один чат, один
процесс, long polling, без вебхуков и без веб-панели. Подробности поведения — в
`CHARACTER.md`, устройство кода — в `PLAN.md` и `CLAUDE.md`.

## Настройка перед первым запуском

### 1. BotFather

1. Создать бота через [@BotFather](https://t.me/BotFather) командой `/newbot`,
   сохранить токен.
2. Задать имя профиля бота — **Отец Фёдор**.
3. Выполнить `/setprivacy` → **Disable**. Это позволяет боту видеть все
   сообщения в группе, а не только команды и реплаи на себя.
4. Если бот уже был добавлен в чат до `/setprivacy` — режим приватности не
   применится к уже состоящему в чате боту. **Удалить бота из чата и добавить
   заново.**

### 2. OpenRouter

1. Создать ключ на [openrouter.ai](https://openrouter.ai/keys).
2. **Обязательно** поставить на ключе `credit limit` в личном кабинете
   OpenRouter. Без лимита сбой предохранителя в коде ничем не ограничен —
   не выкатывать без этого шага.

### 3. Discovery mode — узнать chat_id и свой user_id

Пока `ALLOWED_CHAT_ID` в `.env` не задан (или равен `0`), бот работает в
discovery mode: ничего не пишет в БД, ни на что не отвечает, а только
логирует уровнем `WARNING` `chat_id`, название чата, `user_id` и
`display_name` каждого входящего сообщения.

1. Заполнить `BOT_TOKEN` в `.env`, оставить `ALLOWED_CHAT_ID` пустым.
2. Запустить бота (см. ниже), добавить его в целевой чат, написать туда
   любое сообщение.
3. В логе первого запуска найти строку с нужным `chat_id` — вписать его в
   `ALLOWED_CHAT_ID`. Тем же способом смотрите `user_id` своего аккаунта для
   `ADMIN_USER_ID`.
4. Перезапустить бота — discovery mode выключится, бот начнёт работать в
   указанном чате.

## Запуск локально

```bash
uv sync
cp .env.example .env   # заполнить BOT_TOKEN и остальные ключи
uv run python -m trolobot
```

## Запуск в Docker

Перед первым `docker compose up` обязательно:

```bash
mkdir -p data && sudo chown 10001:10001 data
```

Без этого шага bind-mount `./data`, созданный Docker'ом, принадлежит root, а процесс в контейнере работает от uid 10001 и не сможет писать `bot.db`.

```bash
cp .env.example .env   # заполнить ключи
docker compose up -d --build
```

`docker-compose.yml` монтирует `./data` в `/app/data` внутри контейнера —
там живёт `bot.db`. Каталог `data/` в `.gitignore`, в репозиторий не
попадает. Контейнер ничего не слушает наружу (long polling, открытых портов
нет).

Для продакшен-деплоя через готовый образ из GHCR (см. `PLAN.md`, этап 1.5)
задайте `GHCR_OWNER` и `IMAGE_TAG` в окружении или в `.env` вместо флага
`--build`.

## Деплой

Продакшен — VPS (Hetzner, Ubuntu) с публичным IP, деплой push-моделью по SSH
из GitHub Actions при каждом пуше в `main` (см. `PLAN.md`, этап 1.5).
Образ собирается и публикуется в `ghcr.io/tslava/trolobot`. Ничего не слушает
наружу — long polling, firewall на хосте пропускает только SSH.

### GitHub Secrets (Settings → Secrets and variables → Actions)

Заводятся в environment `production`:

| Secret | Значение |
|---|---|
| `SSH_HOST` | IP или домен VPS |
| `SSH_USER` | `bot` |
| `SSH_KEY` | приватный ключ деплоя (парный к ключу в `authorized_keys` на хосте) |
| `SSH_KNOWN_HOSTS` | вывод `ssh-keyscan -H <host>` |

Секретов приложения (`BOT_TOKEN`, `OPENROUTER_API_KEY` и т.д.) в CI нет и быть
не должно — они живут только в `/opt/trolobot/.env` на хосте.

### Первый выкат

1. На хосте под sudo: `sudo bash deploy/setup-host.sh --pubkey-file deploy_key.pub`
   (ставит docker, пользователя `bot`, `/opt/trolobot`, `deploy.env`,
   ограниченный SSH-ключ, ufw, cron-бэкап). Скрипт идемпотентен, можно
   перезапускать.
2. Заполнить `/opt/trolobot/.env` на хосте (скопирован пустым из
   `.env.example`, права 600) — `BOT_TOKEN`, `OPENROUTER_API_KEY`,
   `GOOGLE_PLACES_KEY`, `ADMIN_USER_ID`, `ALLOWED_CHAT_ID`.
3. Если пакет `ghcr.io/tslava/trolobot` приватный — залогинить `bot` в GHCR
   (`docker login ghcr.io`, PAT с `read:packages`), либо сделать пакет
   публичным в настройках GHCR. Без этого `docker compose pull` на хосте не
   сработает.
4. Завести секреты выше в GitHub, создать environment `production`.
5. Push в `main` (или `workflow_dispatch` на вкладке Actions) — прогонит
   тесты, соберёт и запушит образ, выполнит `deploy/deploy.sh` на хосте по SSH.
6. Проверить логи: ключ деплоя (`SSH_KEY`) — forced-command, пропускает
   только `bash -s -- sha-xxx` (см. `deploy/deploy-only.sh`), им `docker compose
   logs` не выполнить. Владелец заходит на хост **своим** пользователем и
   ключом, а дальше смотрит логи от имени `bot`:
   ```bash
   ssh owner@host "sudo -u bot docker compose -f /opt/trolobot/docker-compose.yml --env-file /opt/trolobot/deploy.env logs -f"
   ```
7. Проверить ключевой вход владельца (шаг выше), затем вручную отключить
   парольный и root-доступ по SSH — `deploy/setup-host.sh` этого намеренно
   не делает, чтобы не отрезать доступ до проверки. На хосте, под sudo, в
   `/etc/ssh/sshd_config`: `PasswordAuthentication no`, `PermitRootLogin
   prohibit-password` (или `no`), затем `systemctl restart sshd`.
8. Бэкапы `bot.db` (cron из `setup-host.sh`) по умолчанию пишутся в
   `/opt/trolobot/backups` на том же диске, что и `data/` — при потере диска
   бэкап пропадает вместе с БД. Для бэкапа на отдельном диске: подключить
   Hetzner Volume к серверу, смонтировать его в `/opt/trolobot/backups`
   (перенести туда существующие файлы перед первым монтированием, если они
   уже есть). Это ручной шаг, `setup-host.sh` том не создаёт и не монтирует.

### Откат руками

Без нового пуша — руками на хосте, тоже от имени владельца (свой пользователь
и ключ; ключ деплоя для этого не годится, он пропускает только форму
`bash -s -- sha-xxx`, см. выше):

```bash
# вариант 1: зайти на хост и вручную отредактировать IMAGE_TAG
ssh owner@host
sudo -u bot -i
cd /opt/trolobot && nano deploy.env   # выставить нужный IMAGE_TAG=sha-xxxxxxx
docker compose --env-file deploy.env up -d

# вариант 2: прогнать deploy.sh с явным тегом, от имени bot, со своей
# (проверенной) локальной копии репозитория, без захода на хост интерактивно
ssh owner@host "sudo -u bot bash -s -- sha-xxxxxxx" < deploy/deploy.sh
```

`deploy/deploy.sh` сам откатится на `IMAGE_TAG_PREV`, если новый тег не стал
healthy за 60 секунд, или если `docker compose pull`/`up -d` не отработали —
ручной откат нужен только для более старых версий.

## Приватность

Бот хранит сообщения чата **30 дней** (`message_retention_days` в
`config.yaml`), затем они удаляются ретеншн-джобом. При каждом ответе бот
отправляет последние 30 сообщений чата во внешний LLM API (OpenRouter).
Участникам чата стоит об этом знать заранее.

## Разработка

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest
```

Все четыре команды должны быть зелёными перед сдачей изменений.
