"""Robot-independent knowledge retrieval and LLM dialogue core."""

from dataclasses import dataclass
from time import perf_counter
from typing import Any, List, Optional


@dataclass
class DialogueReply:
    question: str
    answer: str
    kb_ms: float
    llm_ms: float
    retrieved_count: int


class DialogueCore:
    """Reusable dialogue logic shared by Unitree and UBTECH adapters."""

    def __init__(
        self,
        knowledge_base: Any,
        llm_client: Any,
        top_k: int = 2,
        max_history_rounds: int = 3,
    ) -> None:
        self.knowledge_base = knowledge_base
        self.llm_client = llm_client
        self.top_k = top_k
        self.max_history_rounds = max_history_rounds
        self.history: List[dict] = []

    def ask(self, question: str, external_context: str = "") -> DialogueReply:
        normalized = question.strip()
        if not normalized:
            raise ValueError("question must not be empty")

        started = perf_counter()
        results = self.knowledge_base.search(normalized, top_k=self.top_k)
        kb_ms = (perf_counter() - started) * 1000
        context = "\n".join(item.get("answer", "") for item in results)
        if external_context:
            context = (context + "\n" + external_context).strip()

        started = perf_counter()
        answer = self.llm_client.ask(normalized, context, self.history)
        llm_ms = (perf_counter() - started) * 1000

        self.history.extend(
            [
                {"role": "user", "content": normalized},
                {"role": "assistant", "content": answer},
            ]
        )
        keep = self.max_history_rounds * 2
        if len(self.history) > keep:
            del self.history[: len(self.history) - keep]

        return DialogueReply(
            question=normalized,
            answer=answer,
            kb_ms=kb_ms,
            llm_ms=llm_ms,
            retrieved_count=len(results),
        )

    def clear_history(self) -> None:
        self.history.clear()
