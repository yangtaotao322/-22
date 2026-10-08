"""UBTECH built-in microphone reader, VAD and Qwen ASR client.

The shared-memory layout mirrors the vendor ``SharedStreamReader`` shipped in
``vendor_demos``.  Audio is currently delivered as 16 kHz mono signed 16-bit
PCM, 3200 bytes per 100 ms frame.
"""
from __future__ import annotations

import base64
import io
import json
import math
import mmap
import os
import struct
import time
import urllib.request
import wave
from collections import deque
from pathlib import Path


RING_HEADER_SIZE = 64
FRAME_HEADER_SIZE = 64
DEFAULT_FRAME_BYTES = 3200
DEFAULT_MAX_FRAMES = 64
DEFAULT_SAMPLE_RATE = 16000


def runtime_audio_path(config_file="/tmp/robo/ipc/robo_sdk_paths.conf"):
    """Return the vendor runtime's shared-memory audio path."""
    path = Path(config_file)
    if not path.is_file():
        raise FileNotFoundError(f"找不到优必选音频 IPC 配置：{path}")
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    result = values.get("ROBO_AUDIO_STREAM_FILE", "")
    if not result:
        raise ValueError(f"{path} 缺少 ROBO_AUDIO_STREAM_FILE")
    return result


class SharedAudioReader:
    """Read live PCM frames from the vendor shared-memory ring buffer."""

    def __init__(self, path, frame_payload_size=DEFAULT_FRAME_BYTES,
                 max_frames=DEFAULT_MAX_FRAMES):
        self.path = str(path)
        self.frame_payload_size = int(frame_payload_size)
        self.max_frames = int(max_frames)
        self.fd = None
        self.mapping = None
        self.next_index = 0

    @property
    def mapped_size(self):
        return RING_HEADER_SIZE + self.max_frames * (
            FRAME_HEADER_SIZE + self.frame_payload_size
        )

    def open(self, timeout=8.0):
        deadline = time.monotonic() + timeout
        while not os.path.exists(self.path):
            if time.monotonic() >= deadline:
                raise FileNotFoundError(f"优必选音频共享内存尚未出现：{self.path}")
            time.sleep(0.1)
        while True:
            self.fd = open(self.path, "rb", buffering=0)
            actual_size = os.fstat(self.fd.fileno()).st_size
            if actual_size >= self.mapped_size:
                self.mapping = mmap.mmap(
                    self.fd.fileno(), self.mapped_size, access=mmap.ACCESS_READ
                )
                ring_frames, ring_payload = struct.unpack_from("<QQ", self.mapping, 8)
                if (ring_frames == self.max_frames and
                        ring_payload == self.frame_payload_size):
                    break
            self.close()
            if time.monotonic() >= deadline:
                raise ValueError(
                    "优必选音频共享内存尚未完成初始化或参数不匹配"
                )
            time.sleep(0.1)
        self.discard_pending()
        return self

    def discard_pending(self):
        if self.mapping is not None:
            self.next_index = struct.unpack_from("<Q", self.mapping, 0)[0]

    def read_frame(self, timeout=1.0):
        if self.mapping is None:
            raise RuntimeError("音频共享内存尚未打开")
        deadline = time.monotonic() + timeout
        while True:
            available = struct.unpack_from("<Q", self.mapping, 0)[0]
            if available - self.next_index > self.max_frames:
                self.next_index = available - self.max_frames
            if self.next_index < available:
                offset = RING_HEADER_SIZE + (
                    self.next_index % self.max_frames
                ) * (FRAME_HEADER_SIZE + self.frame_payload_size)
                sequence, timestamp_ns, payload_size = struct.unpack_from(
                    "<QQQ", self.mapping, offset
                )
                if payload_size > self.frame_payload_size:
                    self.next_index += 1
                    raise ValueError(f"音频帧过大：{payload_size}")
                start = offset + FRAME_HEADER_SIZE
                payload = self.mapping[start:start + payload_size]
                self.next_index += 1
                return sequence, timestamp_ns, payload
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    def close(self):
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        if self.fd is not None:
            self.fd.close()
            self.fd = None


def pcm_rms(payload):
    """Compute RMS without numpy/audioop dependencies."""
    count = len(payload) // 2
    if count <= 0:
        return 0
    samples = struct.unpack("<" + "h" * count, payload[:count * 2])
    return int(math.sqrt(sum(sample * sample for sample in samples) / count))


class VoiceActivityDetector:
    """Capture one utterance using adaptive energy and silence detection."""

    def __init__(self, reader):
        self.reader = reader
        self.min_rms = int(os.getenv("XIAOYOU_VAD_MIN_RMS", "350"))
        self.ratio = float(os.getenv("XIAOYOU_VAD_RATIO", "1.35"))
        self.start_frames = int(os.getenv("XIAOYOU_VAD_START_FRAMES", "2"))
        self.quiet_frames = int(os.getenv("XIAOYOU_VAD_QUIET_FRAMES", "8"))
        self.min_speech_frames = int(os.getenv("XIAOYOU_VAD_MIN_SPEECH_FRAMES", "5"))
        self.max_speech_frames = int(os.getenv("XIAOYOU_VAD_MAX_SPEECH_FRAMES", "120"))
        self.noise = deque(maxlen=30)

    def _threshold(self):
        if not self.noise:
            return self.min_rms
        ordered = sorted(self.noise)
        baseline = ordered[len(ordered) // 2]
        return max(self.min_rms, int(baseline * self.ratio))

    def capture(self, wait_timeout=30.0):
        self.reader.discard_pending()
        deadline = time.monotonic() + wait_timeout
        preroll = deque(maxlen=4)
        speech = []
        loud = quiet = 0
        started = False
        while started or time.monotonic() < deadline:
            frame = self.reader.read_frame(timeout=1.0)
            if frame is None:
                if started:
                    break
                continue
            payload = frame[2]
            if not payload:
                continue
            level = pcm_rms(payload)
            threshold = self._threshold()
            if not started:
                preroll.append(payload)
                if level >= threshold:
                    loud += 1
                else:
                    loud = 0
                    self.noise.append(level)
                if loud >= self.start_frames:
                    started = True
                    speech.extend(preroll)
                    quiet = 0
                    print(f"正在听你说话…（RMS={level}，阈值={threshold}）", flush=True)
            else:
                speech.append(payload)
                quiet = quiet + 1 if level < threshold else 0
                if (len(speech) >= self.min_speech_frames and
                        quiet >= self.quiet_frames):
                    break
                if len(speech) >= self.max_speech_frames:
                    break
        return b"".join(speech)


def pcm_to_wav(pcm, sample_rate=DEFAULT_SAMPLE_RATE):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return output.getvalue()


class QwenAsrClient:
    def __init__(self):
        self.key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not self.key:
            raise ValueError("DASHSCOPE_API_KEY 未配置")
        self.model = os.getenv("QWEN_ASR_MODEL", "qwen3-asr-flash-2026-02-10")
        self.url = os.getenv(
            "QWEN_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ).rstrip("/")

    def transcribe(self, pcm):
        encoded = base64.b64encode(pcm_to_wav(pcm)).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "忠实转写普通话，只输出听到的文字。注意小优、优必选、"
                        "机械伊甸等专有名词，不要添加解释。"
                    ),
                },
                {
                    "role": "user",
                    "content": [{
                        "type": "input_audio",
                        "input_audio": {
                            "data": "data:audio/wav;base64," + encoded
                        },
                    }],
                },
            ],
            "asr_options": {"language": "zh", "enable_itn": True},
        }
        request = urllib.request.Request(
            self.url + "/chat/completions",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
        content = result["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        return str(content or "").strip()
