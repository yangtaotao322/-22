"""Deterministic robot output for migration tests without hardware."""
import time
import uuid
from .ubtech_adapter import PlaybackResult


class MockUbtechAdapter:
    def __init__(self, playback_delay=0.01):
        self.playback_delay = playback_delay
        self.spoken_texts, self.actions = [], []

    def health_check(self):
        return {"authorized": True, "ready": True, "simulated": True}

    def _play(self, request_type, wait):
        started = time.perf_counter(); request_id = str(uuid.uuid4())
        if wait: time.sleep(self.playback_delay)
        elapsed = (time.perf_counter() - started) * 1000
        return PlaybackResult(
            request_id, request_type, True, wait, True, message="mock completed" if wait else "accepted",
            request_ms=elapsed, first_event_ms=elapsed if wait else None,
            playback_ms=elapsed if wait else None,
            raw_response={"ok": True, "data": {"uuid": request_id}},
            raw_event={"ok": True, "data": {"uuid": request_id, "phase": "result", "success": True}} if wait else {},
        )

    def speak(self, text, action="", wait=True, timeout=None):
        if not text.strip(): raise ValueError("text must not be empty")
        self.spoken_texts.append({"text": text.strip(), "action": action})
        return self._play("play_text", wait)

    def play_action(self, action, wait=True, timeout=None):
        if not action.strip(): raise ValueError("action must not be empty")
        self.actions.append(action.strip()); return self._play("play_action", wait)

    def interrupt(self): return {"ok": True, "code": "OK", "data": {}}
    def close(self): pass
