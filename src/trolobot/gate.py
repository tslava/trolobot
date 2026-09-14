"""Гейт (этап 2): решает, рассматривать ли сообщение как повод для ответа.

Чистая функция без I/O. Состояние не меняет — изменения возвращаются в
``Decision.state_changes``, применяет их вызывающий код.

Порядок шагов (шаг 0 — фильтр по chat_id — уже в хендлере, до вызова гейта):

1.  ``is_bot``                              -> DROP ``gate:is_bot``
2.  ``panic`` / ``stop_until > now``         -> DROP ``gate:panic`` / ``gate:stop``
3.  автор в ``muted_user_ids``               -> DROP ``gate:muted``
4.  стоп-лист тем                            -> DROP ``gate:topic`` (+ topic_cooldown_until)
5.  ``topic_cooldown_until > now``           -> DROP ``gate:topic_cooldown``
5a. маркеры команд (инъекция)                -> DROP ``gate:injection``
6.  прямое обращение (reply > mention > name):
    ночь                                     -> QUEUE_NIGHT ``gate:night_queued``
    дневной лимит обращений                  -> DROP ``gate:mention_cap``
    иначе                                    -> PASS ``pass:<trigger>``

    Прямое обращение никогда не отбрасывается кулдауном (решение владельца):
    кулдаун по чату и по человеку (``mention_chat_cooldown_sec``/``mention_cooldown_sec``)
    здесь не проверяется вовсе — гейт всегда пропускает обращение дальше, а
    сам кулдаун превращается в задержку ответа на этапе 3 (``responder.py``,
    ``earliest`` в постановке/схлопывании pending).
7.  ночь (без обращения)                     -> DROP ``gate:night``
8.  логистика                                -> DROP ``gate:logistics``

    Горячее окно после ``/life``/``/say`` (``state.hot_until``, CLAUDE.md,
    "горячее окно"): пока оно открыто, шаги 9-11 заменяются на:
    9h. лимит ambient-реплик за окно         -> DROP ``gate:hot_cap``
    10h. кости с ``hot_window.ambient_probability`` -> DROP ``gate:dice``;
         иначе PASS ``pass:ambient_hot`` (шаг 9, "не живой разговор", пропускается).

    Вне окна — как раньше:
9.  не живой разговор                        -> DROP ``gate:not_live``
10. дневной лимит ambient / кулдаун чата     -> DROP ``gate:ambient_cap``
    / ``gate:ambient_cooldown``
11. кости                                    -> DROP ``gate:dice``; иначе PASS ``pass:ambient``
"""

from __future__ import annotations

import random

from trolobot.config_models import Config
from trolobot.gate_types import (
    Decision,
    GateMessage,
    GateState,
    PatternsLike,
    StateChange,
    Trigger,
    Verdict,
)
from trolobot.timeutil import in_window


def _drop(reason: str) -> Decision:
    return Decision(verdict=Verdict.DROP, trigger=None, reason=reason)


def _direct_trigger(msg: GateMessage, patterns: PatternsLike) -> Trigger | None:
    """Тип прямого обращения, приоритет reply > mention > name; None, если обращения нет."""
    if msg.reply_to_bot:
        return Trigger.REPLY
    if patterns.mentions_bot(msg.text):
        return Trigger.MENTION
    if patterns.name_trigger(msg.text) is not None:
        return Trigger.NAME
    return None


def _is_live(state: GateState, cfg: Config) -> bool:
    """Живой разговор: минимум сообщений и разных авторов за окно live_talk."""
    live = cfg.behaviour.live_talk
    if len(state.recent) < live.min_messages:
        return False
    distinct_users = {activity.user_id for activity in state.recent}
    return len(distinct_users) >= live.min_people


def _direct_address_decision(
    msg: GateMessage,
    state: GateState,
    cfg: Config,
    now: int,
    trigger: Trigger,
) -> Decision:
    """Шаг 6: свой бюджет для прямого обращения, логистика и live-talk его не касаются.

    Кулдаун по чату (``mention_chat_cooldown_sec``) и по человеку
    (``mention_cooldown_sec``) сюда больше не заходит: решение владельца — прямое
    обращение никогда не отбрасывается кулдауном, только дневной потолок и ночь.
    Кулдаун сдвигает момент ответа, а не отменяет его — это делает ``responder.py``
    (``earliest`` при постановке/схлопывании pending)."""
    behaviour = cfg.behaviour
    if in_window(now, cfg.persona.timezone, behaviour.quiet_window):
        return Decision(verdict=Verdict.QUEUE_NIGHT, trigger=trigger, reason="gate:night_queued")
    if state.mention_count_today >= behaviour.mention_daily_cap:
        return _drop("gate:mention_cap")
    return Decision(verdict=Verdict.PASS, trigger=trigger, reason=f"pass:{trigger.value}")


def should_consider(
    msg: GateMessage,
    state: GateState,
    cfg: Config,
    patterns: PatternsLike,
    now: int,
    rng: random.Random,
) -> Decision:
    """Решить, отвечать ли на сообщение. Порядок шагов — см. докстринг модуля."""
    behaviour = cfg.behaviour
    tz = cfg.persona.timezone

    # 1. другой бот
    if msg.is_bot:
        return _drop("gate:is_bot")

    # 2. паника / временная остановка
    if state.panic:
        return _drop("gate:panic")
    if state.stop_until is not None and state.stop_until > now:
        return _drop("gate:stop")

    # 3. заглушенный автор
    if msg.user_id in state.muted_user_ids:
        return _drop("gate:muted")

    # 4. стоп-лист тем на входе — ставит кулдаун
    if patterns.topic_stop(msg.text) is not None:
        cooldown_until = now + behaviour.topic_cooldown_min * 60
        return Decision(
            verdict=Verdict.DROP,
            trigger=None,
            reason="gate:topic",
            state_changes=(StateChange("topic_cooldown_until", str(cooldown_until)),),
        )

    # 5. кулдаун после стоп-листа
    if state.topic_cooldown_until is not None and state.topic_cooldown_until > now:
        return _drop("gate:topic_cooldown")

    # 5a. маркеры команд — молчание без кулдауна
    if patterns.injection(msg.text) is not None:
        return _drop("gate:injection")

    # 6. прямое обращение — свой бюджет, дальше шаги 7-11 не действуют
    trigger = _direct_trigger(msg, patterns)
    if trigger is not None:
        return _direct_address_decision(msg, state, cfg, now, trigger)

    # 7. ночное окно для ambient
    if in_window(now, tz, behaviour.quiet_window):
        return _drop("gate:night")

    # 8. логистический фильтр
    if patterns.logistics(msg.text) is not None:
        return _drop("gate:logistics")

    # Горячее окно после /life и /say (решение владельца, CLAUDE.md): пока оно
    # открыто, живость чата (шаг 9) не проверяется, а дневной лимит/кулдаун
    # ambient (шаг 10) заменяются своим бюджетом на окно и своей вероятностью.
    hot_window = behaviour.hot_window
    hot = hot_window.enabled and state.hot_until is not None and now < state.hot_until
    if hot:
        if state.hot_ambient_count >= hot_window.ambient_cap:
            return _drop("gate:hot_cap")
        if rng.random() >= hot_window.ambient_probability:
            return _drop("gate:dice")
        return Decision(verdict=Verdict.PASS, trigger=Trigger.AMBIENT, reason="pass:ambient_hot")

    # 9. живой разговор
    if not _is_live(state, cfg):
        return _drop("gate:not_live")

    # 10. дневной лимит ambient и кулдаун чата
    if state.ambient_count_today >= behaviour.daily_cap:
        return _drop("gate:ambient_cap")
    if (
        state.last_ambient_at is not None
        and state.last_ambient_at + behaviour.chat_cooldown_min * 60 > now
    ):
        return _drop("gate:ambient_cooldown")

    # 11. кости
    if rng.random() >= behaviour.ambient_probability:
        return _drop("gate:dice")
    return Decision(verdict=Verdict.PASS, trigger=Trigger.AMBIENT, reason="pass:ambient")
