"""Concrete ROS 2 RobotAdapter for the UBTECH/优世界 SDK."""
from __future__ import annotations

import json
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path


AUTH = "/robo/auth/call/authorize"
AUTH_STATE = "/robo/auth/call/auth_state"
READY = "/robo/system/call/get_ready_state"
SERIAL_NUMBER = "/robo/system/call/get_serial_number"
SYSTEM_VERSION = "/robo/system/call/get_system_version"
SOFT_VERSION = "/robo/system/call/get_soft_version"
SET_WAKEUP_FOLLOWUP = "/robo/system/call/set_wakeup_followup"
GET_WAKEUP_FOLLOWUP = "/robo/system/call/get_wakeup_followup"
SET_WAKEUP_ENABLED = "/robo/system/call/set_wakeup_enabled"
GET_WAKEUP_ENABLED = "/robo/system/call/get_wakeup_enabled"
SET_VISION_ENABLED = "/robo/system/call/set_vision_enabled"
GET_VISION_ENABLED = "/robo/system/call/get_vision_enabled"
SET_FACE_RECOGNITION_ENABLED = "/robo/system/call/set_face_recognition_enabled"
GET_FACE_RECOGNITION_ENABLED = "/robo/system/call/get_face_recognition_enabled"
PLAY_TEXT = "/robo/audio/call/play_text"
PLAY_ACTION = "/robo/audio/call/play_action"
INTERRUPT = "/robo/audio/call/interrupt_action_audio"
MOTIONS = "/robo/audio/call/get_motion_info_list"
PLAYBACK = "/robo/media/subscribe/playback_state"
AUDIO_OPEN = "/robo/audio/call/open_stream"
AUDIO_STATE = "/robo/audio/call/stream_state"
AUDIO_CLOSE = "/robo/audio/call/close_stream"
VIDEO_OPEN = "/robo/video/call/open_stream"
VIDEO_STATE = "/robo/video/call/stream_state"
VIDEO_CLOSE = "/robo/video/call/close_stream"

READY_STATE = "/robo/system/subscribe/ready_state"
MAIN_WAKEUP_WORD = "/robo/audio/subscribe/main_wakeup_word"
WAKEUP_EVENT = "/robo/audio/subscribe/wakeup_event"
WAKEUP_STATE = "/robo/audio/subscribe/wakeup_state"
DOA_EVENT = "/robo/audio/subscribe/doa_event"
VIDEO_METADATA = "/robo/video/subscribe/metadata"

EVENT_TOPICS = (
    READY_STATE, PLAYBACK, MAIN_WAKEUP_WORD, WAKEUP_EVENT,
    WAKEUP_STATE, DOA_EVENT, VIDEO_METADATA,
)


class UbtechServiceError(RuntimeError):
    def __init__(self, service, message, payload=None):
        super().__init__(f"{service}: {message}")
        self.service, self.payload = service, payload or {}


def parse_envelope(raw):
    try:
        value = json.loads(str(raw or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("SDK returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("SDK envelope must be an object")
    return value


@dataclass(frozen=True)
class UbtechCredentials:
    appid: str
    api_key: str
    api_secret: str
    license_text: str
    device_id: str

    @classmethod
    def from_env(cls):
        license_text = os.getenv("UBTECH_LICENSE_TEXT", "").strip()
        license_file = os.getenv("UBTECH_LICENSE_FILE", "").strip()
        if license_file:
            license_text = Path(license_file).expanduser().read_text(encoding="utf-8").strip()
        values = {
            "appid": os.getenv("UBTECH_APPID", "").strip(),
            "api_key": os.getenv("UBTECH_API_KEY", "").strip(),
            "api_secret": os.getenv("UBTECH_API_SECRET", "").strip(),
            "license_text": license_text,
            "device_id": os.getenv("UBTECH_DEVICE_ID", "").strip(),
        }
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ValueError("missing UBTECH credentials: " + ", ".join(missing))
        return cls(**values)

    def request(self):
        return {
            "appid": self.appid, "api_key": self.api_key,
            "api_secret": self.api_secret, "license": self.license_text,
            "device_id": self.device_id,
        }


@dataclass
class PlaybackResult:
    request_id: str
    request_type: str
    accepted: bool
    completed: bool
    success: bool
    code: int = 0
    message: str = ""
    request_ms: float = 0.0
    first_event_ms: float | None = None
    playback_ms: float | None = None
    raw_response: dict | None = None
    raw_event: dict | None = None


class UbtechRos2Adapter:
    def __init__(self, service_timeout=3.0, playback_timeout=45.0):
        os.environ.setdefault("ROS_DOMAIN_ID", "20")
        # Do not force CycloneDDS; let rclpy choose an installed RMW by default.
        try:
            import rclpy
            from rclpy.qos import QoSProfile, ReliabilityPolicy
            from robo_sdk.srv import StringCall
            from std_msgs.msg import String
            from std_srvs.srv import Trigger
        except ImportError as exc:
            raise RuntimeError("source ROS 2 Humble and the vendor robo_sdk package first") from exc
        self.rclpy, self.StringCall, self.Trigger = rclpy, StringCall, Trigger
        self.service_timeout, self.playback_timeout = service_timeout, playback_timeout
        self.owns_rclpy = not rclpy.ok()
        if self.owns_rclpy:
            rclpy.init(args=None)
        self.node = rclpy.create_node("ubtech_dialogue_adapter")
        self.clients, self.events = {}, deque()
        self.topic_events = {topic: deque(maxlen=200) for topic in EVENT_TOPICS}
        self.event_callbacks = {topic: [] for topic in EVENT_TOPICS}
        self.cancel_requested = lambda: False
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE)
        self.subscriptions = {
            topic: self.node.create_subscription(
                String, topic, lambda msg, current=topic: self._on_topic(current, msg), qos
            ) for topic in EVENT_TOPICS
        }

    def _on_topic(self, topic, message):
        try:
            event = parse_envelope(message.data)
        except ValueError:
            event = {"ok": False, "code": "INVALID_EVENT", "data": {}, "raw": message.data}
        stamp = time.perf_counter()
        self.topic_events[topic].append((stamp, event))
        if topic == PLAYBACK:
            self.events.append((stamp, event))
        for callback in tuple(self.event_callbacks.get(topic, ())):
            callback(event)

    def _on_playback(self, message):
        self._on_topic(PLAYBACK, message)

    def _client(self, service, service_type):
        key = (service, service_type)
        if key not in self.clients:
            client = self.node.create_client(service_type, service)
            if not client.wait_for_service(timeout_sec=self.service_timeout):
                raise UbtechServiceError(service, "service unavailable")
            self.clients[key] = client
        return self.clients[key]

    def _result(self, service, future):
        self.rclpy.spin_until_future_complete(self.node, future, timeout_sec=self.service_timeout)
        if not future.done():
            raise UbtechServiceError(service, "service timeout")
        if future.exception():
            raise UbtechServiceError(service, type(future.exception()).__name__)
        return future.result()

    @staticmethod
    def _require_ok(service, envelope):
        if envelope.get("ok") is not True:
            raise UbtechServiceError(service, envelope.get("message") or envelope.get("code") or "failed", envelope)
        return envelope

    def call_string(self, service, params):
        client = self._client(service, self.StringCall)
        request = self.StringCall.Request()
        request.params = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        response = self._result(service, client.call_async(request))
        if not response.success:
            raise UbtechServiceError(service, "ROS service rejected request")
        return self._require_ok(service, parse_envelope(response.message))

    def call_trigger(self, service, require_ok=True):
        client = self._client(service, self.Trigger)
        response = self._result(service, client.call_async(self.Trigger.Request()))
        envelope = parse_envelope(response.message)
        if require_ok and not response.success:
            raise UbtechServiceError(service, "ROS service rejected request")
        return self._require_ok(service, envelope) if require_ok else envelope

    def authorize(self, credentials): return self.call_string(AUTH, credentials.request())
    def authorize_from_env(self): return self.authorize(UbtechCredentials.from_env())
    def auth_state(self): return self.call_trigger(AUTH_STATE, require_ok=False)
    def ready_state(self): return self.call_trigger(READY)
    def serial_number(self): return self.call_trigger(SERIAL_NUMBER)
    def system_version(self): return self.call_trigger(SYSTEM_VERSION)
    def soft_version(self): return self.call_trigger(SOFT_VERSION)
    def set_wakeup_followup(self, enabled): return self.call_string(SET_WAKEUP_FOLLOWUP, {"enabled": bool(enabled)})
    def get_wakeup_followup(self): return self.call_trigger(GET_WAKEUP_FOLLOWUP)
    def set_wakeup_enabled(self, enabled): return self.call_string(SET_WAKEUP_ENABLED, {"enabled": bool(enabled)})
    def get_wakeup_enabled(self): return self.call_trigger(GET_WAKEUP_ENABLED)
    def set_vision_enabled(self, enabled): return self.call_string(SET_VISION_ENABLED, {"enabled": bool(enabled)})
    def get_vision_enabled(self): return self.call_trigger(GET_VISION_ENABLED)
    def set_face_recognition_enabled(self, enabled):
        return self.call_string(SET_FACE_RECOGNITION_ENABLED, {"enabled": bool(enabled)})
    def get_face_recognition_enabled(self): return self.call_trigger(GET_FACE_RECOGNITION_ENABLED)
    def interrupt(self): return self.call_trigger(INTERRUPT)
    def get_motion_info_list(self): return self.call_string(MOTIONS, {})
    def open_audio_stream(self): return self.call_trigger(AUDIO_OPEN)
    def audio_stream_state(self): return self.call_trigger(AUDIO_STATE)
    def close_audio_stream(self): return self.call_trigger(AUDIO_CLOSE)
    def open_video_stream(self): return self.call_trigger(VIDEO_OPEN)
    def video_stream_state(self): return self.call_trigger(VIDEO_STATE)
    def close_video_stream(self): return self.call_trigger(VIDEO_CLOSE)

    def on_event(self, topic, callback):
        if topic not in self.event_callbacks:
            raise ValueError(f"unsupported event topic: {topic}")
        self.event_callbacks[topic].append(callback)

    def recent_events(self, topic):
        if topic not in self.topic_events:
            raise ValueError(f"unsupported event topic: {topic}")
        return [event for _, event in self.topic_events[topic]]

    def spin_events(self, seconds=1.0):
        deadline = time.perf_counter() + max(0.0, float(seconds))
        while time.perf_counter() < deadline:
            self.rclpy.spin_once(self.node, timeout_sec=min(0.1, deadline - time.perf_counter()))

    def health_check(self):
        auth = self.auth_state()
        authorized = bool(auth.get("data", {}).get("authorized"))
        if not authorized:
            return {"authorized": False, "ready": False, "auth": auth}
        ready = self.ready_state()
        data = ready.get("data", {})
        is_ready = bool(data.get("ready", data.get("state") in ("READY", "ready", True)))
        return {"authorized": True, "ready": is_ready, "auth": auth, "ready_response": ready}

    def system_snapshot(self):
        return {
            "auth": self.auth_state(),
            "ready": self.ready_state(),
            "serial_number": self.serial_number(),
            "system_version": self.system_version(),
            "soft_version": self.soft_version(),
            "wakeup_followup": self.get_wakeup_followup(),
            "wakeup_enabled": self.get_wakeup_enabled(),
            "vision_enabled": self.get_vision_enabled(),
            "face_recognition_enabled": self.get_face_recognition_enabled(),
            "audio_stream": self.audio_stream_state(),
            "video_stream": self.video_stream_state(),
        }

    def _wait(self, request_id, request_type, started, timeout):
        deadline, first_ms = time.perf_counter() + timeout, None
        while time.perf_counter() < deadline:
            if self.cancel_requested():
                self.interrupt()
                return first_ms, (time.perf_counter() - started) * 1000, {
                    "data": {"uuid": request_id, "phase": "result", "success": False,
                             "message": "cancelled by operator"}}
            self.rclpy.spin_once(self.node, timeout_sec=0.1)
            while self.events:
                event_time, event = self.events.popleft()
                data = event.get("data", {})
                if data.get("uuid") != request_id:
                    continue
                if data.get("request_type") != request_type:
                    continue
                if first_ms is None:
                    first_ms = (event_time - started) * 1000
                if str(data.get("phase", "")).lower() == "result":
                    return first_ms, (event_time - started) * 1000, event
        return first_ms, None, None

    def _play(self, service, request_type, params, wait, timeout):
        request_id = str(params.get("uuid") or uuid.uuid4())
        params["uuid"] = request_id
        self.events.clear()
        started = time.perf_counter()
        response = self.call_string(service, params)
        request_ms = (time.perf_counter() - started) * 1000
        request_id = str(response.get("data", {}).get("uuid") or request_id)
        accepted = bool(response.get("data", {}).get("accepted", response.get("ok")))
        if not wait:
            return PlaybackResult(request_id, request_type, accepted, False, accepted,
                                  message=response.get("message", ""), request_ms=request_ms,
                                  raw_response=response, raw_event={})
        first_ms, complete_ms, event = self._wait(request_id, request_type, started, timeout or self.playback_timeout)
        data = (event or {}).get("data", {})
        completed = complete_ms is not None
        if not completed:
            self.interrupt()
        return PlaybackResult(
            request_id, request_type, accepted, completed,
            completed and bool(data.get("success")), int(data.get("code", 0) or 0),
            str(data.get("message") or ("playback timeout" if not completed else "playback failed")),
            request_ms, first_ms, complete_ms, response, event or {},
        )

    def speak(self, text, action="", wait=True, timeout=None):
        text = text.strip()
        if not text:
            raise ValueError("text must not be empty")
        params = {"text": text, "save": False}
        # SDK v0.0.8 keeps action/motion on play_text only for compatibility;
        # it does not execute the action. Call play_action separately.
        return self._play(PLAY_TEXT, "play_text", params, wait, timeout)

    def play_action(self, action, wait=True, timeout=None):
        action = action.strip()
        if not action:
            raise ValueError("action must not be empty")
        return self._play(PLAY_ACTION, "play_action", {"action": action}, wait, timeout)

    def close(self):
        if self.node:
            self.node.destroy_node(); self.node = None
        if self.owns_rclpy and self.rclpy.ok():
            self.rclpy.shutdown()
