"""Small adapter around UBTECH's native face tracking and VIEW_FOLLOW APIs.

This module deliberately contains no vision algorithm and no servo control. It
only reads the vendor face-results topic and calls the documented services.
"""
from __future__ import annotations

import json
import re
import select
import subprocess
import threading
import time
from pathlib import Path


class NativeGazeManager:
    def __init__(self, log_path: str = "/tmp/xiaoyou_voice.log"):
        self.log_path = Path(log_path)
        self._topic = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state = "OFF"
        self.face_id = None
        self.state_path = Path("/tmp/xiaoyou_gaze_state.json")
        self._write_state()

    def _write_state(self):
        """Publish a small, self-owned snapshot for fixed scene decisions."""
        payload = {"gaze_state": self.state, "face_id": self.face_id,
                   "updated_at": time.time()}
        try:
            self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    def _log(self, text: str):
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(text + "\n")

    @staticmethod
    def _call(container, service, kind, body, timeout=10):
        script = ("source /opt/walker/entrypoint.sh; timeout 8 "
                  "/opt/rosa/rosa_cli/bin/rosa service call --no-daemon "
                  f"{service} {kind} '{body}'")
        try:
            r = subprocess.run(["docker", "exec", container, "bash", "-lc", script],
                               capture_output=True, text=True, timeout=timeout, check=False)
            output = r.stdout + r.stderr
            return output, ("code=0" in output or "success=True" in output or "Motion mode set to" in output)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc), False

    def _set_vision(self, enabled):
        # Official SDK bridge: StringCall.params carries the JSON envelope.
        params = json.dumps({"enabled": bool(enabled)}, separators=(",", ":"))
        body = json.dumps({"params": params}, separators=(",", ":"))
        return self._call("walker-registry.registry-1", "/robo/system/call/set_vision_enabled",
                          "robo_sdk/srv/StringCall", body)

    def _set_tracking(self, mode, face_id):
        return self._call("walker-perception.perception-1",
                          "/vision/vision_perception_controller/face_tracking_mode_command",
                          "vision_msgs/srv/FaceTrackingModeCommand",
                          json.dumps({"track_mode": mode, "face_id": face_id}))

    def _set_motion(self, mode):
        return self._call("walker-motion.motion-1", "/mc/set_uworld_motion_mode",
                          "uworld_motion_msgs/srv/SetMotionMode",
                          json.dumps({"motion_mode": {"mode_type": mode}}))

    def _read_faces(self, deadline):
        script = ("source /opt/walker/entrypoint.sh; exec "
                  "/opt/rosa/rosa_cli/bin/rosa topic echo --no-daemon "
                  "/vision/vision_perception_controller/face_results")
        self._topic = subprocess.Popen(["docker", "exec", "walker-perception.perception-1",
                                        "bash", "-lc", script], stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True,
                                       start_new_session=True)
        try:
            while time.monotonic() < deadline and not self._stop.is_set():
                remaining = max(0.05, deadline - time.monotonic())
                ready, _, _ = select.select([self._topic.stdout], [], [], remaining)
                if not ready:
                    break
                line = self._topic.stdout.readline()
                if not line:
                    break
                match = re.search(r"face_id:\s*(-?\d+)", line)
                if match:
                    face_id = int(match.group(1))
                    if face_id >= 0:
                        return face_id
        finally:
            self._stop_topic()
        return None

    def _stop_topic(self):
        if self._topic is not None and self._topic.poll() is None:
            self._topic.terminate()
            try:
                self._topic.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._topic.kill()
                self._topic.wait(timeout=1)
        self._topic = None

    def enable(self):
        with self._lock:
            self._stop.clear()
            self.state = "NATIVE_VIEW_FOLLOW_STARTING"
            self._write_state()
            self._log("VISION_GAZE_STATE: NATIVE_VIEW_FOLLOW_STARTING")
            out, ok = self._set_vision(True)
            self._log("VISION_ENABLE: " + ("OK " if ok else "VISION_GLOBAL_WARNING ") + out.strip())
            # Use the vendor's proven autonomous multi-face mode. Face results
            # are observational only and must never gate VIEW_FOLLOW.
            out, ok = self._set_tracking(0, -1)
            self._log("FACE_TRACKING_ENABLED: " + ("OK " if ok else "FAILED ") + out.strip())
            if not ok:
                self.state = "ERROR"
                return False
            out, ok = self._set_motion(3)
            self._log("VIEW_FOLLOW_ENABLED: " + ("OK " if ok else "FAILED ") + out.strip())
            self.state = "NATIVE_VIEW_FOLLOW_ACTIVE" if ok else "ERROR"
            self._write_state()
            if ok:
                self._log("NATIVE_VIEW_FOLLOW_ACTIVE")
                threading.Thread(target=self._observe_faces, name="face-results", daemon=True).start()
            return ok

    def _observe_faces(self):
        """Observe vendor results without controlling or gating the follower."""
        script = ("source /opt/walker/entrypoint.sh; exec "
                  "/opt/rosa/rosa_cli/bin/rosa topic echo --no-daemon "
                  "/vision/vision_perception_controller/face_results")
        proc = subprocess.Popen(["docker", "exec", "walker-perception.perception-1",
                                 "bash", "-lc", script], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        self._topic = proc
        try:
            while not self._stop.is_set():
                line = proc.stdout.readline()
                if not line:
                    self._log("FACE_RESULT_UNAVAILABLE")
                    break
                m = re.search(r"face_id:\s*(-?\d+)", line)
                if m:
                    self.face_id = int(m.group(1))
                    self._write_state()
                    self._log("FACE_RESULT face_id=" + str(self.face_id))
                for key in ("distance", "is_front_face", "center_x_offset"):
                    hit = re.search(rf"{key}:\s*([^\s]+)", line)
                    if hit:
                        self._log(f"FACE_RESULT {key}={hit.group(1)}")
        finally:
            self._stop_topic()

    def disable(self):
        with self._lock:
            self._stop.set()
            self._stop_topic()
            self._set_vision(False)
            self._set_motion(1)
            self.face_id = None
            self.state = "OFF"
            self._write_state()
            self._log("VISION_GAZE_STATE: OFF")
