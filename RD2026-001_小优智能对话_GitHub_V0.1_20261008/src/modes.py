"""Eight user-facing Xiaoyou dialogue modes."""
import json
from pathlib import Path


class ModeManager:
    def __init__(self, path: Path):
        self.styles = json.loads(path.read_text(encoding="utf-8"))
        self.current = "甜美模式"

    @property
    def instruction(self):
        return self.styles[self.current]["instruction"]

    @property
    def opening(self):
        return self.styles[self.current]["opening"]

    def switch_from_text(self, text: str):
        if not any(word in text for word in ("切换", "进入", "换成", "使用", "开启")):
            return None
        for name, config in self.styles.items():
            candidates = [name] + config.get("aliases", [])
            if any(candidate in text for candidate in candidates):
                self.current = name
                return name
        return None
