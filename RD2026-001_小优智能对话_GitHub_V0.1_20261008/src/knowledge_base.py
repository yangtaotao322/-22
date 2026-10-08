"""Optional lightweight lexical retrieval; no embedding model download needed."""
import json
import re
from pathlib import Path


class KnowledgeBase:
    def __init__(self, path):
        path = Path(path)
        files = sorted(path.glob("*knowledge.json")) if path.is_dir() else [path]
        merged = {}
        for file in files:
            values = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(values, list):
                raise ValueError(f"Knowledge must be a list: {file}")
            for item in values:
                question = str(item.get("question", "")).strip()
                answer = str(item.get("answer", "")).strip()
                if not question or not answer:
                    continue
                key = re.sub(r"[\s？?。！!，,]", "", question)
                merged.setdefault(key, item)
        self.items = list(merged.values())

    def search(self, query, top_k=2):
        def grams(text):
            return {text[i:i+2] for i in range(len(text)-1)}
        terms = grams(query)
        scored = [(len(terms & grams(item.get("question", ""))), item) for item in self.items]
        return [item for score, item in sorted(scored, key=lambda x: x[0], reverse=True)[:top_k] if score > 0]
