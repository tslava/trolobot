"""Тесты для trolobot.export_parser — маленький result.json собирается вручную."""

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trolobot.export_parser import ExportMessage, parse_export

TZ = "Europe/Warsaw"


def _dt(iso: str) -> int:
    """ISO без tz -> unix seconds в TZ, для сверки с created_at в тестах."""
    return int(datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(TZ)).timestamp())


def _write_export(tmp_path: Path, messages: list[dict]) -> Path:
    payload = {
        "name": "АлкоПознань",
        "type": "private_group",
        "id": 1234567890,
        "messages": messages,
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# Сырые записи экспорта, намеренно в перемешанном порядке (id/дата растут не по
# порядку в списке) — проверяет, что parse_export действительно сортирует,
# а не просто сохраняет порядок JSON.
_RAW_MESSAGES = [
    {
        "id": 8,
        "type": "message",
        "date": "2026-09-10T12:07:00",
        # from_id намеренно отсутствует -> user_id должен стать 0
        "from": "Гость",
        "text": "Без id отправителя",
    },
    {
        "id": 2,
        "type": "message",
        "date": "2026-09-10T12:01:00",
        "from": "Аня",
        "from_id": "user222",
        "text": [
            "Привет, ",
            {"type": "mention", "text": "@otec_fedor_bot"},
            "!",
        ],
    },
    {
        "id": 1,
        "type": "message",
        "date": "2026-09-10T12:00:00",
        "from": "Дима",
        "from_id": "user111",
        "text": "Привет всем",
    },
    {
        "id": 4,
        "type": "message",
        "date": "2026-09-10T12:03:00",
        "from": "Дима",
        "from_id": "user111",
        "photo": "photos/photo_4.jpg",
        "text": "",
    },
    {
        "id": 9,
        "type": "message",
        # ни "date", ни "date_unixtime" -> битая запись, пропускается с WARNING
        "from": "Дима",
        "from_id": "user111",
        "text": "Битое сообщение без даты",
    },
    {
        "id": 3,
        "type": "service",
        "date": "2026-09-10T12:02:00",
        "action": "pin_message",
        "text": "",
    },
    {
        "id": 6,
        "type": "message",
        "date": "2026-09-10T12:05:00",
        "from": "Дима",
        "from_id": "user111",
        "media_type": "voice_message",
        "text": "",
    },
    {
        "id": 5,
        "type": "message",
        "date": "2026-09-10T12:04:00",
        "from": "Аня",
        "from_id": "user222",
        "media_type": "sticker",
        "text": "",
    },
    {
        "id": 7,
        "type": "message",
        "date": "2026-09-10T12:06:00",
        "from": "Аня",
        "from_id": "user222",
        "reply_to_message_id": 1,
        "text": "Ответ на первое",
    },
    {
        "id": 10,
        "type": "message",
        # date заведомо не совпадает с date_unixtime — приоритет у unixtime
        "date": "2026-09-10T23:59:00",
        "date_unixtime": "1700000000",
        "from": "Дима",
        "from_id": "user111",
        "text": "С unixtime",
    },
]


def test_parse_export_full(tmp_path: Path) -> None:
    path = _write_export(tmp_path, _RAW_MESSAGES)
    result = parse_export(path, TZ)

    # service (id=3) и битое без даты (id=9) отброшены -> 8 сообщений из 10 записей.
    # id=10 использует date_unixtime=1700000000 (2023 год) — оно раньше остальных
    # по created_at, несмотря на то что "date" в записи указывает на 2026 год.
    assert [m.tg_message_id for m in result] == [10, 1, 2, 4, 5, 6, 7, 8]

    by_id: dict[int, ExportMessage] = {m.tg_message_id: m for m in result}

    # Обычное сообщение.
    msg1 = by_id[1]
    assert msg1.text == "Привет всем"
    assert msg1.display_name == "Дима"
    assert msg1.user_id == 111
    assert msg1.reply_to_tg_message_id is None
    assert msg1.created_at == _dt("2026-09-10T12:00:00")

    # list-text со склейкой сущности mention.
    msg2 = by_id[2]
    assert msg2.text == "Привет, @otec_fedor_bot!"
    assert msg2.user_id == 222

    # Фото без подписи -> плейсхолдер.
    msg4 = by_id[4]
    assert msg4.text == "[фото]"

    # Стикер -> плейсхолдер.
    msg5 = by_id[5]
    assert msg5.text == "[стикер]"

    # Голосовое -> плейсхолдер.
    msg6 = by_id[6]
    assert msg6.text == "[голосовое]"

    # Реплай сохраняет reply_to_message_id.
    msg7 = by_id[7]
    assert msg7.text == "Ответ на первое"
    assert msg7.reply_to_tg_message_id == 1

    # Без from_id -> user_id 0.
    msg8 = by_id[8]
    assert msg8.user_id == 0
    assert msg8.display_name == "Гость"

    # date_unixtime приоритетнее date.
    msg10 = by_id[10]
    assert msg10.created_at == 1700000000
    assert msg10.created_at != _dt("2026-09-10T23:59:00")

    # Сортировка по (created_at, id): ascending, несмотря на перемешанный JSON.
    assert [m.created_at for m in result] == sorted(m.created_at for m in result)


def test_service_messages_are_skipped(tmp_path: Path) -> None:
    path = _write_export(tmp_path, _RAW_MESSAGES)
    result = parse_export(path, TZ)
    assert 3 not in [m.tg_message_id for m in result]


def test_broken_record_without_date_is_skipped_with_warning(tmp_path: Path, caplog) -> None:
    path = _write_export(tmp_path, _RAW_MESSAGES)
    with caplog.at_level(logging.WARNING, logger="trolobot.export_parser"):
        result = parse_export(path, TZ)

    assert 9 not in [m.tg_message_id for m in result]
    assert any("date" in record.message for record in caplog.records)


def test_broken_record_without_id_is_skipped_with_warning(tmp_path: Path, caplog) -> None:
    messages = [
        {
            "type": "message",
            "date": "2026-09-10T12:00:00",
            "from": "Дима",
            "from_id": "user111",
            "text": "Без id",
        },
        {
            "id": 1,
            "type": "message",
            "date": "2026-09-10T12:00:00",
            "from": "Дима",
            "from_id": "user111",
            "text": "Нормальное",
        },
    ]
    path = _write_export(tmp_path, messages)
    with caplog.at_level(logging.WARNING, logger="trolobot.export_parser"):
        result = parse_export(path, TZ)

    assert [m.tg_message_id for m in result] == [1]
    assert any("id" in record.message for record in caplog.records)


def test_media_and_empty_text_without_placeholder_is_dropped(tmp_path: Path) -> None:
    """Нет текста и нет распознанного медиа -> сообщение молча пропускается."""
    messages = [
        {
            "id": 1,
            "type": "message",
            "date": "2026-09-10T12:00:00",
            "from": "Дима",
            "from_id": "user111",
            "text": "",
        }
    ]
    path = _write_export(tmp_path, messages)
    result = parse_export(path, TZ)
    assert result == []
