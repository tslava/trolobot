"""Загрузка config.yaml с применением плоских overrides из БД (config_overrides)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args, get_origin

import yaml
from annotated_types import Ge, Gt, Le, Lt
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from trolobot.config_models import Config


@dataclass(frozen=True)
class KeyInfo:
    """Карточка одного ключа конфига для /get <ключ> (CLAUDE.md, «справка по ключам»)."""

    key: str
    value: str
    default: str
    overridden: bool
    type_name: str
    bounds: str
    description: str
    settable: bool


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


def _format_value(value: object) -> str:
    """Питоновское значение листового поля -> строка, единый формат для /get и /set."""
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
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value
        ]
        return yaml.safe_dump(
            plain, default_flow_style=True, allow_unicode=True, width=10_000
        ).strip()
    return str(value)


def flatten_config(cfg: Config) -> dict[str, str]:
    """Config -> плоский словарь "section.key" -> строка, для будущей команды /get."""
    result: dict[str, str] = {}

    def _walk(prefix: str, obj: Any) -> None:
        if isinstance(obj, BaseModel):
            for name in obj.__class__.model_fields:
                child = getattr(obj, name)
                _walk(f"{prefix}.{name}" if prefix else name, child)
        else:
            result[prefix] = _format_value(obj)

    _walk("", cfg)
    return result


def _type_name(annotation: Any) -> str:
    """Аннотация поля -> короткое имя типа для карточки /get <ключ>."""
    origin = get_origin(annotation)
    if origin is None:
        if isinstance(annotation, type):
            return annotation.__name__
        return str(annotation)
    args = get_args(annotation)
    if origin is list:
        inner = _type_name(args[0]) if args else "Any"
        return f"list[{inner}]"
    if origin is tuple:
        inner = ", ".join(_type_name(arg) for arg in args)
        return f"tuple[{inner}]"
    return str(origin)


def _format_bound(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _bounds_str(field_info: FieldInfo) -> str:
    """Границы поля из annotated_types-метаданных Field(ge=..., le=...).

    "0..50" при заданных обеих границах, ">=1"/"<=5" при одной, "" без границ.
    """
    ge = gt = le = lt = None
    for constraint in field_info.metadata:
        if isinstance(constraint, Ge):
            ge = constraint.ge
        elif isinstance(constraint, Gt):
            gt = constraint.gt
        elif isinstance(constraint, Le):
            le = constraint.le
        elif isinstance(constraint, Lt):
            lt = constraint.lt
    lo = ge if ge is not None else gt
    hi = le if le is not None else lt
    if lo is not None and hi is not None:
        return f"{_format_bound(lo)}..{_format_bound(hi)}"
    if lo is not None:
        return f">={_format_bound(lo)}"
    if hi is not None:
        return f"<={_format_bound(hi)}"
    return ""


def describe_key(
    current: Config, base: Config, overrides: dict[str, str], key: str
) -> KeyInfo | None:
    """Карточка одного ключа конфига: тип, границы, описание, текущее и дефолтное значение.

    None, если такого ключа нет, либо key указывает на секцию (BaseModel), а не на лист
    (обход по model_fields, как в flatten_config).
    """
    parts = key.split(".")
    model_cls: type[BaseModel] = Config
    field_info: FieldInfo | None = None
    for index, part in enumerate(parts):
        fields = model_cls.model_fields
        if part not in fields:
            return None
        field_info = fields[part]
        annotation = field_info.annotation
        is_last = index == len(parts) - 1
        if is_last:
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                return None
            break
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            return None
        model_cls = annotation

    if field_info is None:
        return None

    current_value: Any = current
    default_value: Any = base
    for part in parts:
        current_value = getattr(current_value, part)
        default_value = getattr(default_value, part)

    return KeyInfo(
        key=key,
        value=_format_value(current_value),
        default=_format_value(default_value),
        overridden=key in overrides,
        type_name=_type_name(field_info.annotation),
        bounds=_bounds_str(field_info),
        description=field_info.description or "",
        settable=not key.startswith("persona."),
    )
