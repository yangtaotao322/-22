import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.character_config import load_character, load_character_id, list_characters
from src.knowledge_base import KnowledgeBase


class CharacterConfigTests(unittest.TestCase):
    def test_libai_loads_and_is_explicitly_isolated(self):
        cfg = load_character(ROOT / "characters")
        self.assertEqual(cfg.character_id, "libai")
        self.assertEqual(cfg.name, "李白")
        self.assertIn("李白", cfg.opening["zh"])
        self.assertTrue(cfg.persona_prompt)
        self.assertEqual(cfg.knowledge_path.name, "knowledge.json")
        kb = KnowledgeBase(cfg.knowledge_path)
        self.assertTrue(any("李白" in item["answer"] for item in kb.search("你是谁", top_k=3)))
        self.assertFalse(any("机械伊甸" in item["answer"] for item in kb.items))

    def test_test_character_switches_without_code_change(self):
        selector = ROOT / "characters/current_character.json"
        original = selector.read_text(encoding="utf-8")
        try:
            selector.write_text(json.dumps({"current_character": "test_character"}), encoding="utf-8")
            cfg = load_character(ROOT / "characters")
            self.assertEqual(cfg.name, "测试讲解员")
            self.assertIn("测试讲解员", cfg.opening["zh"])
            kb = KnowledgeBase(cfg.knowledge_path)
            self.assertTrue(kb.search("测试展厅开放时间", top_k=1))
            hits = kb.search("李白是谁", top_k=3)
            self.assertFalse(any("李白" in item["answer"] for item in hits))
        finally:
            selector.write_text(original, encoding="utf-8")

    def test_missing_files_and_unknown_id_fail_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "current_character.json").write_text('{"current_character":"missing"}', encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_character(base)

    def test_five_demo_characters_are_complete_and_isolated(self):
        expected = {"libai", "liqingzhao", "teacher", "companion", "sichuan"}
        discovered = {item.character_id for item in list_characters(ROOT / "characters")}
        self.assertTrue(expected.issubset(discovered))
        for character_id in expected:
            cfg = load_character_id(ROOT / "characters", character_id)
            self.assertEqual(cfg.character_id, character_id)
            self.assertTrue(cfg.opening["zh"])
            self.assertGreaterEqual(len(cfg.fixed_scenarios), 5)
            self.assertTrue(KnowledgeBase(cfg.knowledge_path).items)
            self.assertGreaterEqual(float(cfg.voice.get("pcm_gain", 0)), 2.0)

    def test_new_demo_fixed_answers_are_exactly_five(self):
        for character_id in ("liqingzhao", "teacher", "companion", "sichuan"):
            cfg = load_character_id(ROOT / "characters", character_id)
            self.assertEqual(len(cfg.fixed_scenarios), 5)
            self.assertTrue(all(scene.get("fixed_answer") for scene in cfg.fixed_scenarios))


if __name__ == "__main__":
    unittest.main()
