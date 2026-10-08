"""Validation boundary for UBTECH shared-memory stream configuration and frames."""
from dataclasses import dataclass


REQUIRED_CONFIG = ("stream", "state", "path", "frame_payload_size", "max_frames")


def validate_stream_config(data, expected_stream="audio"):
    missing = [key for key in REQUIRED_CONFIG if key not in data]
    if missing:
        raise ValueError("stream config missing: " + ", ".join(missing))
    if data["stream"] != expected_stream:
        raise ValueError(f"expected {expected_stream} stream, got {data['stream']}")
    if data["state"] not in {"OPEN", "OPENED"}:
        raise ValueError("stream is not open")
    if not str(data["path"]).strip():
        raise ValueError("shared-memory path is empty")
    if int(data["frame_payload_size"]) <= 0 or int(data["max_frames"]) <= 0:
        raise ValueError("invalid stream capacity")
    return {
        "stream": data["stream"], "state": data["state"], "path": str(data["path"]),
        "frame_payload_size": int(data["frame_payload_size"]),
        "max_frames": int(data["max_frames"]),
    }


@dataclass(frozen=True)
class AudioFrame:
    sequence: int
    timestamp_ns: int
    payload_size: int
    payload: bytes


class AudioFrameTracker:
    def __init__(self, frame_payload_size):
        self.limit = int(frame_payload_size)
        self.last_sequence = None
        self.last_timestamp = None
        self.dropped_frames = 0

    def accept(self, frame):
        if frame.payload_size != len(frame.payload):
            raise ValueError("payload_size does not match payload")
        if frame.payload_size > self.limit:
            raise ValueError("payload exceeds frame_payload_size")
        if frame.sequence < 0 or frame.timestamp_ns < 0:
            raise ValueError("negative sequence or timestamp")
        if self.last_sequence is not None:
            if frame.sequence <= self.last_sequence:
                raise ValueError("sequence is not increasing")
            self.dropped_frames += max(0, frame.sequence - self.last_sequence - 1)
        if self.last_timestamp is not None and frame.timestamp_ns < self.last_timestamp:
            raise ValueError("timestamp moved backwards")
        self.last_sequence, self.last_timestamp = frame.sequence, frame.timestamp_ns
        return frame
