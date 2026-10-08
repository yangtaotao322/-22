"""Offline Mock for the web-controlled XiaoYou speaker flow."""
from dataclasses import dataclass
from enum import Enum
import argparse, time, uuid

class Scenario(str, Enum):
    AUTH_FAIL = "auth_fail"
    NOT_READY = "not_ready"
    SPEAK_ACCEPTED = "speak_accepted"
    TIMEOUT = "timeout"
    INTERRUPT = "interrupt"
    AUDIO_OPEN_FAILED = "audio_open_failed"

@dataclass
class MockResult:
    uuid: str; request_type: str; accepted: bool; success: bool | None; state: str; message: str

class MockRobot:
    def __init__(self, scenario="normal", playback_delay=0.02):
        self.scenario, self.playback_delay = scenario, playback_delay
        self.events, self.authorized = [], False
        self.ready = scenario != Scenario.NOT_READY.value
        self.active_request = None

    def _event(self, name, **data):
        event = {"event": name, "ts": round(time.time(), 3), **data}; self.events.append(event); return event

    def authorize(self):
        ok = self.scenario != Scenario.AUTH_FAIL.value
        self.authorized = ok; self._event("authorize", success=ok, message="mock authorization succeeded" if ok else "mock authorization failed"); return ok

    def health(self):
        result = {"authorized": self.authorized, "ready": self.ready, "simulated": True}; self._event("health", **result); return result

    def open_audio(self):
        ok = self.scenario != Scenario.AUDIO_OPEN_FAILED.value; self._event("audio_open", success=ok); return ok

    def speak(self, text): return self._request("play_text", text=text)

    def interrupt(self):
        request_id = self.active_request or str(uuid.uuid4()); self._event("interrupt", uuid=request_id); self.active_request = None
        return MockResult(request_id, "interrupt_action_audio", True, True, "INTERRUPTED", "mock interrupted")

    def _request(self, request_type, **payload):
        request_id = str(uuid.uuid4()); self.active_request = request_id; self._event("request", uuid=request_id, request_type=request_type, **payload)
        if self.scenario == Scenario.TIMEOUT.value:
            self._event("timeout", uuid=request_id, request_type=request_type); return MockResult(request_id, request_type, False, False, "FAILED", "mock timeout")
        self._event("accepted", uuid=request_id, request_type=request_type)
        if self.scenario == Scenario.SPEAK_ACCEPTED.value: return MockResult(request_id, request_type, True, None, "ACCEPTED", "mock accepted")
        time.sleep(self.playback_delay); self._event("result", uuid=request_id, request_type=request_type, success=True); self.active_request = None
        return MockResult(request_id, request_type, True, True, "COMPLETED", "mock completed")

def run(scenario):
    robot = MockRobot(scenario); print(f"scenario={scenario}"); robot.authorize(); robot.health()
    if scenario == Scenario.AUDIO_OPEN_FAILED.value: robot.open_audio()
    else:
        print(robot.speak("这是网页启动对话后的离线 Mock 播报"))
        if scenario == Scenario.INTERRUPT: print(robot.interrupt())
    for event in robot.events: print(event)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("scenario", nargs="?", default="normal", choices=["normal"] + [s.value for s in Scenario]); run(parser.parse_args().scenario)
