"""Загрузка config.yaml с применением плоских overrides из БД (config_overrides)."""

from pathlib import Path
from typing import Any, get_origin

import yaml
from pydantic import BaseModel, ValidationError

from trolobot.config_models import Config


def _resolve_field_annotation(model: type[BaseModel], parts: list[str], full_key: str) -> Any:
    """Проверяет, что путь parts существует в модели, и возвращает аннотацию листа."""
    field_name = parts[0]
    fields = model.model_fields
    if field_name not in fields:
        raise ValueError(
            f"unknown config key: {full_key!r} (no field {field_name!r} in {model.__name__})"
        )
    annotation = fields[field_name].annotation
    if len(parts) == 1:
        return annotation
    if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
        raise ValueError(
            f"unknown config key: {full_key!r} ({'.'.join(parts)!r} is not a config section)"
        )
    return _resolve_field_annotation(annotation, parts[1:], full_key)


def _convert_override_value(annotation: Any, raw_value: str, key: str) -> Any:
    """Строка -> питоновское значение. Списки/bool идут через yaml.safe_load,
    остальное остаётся строкой и приводится типом самим pydantic."""
    origin = get_origin(annotation)
    needs_yaml = annotation is bool or origin in (list, tuple)
    if not needs_yaml:
        return raw_value
    try:
        parsed = yaml.safe_load(raw_value)
    except yaml.YAMLError as exc:
        raise ValueError(
            f"invalid value for override {key!r}: {raw_value!r} is not valid YAML"
        ) from exc
    return parsed


def _set_nested(data: dict[str, Any], parts: list[str], value: Any) -> None:
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def load_config(path: Path, overrides: dict[str, str] | None = None) -> Config:
    """Читает yaml, накатывает плоские overrides ("section.key" -> "value") и валидирует.

    Невалидный override или неизвестный ключ -> ValueError с ключом в тексте.
    """
    data: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise ValueError(f"config file {path} must contain a mapping at top level")
            data = loaded

    if overrides:
        for key, raw_value in overrides.items():
            parts = key.split(".")
            annotation = _resolve_field_annotation(Config, parts, key)
            value = _convert_override_value(annotation, raw_value, key)
            _set_nested(data, parts, value)

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        lines = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()]
        raise ValueError("invalid config: " + "; ".join(lines)) from exc


def flatten_config(cfg: Config) -> dict[str, str]:
    """Config -> плоский словарь "section.key" -> строка, для будущей команды /get."""
    result: dict[str, str] = {}

    def _format(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if hasattr(value, "isoformat"):
            return str(value.isoformat())
        if isinstance(value, (list, tuple)):
            plain = [
                item.model_dump(mode="json") if isinstance(item, BaseModel) else item
                for item in value
            ]
            return yaml.safe_dump(plain, default_flow_style=True, allow_unicode=True).strip()
        return str(value)

    def _walk(prefix: str, obj: Any) -> None:
        if isinstance(obj, BaseModel):
            for name in obj.__class__.model_fields:
                child = getattr(obj, name)
                _walk(f"{prefix}.{name}" if prefix else name, child)
        else:
            result[prefix] = _format(obj)

    _walk("", cfg)
    return result
