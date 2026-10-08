import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from collections import deque
from src.dialogue_core import DialogueCore
from src.motions import MotionPlanner
from src.knowledge_base import KnowledgeBase
from ubtech.ubtech_adapter import UbtechRos2Adapter, UbtechServiceError


class Tests(unittest.TestCase):
    def test_ros_message_field(self):
        a = UbtechRos2Adapter.__new__(UbtechRos2Adapter)
        a.StringCall = SimpleNamespace(Request=lambda: SimpleNamespace())
        a._client = lambda *args: SimpleNamespace(call_async=lambda req: req)
        a._result = lambda *args: SimpleNamespace(success=True, message='{"ok":true,"data":{}}')
        self.assertTrue(a.call_string("/test", {})["ok"])

    def test_service_rejection(self):
        a = UbtechRos2Adapter.__new__(UbtechRos2Adapter)
        a.StringCall = SimpleNamespace(Request=lambda: SimpleNamespace())
        a._client = lambda *args: SimpleNamespace(call_async=lambda req: req)
        a._result = lambda *args: SimpleNamespace(success=False, message='{"ok":true}')
        with self.assertRaises(UbtechServiceError):
            a.call_string("/test", {})

    def test_missing_ok_rejected(self):
        with self.assertRaises(UbtechServiceError):
            UbtechRos2Adapter._require_ok("/test", {})

    def test_all_documented_wrappers(self):
        a = UbtechRos2Adapter.__new__(UbtechRos2Adapter)
        seen = []
        a.call_trigger = lambda service, require_ok=True: seen.append(("trigger", service)) or {"ok": True}
        a.call_string = lambda service, params: seen.append(("string", service, params)) or {"ok": True}
        a.serial_number(); a.system_version(); a.soft_version()
        a.set_wakeup_followup(True); a.get_wakeup_followup()
        a.set_wakeup_enabled(False); a.get_wakeup_enabled()
        a.set_vision_enabled(True); a.get_vision_enabled()
        a.set_face_recognition_enabled(False); a.get_face_recognition_enabled()
        a.open_audio_stream(); a.audio_stream_state(); a.close_audio_stream()
        a.open_video_stream(); a.video_stream_state(); a.close_video_stream()
        self.assertEqual(len(seen), 17)

    def test_play_text_does_not_embed_action(self):
        a = UbtechRos2Adapter.__new__(UbtechRos2Adapter)
        captured = {}
        a._play = lambda service, request_type, params, wait, timeout: captured.update(params) or SimpleNamespace()
        a.speak("你好", action="A029")
        self.assertEqual(captured, {"text": "你好", "save": False})

    def test_event_must_match_uuid_and_type(self):
        import time
        a = UbtechRos2Adapter.__new__(UbtechRos2Adapter)
        a.node = None
        a.cancel_requested = lambda: False
        a.rclpy = SimpleNamespace(spin_once=lambda *args, **kwargs: None)
        event = lambda uid: {"data": {"uuid": uid, "request_type": "play_text", "phase": "result", "success": True}}
        a.events = deque((time.perf_counter(), event(uid)) for uid in [None, "wrong", "correct"])
        result = a._wait("correct", "play_text", time.perf_counter(), .1)
        self.assertEqual(result[2]["data"]["uuid"], "correct")

    def test_cancel_during_wait(self):
        import time
        a = UbtechRos2Adapter.__new__(UbtechRos2Adapter)
        a.cancel_requested = lambda: True
        calls = []
        a.interrupt = lambda: calls.append("interrupt")
        event = a._wait("id", "play_text", time.perf_counter(), 1)[2]
        self.assertEqual(calls, ["interrupt"])
        self.assertFalse(event["data"]["success"])

    def test_whitelist_and_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "map.json"
            path.write_text(json.dumps({"happy": "approved", "neutral": ""}))
            with self.assertRaises(ValueError):
                MotionPlanner(path, [])
            planner = MotionPlanner(path, ["approved"])
            self.assertEqual(planner.choose("你好"), "approved")
            self.assertEqual(planner.choose("今天下雨"), "")

    def test_original_core_history_preserved(self):
        seen = []
        llm = SimpleNamespace(ask=lambda q, c, h: (seen.append(list(h)) or "answer"))
        core = DialogueCore(SimpleNamespace(search=lambda *args, **kwargs: []), llm, max_history_rounds=2)
        for q in ["one", "two", "three"]:
            core.ask(q)
        self.assertEqual(len(core.history), 4)
        self.assertEqual(seen[1][0]["content"], "one")

    def test_knowledge_directory_loads_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "knowledge.json").write_text(json.dumps([
                {"question": "机械伊甸是什么？", "answer": "a"}
            ]), encoding="utf-8")
            (root / "mechanical_eden_knowledge.json").write_text(json.dumps([
                {"question": "机械伊甸是什么", "answer": "duplicate"},
                {"question": "机械伊甸在哪里？", "answer": "b"}
            ]), encoding="utf-8")
            self.assertEqual(len(KnowledgeBase(root).items), 2)


if __name__ == "__main__":
    unittest.main()
