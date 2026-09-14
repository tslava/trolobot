# RUNBOOK — эксплуатация trolobot

Операционная книжка владельца. Что делать руками, когда и с телефона. Устройство
кода — `CLAUDE.md`, поведение персонажа — `CHARACTER.md`, установка и первый
деплой — `README.md`. Здесь — только то, что происходит после того, как бот уже
работает (PLAN.md, этап 7).

Все команды владельца — в личке с ботом, ответ приходит туда же, `/why [hours]`
не длиннее 168 (неделя).

---

## Ежедневно

Ничего. Бот работает сам, дебаунс/ретеншн/бэкап — фоновые таски и cron.

## Раз в неделю

В личке с ботом:

- `/status` — паника, стоп, версии промпта/few-shot, модели, shadow, счётчики
  дня (ambient/mention/llm_calls/llm_spent/reactions/stickers), pending, night queue.
- `/why 168` — сводка причин молчания за неделю (`stage:reason count`),
  отсортировано по частоте. Главный сигнал: одна причина резко доминирует —
  повод разбираться раньше месячного цикла. `send:sticker` — реплика ушла
  стикером, а не текстом; частое `send:sticker` рядом с малым числом реплик —
  повод снизить `behaviour.stickers.daily_cap` или поднять
  `min_replies_between` через `/set`.
- Проверить баланс OpenRouter ([openrouter.ai/credits](https://openrouter.ai/credits)) —
  без пополнения запросы режутся по длине задолго до нуля на счету (см.
  «Инциденты с телефона» ниже).

На хосте (свой пользователь и ключ, не ключ деплоя — см. README, «Первый выкат»,
п.6):

```bash
ssh owner@host "sudo -u bot docker compose -f /opt/trolobot/docker-compose.yml \
  --env-file /opt/trolobot/deploy.env logs --since 7d | grep -c WARNING"
```

Заметный рост числа WARNING неделя к неделе — читать сами строки (`logs --since 7d`
без `grep -c`), не игнорировать: ротация логов (`docker-compose.yml`, `max-size: 10m`,
`max-file: 5`) держит только последние ~50 МБ, старое пропадёт само.

```bash
ssh owner@host "sudo -u bot du -h /opt/trolobot/data/bot.db"
```

Быстрый рост размера между неделями (не объяснимый обычной активностью чата) —
повод проверить, что ретеншн-джоб (`retention.py`, раз в час, `message_retention_days`
из `config.yaml`, по умолчанию 30 дней) действительно отрабатывает: смотреть лог
на строки `retention purge: messages=...`.

## Раз в месяц

**1. Разбор `filter_log` и правка промпта.**

```bash
ssh owner@host
cd /opt/trolobot
sqlite3 data/bot.db "
  SELECT stage, reason, COUNT(*) AS n
  FROM filter_log
  WHERE verdict = 'cut' AND created_at >= strftime('%s','now','-30 days')
  GROUP BY stage, reason
  ORDER BY n DESC;
"
```

(Остановка контейнера не нужна — SQLite в `WAL`, чтение параллельно с рабочим
процессом безопасно; это именно чтение `SELECT`, не правка руками — см. «Чего
никогда не делать».) Пока `filters.shadow: true` (дефолт), сюда попадает и то,
что реально ушло в чат, — сама эта таблица и есть база для решения про shadow
(раздел ниже).

По результату — правка `prompts/system.txt` (структура ответа, лишние срезы)
или `few_shot.yaml` (примеры голоса) в репозитории → обычный `git push` в `main`
→ деплой по CI (README, «Деплой»). Версии не трогать руками: на старте контейнера
`PromptStore.load()` сравнивает тело файла с активной версией в БД и сам заводит
новую («сид»), если файл отличается — для `system.txt` и для `few_shot.yaml`
одинаково. После деплоя в личке:

```
/prompt
```

убедиться, что версия увеличилась и тело — то самое новое. Неудачная правка —
`/rollback <версия>` (см. «Инциденты» ниже), а не второй пуш с догадками.

**2. Обновление кэша заведений.**

`places_manual.yaml` копируется в образ вместе с `config.yaml` и `few_shot.yaml`, поэтому правки `quiet`/`category`/`district` доезжают на прод обычным пушем в `main`. Прогон наполнения — внутри контейнера:

```bash
ssh owner@host
cd /opt/trolobot
sudo -u bot docker compose -f docker-compose.yml --env-file deploy.env \
  exec bot python -m trolobot.places_fill --dry-run   # сначала просмотреть fact, ничего не пишет
sudo -u bot docker compose -f docker-compose.yml --env-file deploy.env \
  exec bot python -m trolobot.places_fill              # настоящий прогон, пишет в data/bot.db
```

Обязательно просмотреть колонку `fact` в напечатанной таблице (отзыв — чужой
текст из интернета, канал непрямой инъекции; `validate_fact` — последний
автоматический рубеж, не единственный) — вручную поправить, что криво сжалось:

```bash
sqlite3 data/bot.db "SELECT name, fact FROM places;"
```

Место, которое в этом прогоне не нашлось у Google, тихо помечается
`operational=0` и бот перестаёт его называть — ничего дополнительно делать не
нужно. Без ключа Google — `--manual-only` (см. README, «Заведения», п.3).

**3. Обновление каталога стикеров.**

Нужно только после того, как набор стикеров в Telegram поменялся (добавлены/
убраны стикеры). `stickers.yaml` копируется в образ вместе с `config.yaml` —
правки `text`/`when`/`enabled`, сделанные руками в репозитории, доезжают на
прод обычным пушем. Пересобрать каталог из набора — внутри контейнера:

```bash
ssh owner@host
cd /opt/trolobot
sudo -u bot docker compose -f docker-compose.yml --env-file deploy.env \
  exec bot python -m trolobot.stickers_fill <имя_набора> --dry-run   # сначала посмотреть таблицу
sudo -u bot docker compose -f docker-compose.yml --env-file deploy.env \
  exec bot python -m trolobot.stickers_fill <имя_набора>             # настоящий прогон, пишет stickers.yaml
```

Скрипт мержит с уже существующим `stickers.yaml`: правки владельца (`text`,
`when`, `enabled`) у уже известных стикеров (по `file_id`) не теряются, новые
получают следующие id, пропавшие из набора остаются в файле, но выключаются
(`enabled: false`). Результат — **обязательно проверить руками** (`text`/`when`
для новых стикеров — черновик модели со зрением, не то, что реально написано)
и закоммитить `stickers.yaml` в `main`, иначе следующий деплой откатит правки
к тому, что было в репозитории. Перезапуск контейнера не обязателен —
`stickers.yaml` читается на старте, но текущий прогон уже что-то поменял на
диске внутри контейнера, а не в репозитории.

Выключить стикеры совсем, без правки каталога: `/set behaviour.stickers.enabled
false` (обратно — `/set behaviour.stickers.enabled true`) или руками выключить
отдельные (`enabled: false` в `stickers.yaml`, коммит и деплой). В `/why`
`send:sticker` — реплика ушла стикером; `react:error`/иные `stage:reason` со
стикерами не связаны, это реакции-эмодзи (другой модуль, см. выше).

**4. Проверка бэкапов.**

```bash
ssh owner@host "ls -lh /opt/trolobot/backups"
```

Должно быть до 8 файлов `bot-YYYY-MM-DD.db` (cron `0 3 * * 0`, `deploy/setup-host.sh`
хранит последние 8 = ~2 месяца), самый свежий — не старше недели. Пусто или
давно не обновляется — cron не выполнился: `crontab -u bot -l` на хосте и
`/opt/trolobot/backups/backup.log`. Бэкап по умолчанию лежит на том же диске,
что и `data/` — на отдельном Hetzner Volume только если это настроено руками
(README, «Первый выкат», п.8); если не настроено, отдельно напомнить себе, что
при потере диска бэкап пропадёт вместе с БД.

---

## Инциденты с телефона

Всё — из личного чата с ботом, кроме `/stop` и `/mute` (они в общем чате,
доступны без реплая любому участнику).

| Симптом | Действие |
|---|---|
| Бот начал писать не то | `/stop` в чате (любой участник, 24 часа тишины без вопросов) или `/panic` в личке (навсегда, до `/resume`) |
| Бот молчит, а должен говорить | `/why 1` (причина последнего часа), `/status` (panic? stop_until? бюджет дня?), `/get llm.` (лимиты и текущие модели). Причина `llm:circuit_open` в `/why` — предохранитель после серии ошибок API, сам отпустит через `llm.circuit_pause_min` минут, руками не снимается |
| Бот молчит на все обращения, в `/why 1` видны `llm:http` или `llm:circuit_open` | (а) OpenRouter без пополненного баланса режет длину запроса примерно 3400 токенами — ошибка 402 «Prompt tokens limit exceeded» (видна как `llm:http`), лечится пополнением баланса, см. [openrouter.ai/credits](https://openrouter.ai/credits); (б) пять ошибок подряд (`llm.circuit_errors`) открывают предохранитель на 30 минут (`llm.circuit_pause_min`) — снимается `/resume` или само по таймеру; (в) после снятия неотвеченные за это время обращения не повторяются — тем, кому бот не ответил, надо написать ему заново |
| Бот шлёт мусор в формате (markdown, эмодзи, слишком длинно) | `/set filters.shadow false` — включает реальную резку (по умолчанию `shadow: true` только логирует, не режет) |
| Отвечает слишком часто | `/set behaviour.ambient_probability 0.05` (или ниже) |
| Неудачная правка промпта | `/rollback <версия>` — номер смотреть в `/prompt` |
| Кто-то попросил не трогать его | Он сам `/mute` в чате, без реплая (мьютит себя). Замьютить другого может только владелец, реплаем на его сообщение |
| Реакции-эмодзи раздражают или их слишком много | `/set behaviour.reactions.enabled false` — выключает совсем; `/set behaviour.reactions.probability 0.05` — просто реже. `/why` покажет `react:error`, если реакции в чате запрещены администратором группы (Telegram → настройки чата → разрешить реакции) — это не баг бота, чинится в настройках чата, не в `/set` |

---

## Восстановление

**Контейнер не стартует после деплоя.**

```bash
ssh owner@host "sudo -u bot docker compose -f /opt/trolobot/docker-compose.yml \
  --env-file /opt/trolobot/deploy.env logs --tail 200"
```

CI сам откатывает на предыдущий тег, если новый не стал healthy за 60 секунд
(healthcheck — `getMe` к Telegram API). Откат руками, если нужно вернуться
дальше на версию или CI недоступен — README, «Откат руками»: отредактировать
`IMAGE_TAG` в `/opt/trolobot/deploy.env` и `docker compose --env-file deploy.env
up -d`, либо прогнать `deploy/deploy.sh` с явным `sha-xxxxxxx` по SSH со своей
проверенной копии репозитория. Ключ деплоя (`SSH_KEY` в GitHub Secrets) для
ручных операций не годится — это forced-command, пропускает только форму
`bash -s -- sha-xxx`.

**Потерян `.env` на хосте.**

Живёт только в `/opt/trolobot/.env` (права 600), в git и в CI его никогда не
было. Состав — README, «Первый выкат», п.2:

| Ключ | Где перевыпустить |
|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/token` (или `/revoke` + новый бот, если токен скомпрометирован) |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) — новый ключ, снова поставить `credit limit` |
| `GOOGLE_PLACES_KEY` | Google Cloud Console → Credentials — новый ключ, снова ограничить по API (Places API (New)) |
| `ADMIN_USER_ID`, `ALLOWED_CHAT_ID` | Если тоже потеряны — очистить `ALLOWED_CHAT_ID` (discovery mode, README, «Discovery mode») и перечитать `user_id`/`chat_id` из лога уровня `WARNING` |

Файл копируется на хост один раз руками (`setup-host.sh` создаёт его пустым из
`.env.example`), CI его не трогает и не может восстановить.

**Восстановить БД из бэкапа.**

```bash
ssh owner@host
cd /opt/trolobot
sudo -u bot docker compose --env-file deploy.env down     # остановить контейнер
sudo cp backups/bot-2026-09-03.db data/bot.db             # выбрать нужную дату из ls backups/
sudo chown 10001:10001 data/bot.db                        # uid процесса в контейнере
sudo -u bot docker compose --env-file deploy.env up -d    # запустить заново
```

Останавливать контейнер перед копированием обязательно — иначе поверх бэкапа
тут же снова пишет активный процесс.

---

## Выключение shadow mode

Дефолт — `filters.shadow: true`: слои фильтра (regex/dedup/judge) только
считают и пишут в `filter_log`, ничего не режут. Критерий выключения:

1. Бот отработал в shadow минимум неделю.
2. `/why 168` в личке — посмотреть долю причин `regex:*`, `dedup:*`, `judge:*`
   среди всех `verdict='cut'` за неделю (тот же `filter_log`, что и в месячном
   разборе, просто на недельном окне).
3. Если суммарно срезалось бы **больше 30%** реплик — сначала чинить правила
   (частые ложные `regex:venue`/`regex:latin` — расширить `filters.places_whitelist`
   / `filters.polish_words` в `config.yaml`; частый `judge:*` — смотреть,
   не слишком ли строг промпт судьи), потом повторно смотреть `/why 168` через
   неделю. Выключать `shadow` при высоком false positive rate — значит резать
   живые реплики, а не мусор.
4. Только когда доля меньше 30% и причины выглядят как настоящие срезы (эхо,
   утечка промпта, реальный мусор) — `/set filters.shadow false`.

Пока `shadow: true` и задан `llm.judge_model`, расход LLM удваивается
(судья вызывается почти на каждую сгенерированную реплику, включая уже
срезанные слоями 1–2) — учитывать при планировании `credit limit` на ключе
OpenRouter (README, «Бюджет в shadow-режиме»).

---

## Чего никогда не делать

- Открывать порты наружу. Бот работает на long polling, входящих портов не
  требует; когда дойдёт до веб-панели (PLAN.md, этап 8) — строго `127.0.0.1`
  и SSH-туннель, не публичный порт.
- Копировать `data/` (или бэкапы `bot.db`) в облако. Это архив переписки
  шестнадцати живых людей — бэкап живёт на хосте (или отдельном примонтированном
  диске того же сервера), не дальше.
- Вшивать секреты в образ. `Dockerfile` их не содержит и не должен; все ключи —
  только через `/opt/trolobot/.env` на хосте, `env_file` в `docker-compose.yml`.
- Править БД руками на проде без остановки контейнера. Активный процесс и
  ручной `sqlite3 ... UPDATE/DELETE` в одну и ту же `bot.db` — гонка и риск
  битой записи; `SELECT` для разбора (см. «Раз в месяц», «Раз в неделю») —
  можно и без остановки, любая запись — только после `docker compose down`.
