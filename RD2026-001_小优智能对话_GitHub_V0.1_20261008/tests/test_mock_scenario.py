import unittest
from ubtech.mock_scenario import MockRobot


class MockScenarioTests(unittest.TestCase):
    def test_normal_playback_state_sequence(self):
        r = MockRobot()
        self.assertTrue(r.authorize())
        result = r.speak("hello")
        self.assertTrue(result.accepted)
        self.assertTrue(result.success)
        self.assertEqual([e["event"] for e in r.events], ["authorize", "request", "accepted", "result"])

    def test_failures_and_interrupt(self):
        self.assertFalse(MockRobot("auth_fail").authorize())
        self.assertFalse(MockRobot("not_ready").health()["ready"])
        self.assertEqual(MockRobot("timeout").speak("x").state, "FAILED")
        r = MockRobot("interrupt")
        r.speak("x")
        self.assertEqual(r.interrupt().state, "INTERRUPTED")

    def test_microphone_failure(self):
        self.assertFalse(MockRobot("audio_open_failed").open_audio())


if __name__ == "__main__":
    unittest.main()
