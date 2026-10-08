"""Role/persona configuration loader for the self-owned Xiaoyou application."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


REQUIRED_FILES = ("character.json", "persona_prompt.md", "fixed_scenarios.json",
                  "knowledge.json", "expressions.json", "actions.json")


@dataclass(frozen=True)
class CharacterConfig:
    root: Path
    data: dict
    persona_prompt: str
    fixed_scenarios: object
    expressions: object
    actions: object

    @property
    def character_id(self): return self.data["character_id"]
    @property
    def name(self): return self.data["name"]
    @property
    def opening(self): return self.data["opening"]
    @property
    def knowledge_path(self): return self.root / self.data["paths"]["knowledge"]
    @property
    def modes_path(self):
        value = self.data.get("paths", {}).get("modes")
        return self.root / value if value else None
    @property
    def voice(self): return self.data.get("voice", {})
    @property
    def tts_instruction(self): return str(self.data.get("tts_instruction", "")).strip()


def load_character_id(base_dir: Path, character_id: str) -> CharacterConfig:
    """Load one isolated character directory without changing the selector."""
    base_dir = Path(base_dir)
    character_id = str(character_id).strip()
    if not character_id or Path(character_id).name != character_id:
        raise RuntimeError(f"非法 character_id: {character_id!r}")
    root = base_dir / character_id
    if not root.is_dir():
        raise RuntimeError(f"角色目录不存在: {root}")
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise RuntimeError(f"角色 {character_id} 缺少文件: {', '.join(missing)}")
    try:
        data = json.loads((root / "character.json").read_text(encoding="utf-8"))
        fixed = json.loads((root / "fixed_scenarios.json").read_text(encoding="utf-8"))
        expressions = json.loads((root / "expressions.json").read_text(encoding="utf-8"))
        actions = json.loads((root / "actions.json").read_text(encoding="utf-8"))
        prompt = (root / "persona_prompt.md").read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"角色 {character_id} 配置解析失败: {exc}") from exc
    for field in ("character_id", "name", "identity", "paths", "opening"):
        if not data.get(field):
            raise RuntimeError(f"角色 {character_id} 缺少必要字段: {field}")
    if data["character_id"] != character_id:
        raise RuntimeError(f"角色目录与 character_id 不一致: {character_id}")
    knowledge = root / str(data["paths"].get("knowledge", ""))
    if not knowledge.is_file():
        raise RuntimeError(f"角色知识库不存在: {knowledge}")
    if not prompt:
        raise RuntimeError(f"角色 Prompt 为空: {root / 'persona_prompt.md'}")
    return CharacterConfig(root, data, prompt, fixed, expressions, actions)


def load_character(base_dir: Path, current_file: Path | None = None) -> CharacterConfig:
    base_dir = Path(base_dir)
    current_file = current_file or base_dir / "current_character.json"
    try:
        selector = json.loads(current_file.read_text(encoding="utf-8"))
        character_id = str(selector["current_character"]).strip()
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(f"无法读取当前角色配置: {current_file}: {exc}") from exc
    return load_character_id(base_dir, character_id)


def list_characters(base_dir: Path) -> list[CharacterConfig]:
    """Discover only complete, valid character directories."""
    base_dir = Path(base_dir)
    characters = []
    for root in sorted((p for p in base_dir.iterdir() if p.is_dir()), key=lambda p: p.name):
        try:
            characters.append(load_character_id(base_dir, root.name))
        except RuntimeError:
            continue
    return characters


def select_character(base_dir: Path, character_id: str) -> CharacterConfig:
    """Validate first, then atomically select a character."""
    base_dir = Path(base_dir)
    character = load_character_id(base_dir, character_id)
    selector = base_dir / "current_character.json"
    temporary = base_dir / ".current_character.json.tmp"
    temporary.write_text(json.dumps({"current_character": character.character_id}, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    os.replace(temporary, selector)
    return character
