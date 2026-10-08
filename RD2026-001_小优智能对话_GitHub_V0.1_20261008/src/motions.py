"""Only operator-approved IDs present on this robot can be selected."""
import json
from pathlib import Path


class MotionPlanner:
    def __init__(self, path, available_ids):
        self.mapping = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(self.mapping, dict) or any(not isinstance(v, str) for v in self.mapping.values()):
            raise ValueError("Motion map must contain string values")
        unknown = set(self.mapping.values()) - {""} - set(available_ids)
        if unknown:
            raise ValueError("Motion IDs absent on this robot: " + ", ".join(sorted(unknown)))

    def choose(self, answer):
        key = "neutral"
        if any(word in answer for word in ("抱歉", "不能", "不可以")):
            key = "sorry"
        elif any(word in answer for word in ("你好", "欢迎", "高兴", "谢谢")):
            key = "happy"
        elif any(word in answer for word in ("可以", "好的", "是的")):
            key = "nod"
        return self.mapping.get(key) or self.mapping.get("neutral", "")
