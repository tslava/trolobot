-- trolobot schema, этап 1 (PLAN.md) + таблицы этапа 6.
-- Применяется целиком через executescript, когда PRAGMA user_version == 0.

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    tg_message_id INTEGER,
    chat_id INTEGER,
    user_id INTEGER,
    display_name TEXT,
    text TEXT,                       -- медиа без текста: [фото], [стикер], [голосовое]
    reply_to_tg_message_id INTEGER,
    is_bot INTEGER,
    created_at INTEGER
);

CREATE TABLE bot_replies (
    id INTEGER PRIMARY KEY,
    tg_message_id INTEGER,           -- своё сообщение: нужно для реакций и реплаев на него
    reply_to_tg_message_id INTEGER,  -- NULL, если в поток
    trigger TEXT,                    -- mention | reply | name | ambient | places | morning | spontaneous
    trigger_tg_message_id INTEGER,   -- что вызвало
    text TEXT,
    prompt_version INTEGER,
    few_shot_version INTEGER,
    delay_sec INTEGER,               -- фактическая пауза от триггера до отправки
    created_at INTEGER
);

CREATE TABLE state (
    key TEXT PRIMARY KEY,
    -- panic, stop_until, topic_cooldown_until,
    -- last_ambient_at, ambient_count:<YYYY-MM-DD>,
    -- last_mention_reply_at, last_mention_reply_at:<user_id>, mention_count:<YYYY-MM-DD>,
    -- spontaneous_count:<YYYY-Www>,
    -- llm_calls:<YYYY-MM-DD>, llm_spent_usd:<YYYY-MM-DD>, llm_error_streak, llm_circuit_until
    -- счётчики с датой в ключе: сброса в полночь не нужно, старые ключи чистит ретеншн
    value TEXT
);

CREATE TABLE night_queue (           -- обращения, пришедшие в quiet_window; переживают рестарт
    id INTEGER PRIMARY KEY,
    tg_message_id INTEGER,
    user_id INTEGER,
    display_name TEXT,
    text TEXT,
    created_at INTEGER,
    answered_at INTEGER              -- NULL пока не отвечено; утренний джоб ставит одним апдейтом
);

CREATE TABLE pending_replies (       -- отложенные ответы на обращения; переживают рестарт
    id INTEGER PRIMARY KEY,
    trigger_tg_message_id INTEGER,
    user_id INTEGER,
    trigger TEXT,                    -- mention | reply | name
    due_at INTEGER,
    created_at INTEGER,
    done_at INTEGER                  -- NULL пока не отправлено или не отменено
);

CREATE TABLE muted_users (
    user_id INTEGER PRIMARY KEY,
    display_name TEXT,
    muted_by INTEGER,
    created_at INTEGER
);

CREATE TABLE config_overrides (      -- /set; приоритет над config.yaml
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at INTEGER
);

CREATE TABLE places (
    place_id TEXT PRIMARY KEY,
    name TEXT,
    district TEXT,
    category TEXT,         -- craft | cheap | outskirts
    rating REAL,
    reviews INTEGER,
    price_level INTEGER,
    quiet INTEGER,         -- проставляется руками, Google этого не знает
    fact TEXT,             -- один сжатый факт из отзывов
    operational INTEGER,
    refreshed_at INTEGER
);

CREATE TABLE filter_log (            -- каждый отказ на любой стадии; источник для /why
    id INTEGER PRIMARY KEY,
    trigger_tg_message_id INTEGER,
    candidate_text TEXT,             -- реплика бота на выходном фильтре; NULL на гейте
    verdict TEXT,                    -- pass | cut
    stage TEXT,                      -- gate | llm | regex | dedup | judge | send
    reason TEXT,                     -- gate:is_bot, gate:night, gate:night_queued, gate:logistics,
                                      -- gate:injection, gate:not_live, gate:cooldown, gate:cap, gate:dice,
                                      -- llm:timeout, llm:http, llm:invalid_json, llm:budget, llm:circuit_open,
                                      -- regex:length, regex:echo, regex:prompt_leak, regex:model_talk,
                                      -- dedup:jaccard, dedup:polish_freq, judge:risky, judge:obeyed,
                                      -- send:restart, send:recheck_stop, send:recheck_night ...
    shadow INTEGER,
    created_at INTEGER
);

-- Этап 6: управление из телеграма.

CREATE TABLE config_audit (
    id INTEGER PRIMARY KEY, key TEXT, old_value TEXT, new_value TEXT,
    changed_by INTEGER, created_at INTEGER
);

CREATE TABLE prompt_versions (
    version INTEGER PRIMARY KEY, body TEXT, note TEXT, active INTEGER, created_at INTEGER
);

CREATE TABLE few_shot_versions (
    version INTEGER PRIMARY KEY, body_yaml TEXT, note TEXT, active INTEGER, created_at INTEGER
);

-- События жизни персонажа (/life, CLAUDE.md "события жизни") — память, не
-- переписка: retention.py её не трогает, чистит только /life rm.
CREATE TABLE life_events (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    announced_at INTEGER,
    announced_tg_message_id INTEGER
);

-- Долгая память чата (CLAUDE.md, "долгая память чата") — пересказ прошедших
-- разговоров по периодам. Живёт дольше самих сообщений: retention.py чистит её
-- по своему сроку (behaviour.chat_memory.keep_days), а не по message_retention_days.
CREATE TABLE chat_memory (
    id INTEGER PRIMARY KEY,
    period_start INTEGER NOT NULL,   -- unix, включительно
    period_end INTEGER NOT NULL,     -- unix, исключительно
    text TEXT NOT NULL,              -- пересказ, несколько строк
    created_at INTEGER NOT NULL
);

CREATE INDEX idx_messages_chat_created ON messages (chat_id, created_at);
CREATE INDEX idx_messages_tg_message_id ON messages (tg_message_id);
CREATE INDEX idx_bot_replies_created ON bot_replies (created_at);
CREATE INDEX idx_filter_log_created ON filter_log (created_at);
CREATE INDEX idx_night_queue_answered ON night_queue (answered_at);
CREATE INDEX idx_pending_replies_done_due ON pending_replies (done_at, due_at);
CREATE INDEX idx_chat_memory_period_end ON chat_memory (period_end);

PRAGMA user_version = 3;
