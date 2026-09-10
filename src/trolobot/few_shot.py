"""few_shot.yaml: загрузка примеров реплик персонажа и их рендер в промпт."""

import json
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator


class FewShot(BaseModel):
    name: str
    user: str
    speak: bool
    text: str = ""

    @model_validator(mode="after")
    def _check_text(self) -> "FewShot":
        if self.speak and not self.text.strip():
            raise ValueError("speak=true requires non-empty text")
        if not self.speak and self.text.strip():
            raise ValueError("speak=false requires empty text")
        return self


def load_few_shot(path: Path) -> list[FewShot]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        raise ValueError(f"few-shot file {path} must contain a YAML list")
    return [FewShot.model_validate(item) for item in loaded]


def render_few_shot(items: list[FewShot]) -> str:
    """Каждый пример: "Имя: реплика" и на следующей строке компактный JSON.
    Примеры разделяются пустой строкой (CHARACTER.md, раздел 5)."""
    blocks = []
    for item in items:
        payload = {"speak": item.speak, "text": item.text}
        line = json.dumps(payload, ensure_ascii=False, separators=(", ", ": "))
        blocks.append(f"{item.name}: {item.user}\n{line}")
    return "\n\n".join(blocks)
