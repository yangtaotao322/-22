#!/usr/bin/env python3
"""UBTECH built-in microphone -> ASR -> XiaoYou/Qwen -> native speaker."""
from __future__ import annotations

import array
import base64
import collections
from concurrent.futures import ThreadPoolExecutor
import json
import struct
import math
import queue
import random
import re
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import wave
from difflib import SequenceMatcher
from datetime import datetime
from io import BytesIO
from pathlib import Path

from src.dialogue_core import DialogueCore
from src.knowledge_base import KnowledgeBase
from src.modes import ModeManager
from src.qwen_client import QwenClient
from src.live_web import LiveWeb
from src.qwen_tts import QwenTTS
from src.character_config import load_character
ROOT = Path(__file__).resolve().parent
DEMO_CONTAINER = os.getenv("UBTECH_DEMO_CONTAINER", "demo_runtime-main.demo_runtime-1")
DEMO_DIR = "/opt/walker/robo_demo_menu"
CAPTURE_DIR = Path("/tmp/robo/demo_audio")

def speech_text(text: str) -> str:
    """Use digit-by-digit Mandarin pronunciation for four-digit years."""
    digits = "零一二三四五六七八九"
    return re.sub(r"(?<!\d)(20\d{2})年", lambda m: ''.join(digits[int(c)] for c in m.group(1)) + "年", text)
STOP = threading.Event()
DRAIN_AUDIO = threading.Event()
PLAY_PROC = None


def apply_pcm_gain(chunk: bytes, gain: float) -> bytes:
    """Boost signed 16-bit mono PCM with hard clipping protection."""
    if not gain or gain == 1.0:
        return chunk
    usable = len(chunk) - (len(chunk) % 2)
    samples = struct.unpack('<{}h'.format(usable // 2), chunk[:usable])
    boosted = [max(-32768, min(32767, int(sample * gain))) for sample in samples]
    result = struct.pack('<{}h'.format(len(boosted)), *boosted)
    return result + chunk[usable:]
TTS_PLAYING = threading.Event()
LAST_TTS_END = 0.0
HELPER_DIR = ROOT / "vendor_demos"

INTRO_INSTRUCTION = "用热情、自信、洒脱的青年诗人语气说话，带自然笑意和欢迎感，咬字清楚，像李白亲切地迎接朋友，不要客服腔，不要新闻播音腔。"
RECITATION_INSTRUCTION = "以有感情、有节奏的诗人语气朗诵。每句之间保留自然停顿，按意境推进，不要平铺直叙；注意呼吸、轻重和收束，保持清晰自然，不要夸张喊叫。"


def recitation_instruction(text: str) -> str:
    """Use a dedicated expressive instruction only for fixed poetry readings."""
    if "静夜思" in text or "床前明月光" in text:
        return RECITATION_INSTRUCTION + "《静夜思》前半平静温润，举头望明月略抬情绪，低头思故乡明显收束。"
    if "将进酒" in text or "天生我材必有用" in text:
        return RECITATION_INSTRUCTION + "《将进酒》开篇沉稳开阔，逐步增强；天生我材必有用是自信豪迈高潮，结尾收回来。"
    return RECITATION_INSTRUCTION


def showcase_motion(emotion) -> str:
    """Map fixed-scene emotion metadata to our verified safe expressions."""
    emotions = [str(x).lower() for x in (emotion or [])]
    if any(x in emotions for x in ("surprised", "curious")):
        return "A022"
    if any(x in emotions for x in ("grand", "proud", "excited", "happy")):
        return "A023"
    if any(x in emotions for x in ("thinking", "nostalgic", "calm")):
        return "A029"
    if any(x in emotions for x in ("encouraging", "inspired")):
        return "A007"
    return "A001"


class ExpressionBridge:
    """Best-effort facial expressions; never block the speech path."""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.last_at = 0.0
        self.lock = threading.Lock()
        self.answer_stop = threading.Event()
        self.answer_thread: threading.Thread | None = None
        self.last_answer_motion = ""
        self.attention_stop = threading.Event()
        self.attention_thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            install_helper(DEMO_CONTAINER, HELPER_DIR / "xiaoyou_expression",
                           f"{DEMO_DIR}/xiaoyou_expression")
            command = (f"cd {DEMO_DIR}; export LD_LIBRARY_PATH=./lib "
                       "RMW_IMPLEMENTATION=rmw_cyclonedds_cpp ROS_DOMAIN_ID=20; "
                       "./xiaoyou_expression")
            self.process = subprocess.Popen(
                ["docker", "exec", "-i", DEMO_CONTAINER, "bash", "-lc", command],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True,
            )
            threading.Thread(target=self._read_logs, daemon=True).start()
        except Exception as exc:
            print("EXPRESSION_UNAVAILABLE " + str(exc), flush=True)

    def _read_logs(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        for line in self.process.stdout:
            print(line.rstrip(), flush=True)

    def trigger(self, text: str, force: str = "") -> None:
        now = time.monotonic()
        with self.lock:
            if now - self.last_at < 4.0:
                return
            if self.process is None or self.process.poll() is not None or self.process.stdin is None:
                return
            lower = text.lower()
            if force:
                motion = force
            elif any(word in lower for word in ("开心", "高兴", "欢迎", "快乐", "祝贺", "great", "happy", "wonderful")):
                motion = "A023"
            elif any(word in lower for word in ("惊讶", "没想到", "哇", "wow", "surpris")):
                motion = "A022"
            elif any(word in lower for word in ("因为", "分析", "原因", "首先", "think", "because")):
                motion = "A029"
            else:
                motion = random.choice(("A001", "A002", "A007", "A022", "A023", "A029"))
            try:
                self.process.stdin.write(motion + "\n")
                self.process.stdin.flush()
                self.last_at = now
                print(json.dumps({"expression_requested": motion}, ensure_ascii=False), flush=True)
            except (BrokenPipeError, OSError) as exc:
                print("EXPRESSION_SEND_FAILED " + str(exc), flush=True)

    def _motion_for(self, text: str, force: str = "") -> str:
        lower = text.lower()
        if force:
            return force
        if any(word in lower for word in ("开心", "高兴", "欢迎", "快乐", "祝贺", "great", "happy", "wonderful")):
            return "A023"
        if any(word in lower for word in ("惊讶", "没想到", "哇", "wow", "surpris")):
            return "A022"
        if any(word in lower for word in ("因为", "分析", "原因", "首先", "think", "because")):
            return "A029"
        # Only neutral/positive actions; never select a negative emotion.
        # Verified safe pool only. A003 remains excluded because native
        # View Follower owns gaze/head tracking.
        return random.choice(("A001", "A002", "A007", "A022", "A023", "A029"))

    def start_answer(self, text: str, force: str = "") -> None:
        """Repeat a facial motion while the corresponding speech is playing."""
        if self.process is None or self.process.poll() is not None:
            return
        self.stop_answer()
        motion = self._motion_for(text, force)
        self.answer_stop.clear()
        def loop() -> None:
            while not self.answer_stop.is_set():
                self.trigger(motion, force=motion)
                self.answer_stop.wait(1.8)
        self.answer_thread = threading.Thread(target=loop, daemon=True)
        self.answer_thread.start()

    def start_once(self, text: str, force: str = "") -> None:
        """Trigger at most one verified expression for a fixed demo answer."""
        if self.process is None or self.process.poll() is not None:
            return
        self.stop_answer()
        motion = self._motion_for(text, force)
        if motion == self.last_answer_motion:
            alternatives = ("A001", "A002", "A007", "A022", "A023", "A029")
            alternatives = tuple(x for x in alternatives if x != motion)
            motion = random.choice(alternatives)
        self.last_answer_motion = motion
        self.trigger(motion, force=motion)

    def stop_answer(self) -> None:
        self.answer_stop.set()
        if self.answer_thread is not None and self.answer_thread is not threading.current_thread():
            self.answer_thread.join(timeout=1)
        self.answer_thread = None

    def start_attention(self) -> None:
        """Start the SDK's built-in gaze/attention action while Xiaoyou is active.

        A003 is the SDK command action named “注视”.  The SDK package exposed in
        this project does not expose raw head-angle or face-bounding-box control;
        this action is therefore the supported hardware-level way to keep the
        robot looking toward the detected person.  Replaying it periodically
        keeps attention active without blocking ASR/LLM/TTS.
        """
        if self.process is None or self.process.poll() is not None:
            return
        self.stop_attention()
        self.attention_stop.clear()

        def loop() -> None:
            while not self.attention_stop.is_set():
                self.trigger("注视", force="A003")
                self.attention_stop.wait(6.0)

        self.attention_thread = threading.Thread(target=loop, daemon=True)
        self.attention_thread.start()

    def stop_attention(self) -> None:
        self.attention_stop.set()
        if self.attention_thread is not None and self.attention_thread is not threading.current_thread():
            self.attention_thread.join(timeout=1)
        self.attention_thread = None

    def stop(self) -> None:
        self.stop_attention()
        self.stop_answer()
        if self.process is None or self.process.poll() is not None:
            return
        try:
            if self.process.stdin is not None:
                self.process.stdin.write("/stop\n")
                self.process.stdin.flush()
            self.process.wait(timeout=2)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            self.process.terminate()


def docker_bash(container: str, script: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", "-i", container, "bash", "-lc", script],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=timeout, check=False,
    )


def install_helper(container: str, source: Path, target: str) -> None:
    """Copy our ARM64 helper into the vendor container without a host bind mount."""
    if not source.is_file():
        raise RuntimeError(f"交付文件缺失：{source.name}")
    docker_bash(container, f"mkdir -p {os.path.dirname(target)}")
    with source.open("rb") as stream:
        result = subprocess.run(
            ["docker", "exec", "-i", container, "bash", "-lc",
             f"cat > {target} && chmod +x {target}"],
            stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=30, check=False,
        )
    if result.returncode != 0:
        raise RuntimeError("无法把 XiaoYou ARM64 程序复制到厂家音频容器")


def set_robot_volume() -> None:
    volume = max(0, min(100, int(os.getenv("ROBOT_VOLUME", "100"))))
    result = docker_bash(
        "walker-audio.audio-1",
        "source /opt/walker/setup.bash; "
        "rosa service call --no-daemon /sys/device/audio_out/set_mute "
        "audio_msgs/srv/SetMute '{\"mute\":false}'; "
        f"rosa service call --no-daemon /sys/device/audio_out/set_volume "
        f"audio_msgs/srv/SetAudioVolume '{{\"volume\":{volume}}}'",
        timeout=35,
    )
    if result.returncode != 0 or "code=0" not in result.stdout:
        raise RuntimeError("机器人音量设置失败")


def set_tts_output_volume() -> None:
    """Keep independent WAV playback at full, unmuted PulseAudio output."""
    result = docker_bash(
        "walker-audio.audio-1",
        "pactl set-sink-mute @DEFAULT_SINK@ 0; "
        "pactl set-sink-volume @DEFAULT_SINK@ 100%",
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("独立 TTS 输出音量设置失败")


def interrupt() -> None:
    global PLAY_PROC
    if PLAY_PROC is not None and PLAY_PROC.poll() is None:
        try:
            PLAY_PROC.terminate()
        except ProcessLookupError:
            pass
    docker_bash(
        "walker-registry.registry-1",
        "source /opt/walker/setup.bash; rosa service call --no-daemon "
        "/audio/stream/coze/interrupt_action_audio "
        "coze_msgs/srv/InterruptActionAudio '{}'",
        timeout=20,
    )


def set_native_wakeup(enabled: bool) -> None:
    data = "true" if enabled else "false"
    cmd = (
        "source /opt/walker/setup.bash; "
        "timeout 10 rosa service call --no-daemon "
        "/system/control/wakeup_enable_set uworld_state_msgs/srv/SetBool "
        f"'{{\"data\":{data}}}'"
    )
    result = docker_bash("walker-audio.audio-1", cmd, timeout=15)
    if result.returncode != 0 or "success=True" not in result.stdout:
        print("NATIVE_WAKEUP_DISABLE_SKIPPED", flush=True)
    followup_cmd = (
        "source /opt/walker/setup.bash; timeout 10 rosa service call --no-daemon "
        "/system/control/wakeup_followup_set uworld_state_msgs/srv/SetBool "
        f"'{{\"data\":{data}}}'"
    )
    followup = docker_bash("walker-audio.audio-1", followup_cmd, timeout=15)
    if followup.returncode != 0 or "success=True" not in followup.stdout:
        print("NATIVE_FOLLOWUP_DISABLE_SKIPPED", flush=True)


def leave_native_dialogue() -> None:
    """Leave any active vendor Coze room before taking exclusive mic input."""
    result = docker_bash(
        "walker-audio.audio-1",
        "source /opt/walker/setup.bash; timeout 10 rosa service call --no-daemon "
        "/audio/stream/coze/leave_room sys_msgs/srv/Trigger '{}'",
        timeout=15,
    )
    if result.returncode != 0 or "code=0" not in result.stdout:
        raise RuntimeError("无法退出厂家 Coze 对话会话")

def set_native_recording(enabled: bool) -> None:
    """Disable the vendor recording path while XiaoYou owns the microphone."""
    value = "true" if enabled else "false"
    result = docker_bash(
        "walker-audio.audio-1",
        "source /opt/walker/setup.bash; timeout 10 rosa service call --no-daemon "
        "/audio/sense/set_record_audio_switch audio_msgs/srv/SetRecordAudioSwitch "
        f"'{{\"record_enable\":{value}}}'",
        timeout=15,
    )
    if result.returncode != 0 or "code=0" not in result.stdout:
        raise RuntimeError("无法切换厂家原生录音开关")


def say(text: str, instructions: str = "", split_sentences: bool = True,
        voice_config: dict | None = None) -> None:
    """Play an utterance sequentially; never treat first PCM as completion."""
    global PLAY_PROC, LAST_TTS_END
    utterance_id = uuid.uuid4().hex[:12]
    parts = (__import__('re').split(r'(?<=[。！？!?；;])\s*|\n+', text.strip())
             if split_sentences else [text.strip()])
    parts = [p for p in parts if p]
    print(json.dumps({"utterance_start": utterance_id, "segments": len(parts)}, ensure_ascii=False), flush=True)
    TTS_PLAYING.set()
    try:
      for index, segment in enumerate(parts, 1):
        last_error = None
        for attempt in range(1, 3):
            print(json.dumps({"segment_start": f"{index}/{len(parts)}", "attempt": attempt}, ensure_ascii=False), flush=True)
            synthesis_started = time.monotonic()
            first_chunk = True
            PLAY_PROC = subprocess.Popen(
                ["docker", "exec", "-i", "walker-audio.audio-1", "bash", "-lc",
                 "paplay --raw --format=s16le --rate=24000 --channels=1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            audio_bytes = 0
            try:
                assert PLAY_PROC.stdin is not None
                gain = float((voice_config or {}).get("pcm_gain", 1.0))
                for chunk in QwenTTS(voice_config).stream_pcm(speech_text(segment), instructions=instructions):
                    if first_chunk:
                        print(json.dumps({"tts_first_pcm_ms": round((time.monotonic()-synthesis_started)*1000),
                                          "tts_model": "qwen3-tts-instruct-flash-realtime" if instructions
                                          else "qwen3-tts-flash-realtime"}), flush=True)
                        first_chunk = False
                    PLAY_PROC.stdin.write(apply_pcm_gain(chunk, gain))
                    PLAY_PROC.stdin.flush()
                    audio_bytes += len(chunk)
                PLAY_PROC.stdin.close()
                exit_code = PLAY_PROC.wait(timeout=35)
                if exit_code != 0:
                    raise RuntimeError(f"paplay exit={exit_code}")
                print(json.dumps({"segment_done": f"{index}/{len(parts)}", "audio_bytes": audio_bytes}, ensure_ascii=False), flush=True)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                print(json.dumps({"segment_retry": f"{index}/{len(parts)}", "error": str(exc)}, ensure_ascii=False), flush=True)
            finally:
                if PLAY_PROC is not None and PLAY_PROC.poll() is None:
                    PLAY_PROC.terminate()
                    try: PLAY_PROC.wait(timeout=1)
                    except subprocess.TimeoutExpired: PLAY_PROC.kill(); PLAY_PROC.wait()
                PLAY_PROC = None
        if last_error is not None:
            print(json.dumps({"tts_segment_failed": f"{index}/{len(parts)}"}, ensure_ascii=False), flush=True)
            raise last_error
      print(json.dumps({"utterance_complete": utterance_id}, ensure_ascii=False), flush=True)
    finally:
      TTS_PLAYING.clear()
      LAST_TTS_END = time.monotonic()
      DRAIN_AUDIO.set()


def answer_and_speak_stream(core, question: str, live_context: str = "",
                            expressions: ExpressionBridge | None = None,
                            voice_config: dict | None = None,
                            instructions: str = "") -> str:
    """Queue short complete phrases for TTS while Qwen SSE keeps arriving."""
    results = core.knowledge_base.search(question, top_k=core.top_k)
    context = "\n".join(item.get("answer", "") for item in results)
    if live_context:
        context = (context + "\n" + live_context).strip()
    segments: queue.Queue[str | None] = queue.Queue()
    errors: list[Exception] = []
    first_text_at = None
    started = time.monotonic()

    def play_worker():
        global PLAY_PROC
        TTS_PLAYING.set()
        expression_started = False
        def next_segments():
            nonlocal expression_started
            while True:
                part = segments.get()
                if part is None:
                    return
                if part.strip():
                    if expressions is not None and not expression_started:
                        expressions.start_answer(part)
                        expression_started = True
                    yield part
        first_chunk = True
        last_chunk_at = None
        synthesis_started = time.monotonic()
        PLAY_PROC = subprocess.Popen(
            ["docker", "exec", "-i", "walker-audio.audio-1", "bash", "-lc",
             "paplay --raw --format=s16le --rate=24000 --channels=1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            assert PLAY_PROC.stdin is not None
            gain = float((voice_config or {}).get("pcm_gain", 1.0))
            for chunk in QwenTTS(voice_config).stream_pcm_segments(
                    (speech_text(part) for part in next_segments()), instructions=instructions):
                now = time.monotonic()
                if first_chunk:
                    print(json.dumps({"tts_first_pcm_ms": round((now-synthesis_started)*1000),
                                      "tts_model": "qwen3-tts-flash-realtime"}), flush=True)
                    first_chunk = False
                elif last_chunk_at is not None and now - last_chunk_at > 0.5:
                    print(json.dumps({"tts_pcm_gap_ms": round((now-last_chunk_at)*1000)}), flush=True)
                PLAY_PROC.stdin.write(apply_pcm_gain(chunk, gain))
                PLAY_PROC.stdin.flush()
                last_chunk_at = time.monotonic()
            PLAY_PROC.stdin.close()
            if PLAY_PROC.wait(timeout=35) != 0:
                raise RuntimeError("独立 TTS 音频播放失败")
        except Exception as exc:
            errors.append(exc)
        finally:
            PLAY_PROC = None
            TTS_PLAYING.clear()
            DRAIN_AUDIO.set()
            expressions.stop_answer()

    worker = threading.Thread(target=play_worker, daemon=True)
    worker.start()
    text = ""
    pending = ""
    try:
        for delta in core.llm_client.stream(question, context, core.history):
            if first_text_at is None:
                first_text_at = time.monotonic()
                print(json.dumps({"qwen_first_text_ms": round((first_text_at-started)*1000)}), flush=True)
            text += delta
            pending += delta
            # Prefer a natural break; force a short first chunk to start speech.
            match = re.search(r"[，,。！？!?；;:]", pending)
            final_punctuation = match and match.group() in "。！？!?"
            if (match and (match.end() >= 6 or final_punctuation)) or len(pending) >= 18:
                cut = match.end() if match and match.end() <= 24 else min(len(pending), 18)
                segments.put(pending[:cut])
                pending = pending[cut:]
        if pending.strip():
            segments.put(pending)
    finally:
        segments.put(None)
        worker.join(timeout=60)
    if errors:
        raise errors[0]
    answer = text.strip()
    if not answer:
        raise RuntimeError("千问流式输出为空")
    core.history.extend([{"role": "user", "content": question},
                         {"role": "assistant", "content": answer}])
    del core.history[:-core.max_history_rounds * 2]
    return answer


class DemoCapture:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.path: Path | None = None

    def start(self) -> Path:
        # The vendor demo-runtime image is a one-shot container on this robot;
        # make the dependency explicit before copying our ARM64 helpers.
        state = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", DEMO_CONTAINER],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10,
        )
        if state.returncode != 0 or state.stdout.strip() != "true":
            started = subprocess.run(
                ["docker", "start", DEMO_CONTAINER],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20,
            )
            if started.returncode != 0:
                raise RuntimeError("厂家 demo_runtime 启动失败：" + started.stdout[-300:])
            time.sleep(1)
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        before = set(CAPTURE_DIR.glob("audio_*.pcm"))
        install_helper(DEMO_CONTAINER, HELPER_DIR / "xiaoyou_speaker", f"{DEMO_DIR}/xiaoyou_speaker")
        install_helper(DEMO_CONTAINER, HELPER_DIR / "robo_demo_menu_capture", f"{DEMO_DIR}/robo_demo_menu.xiaoyou_capture")
        prepare = (
            f"cd {DEMO_DIR}; "
            "test -f robo_demo_menu.xiaoyou_original || cp robo_demo_menu robo_demo_menu.xiaoyou_original; "
            "cp robo_demo_menu.xiaoyou_capture robo_demo_menu; chmod +x robo_demo_menu"
        )
        result = docker_bash(DEMO_CONTAINER, prepare)
        if result.returncode != 0:
            raise RuntimeError("麦克风采集程序准备失败：" + result.stdout[-300:])
        self.process = subprocess.Popen(
            ["docker", "exec", "-i", DEMO_CONTAINER, "bash", "-lc",
             f"cd {DEMO_DIR} && ./run.sh"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        assert self.process.stdin is not None
        self.process.stdin.write("1\n1\nh\n3\n15\n")
        self.process.stdin.flush()
        deadline = time.time() + 15
        while time.time() < deadline:
            candidates = [p for p in CAPTURE_DIR.glob("audio_*.pcm") if p not in before]
            if candidates:
                self.path = max(candidates, key=lambda p: p.stat().st_mtime)
                print(f"MIC_READY {self.path}", flush=True)
                return self.path
            if self.process.poll() is not None:
                output = self.process.stdout.read() if self.process.stdout else ""
                raise RuntimeError("麦克风采集程序提前退出：" + output[-500:])
            time.sleep(0.1)
        raise RuntimeError("等待机器人麦克风超时")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                assert self.process.stdin is not None
                self.process.stdin.write("q\n")
                self.process.stdin.flush()
                self.process.wait(timeout=5)
            except Exception:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except Exception:
                    self.process.kill()
        docker_bash(
            DEMO_CONTAINER,
            f"cd {DEMO_DIR}; test ! -f robo_demo_menu.xiaoyou_original || "
            "cp robo_demo_menu.xiaoyou_original robo_demo_menu",
            timeout=10,
        )


def rms(frame: bytes) -> float:
    values = array.array("h")
    values.frombytes(frame)
    if not values:
        return 0.0
    return math.sqrt(sum(v * v for v in values) / len(values))


def pcm_to_wav(pcm: bytes) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return output.getvalue()


def transcribe(pcm: bytes) -> str:
    audio = base64.b64encode(pcm_to_wav(pcm)).decode("ascii")
    payload = {
        "model": os.getenv("ASR_MODEL", "qwen3-asr-flash"),
        "messages": [{"role": "user", "content": [{
            "type": "input_audio",
            "input_audio": {"data": "data:audio/wav;base64," + audio},
        }]}],
        "stream": False,
        "asr_options": {"language": "zh", "enable_itn": True},
    }
    url = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    request = urllib.request.Request(
        url + "/chat/completions", json.dumps(payload).encode("utf-8"),
        {"Authorization": "Bearer " + os.environ["DASHSCOPE_API_KEY"],
         "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        result = json.load(response)
    return str(result["choices"][0]["message"]["content"]).strip()


def utterances(capture: DemoCapture):
    frame_bytes = 3200  # 100 ms, 16 kHz, signed 16-bit mono
    # Keep up to one second before VAD activation so the first syllables are
    # retained even when the speaker starts before the energy threshold rises.
    # Keep two seconds before VAD fires so the first syllable is not clipped.
    preroll = collections.deque(maxlen=20)
    active: list[bytes] = []
    speech_frames = silence_frames = 0
    noise = 1.0
    if capture.path is None:
        raise RuntimeError("麦克风采集文件不存在")
    source = capture.path.open("rb", buffering=0)
    try:
        # Ignore startup and welcome audio already recorded before listening begins.
        source.seek(0, 2)
        last_audio = time.monotonic()
        while not STOP.is_set():
            if DRAIN_AUDIO.is_set():
                source.seek(0, 2)
                preroll.clear()
                active = []
                speech_frames = silence_frames = 0
                DRAIN_AUDIO.clear()
            frame = source.read(frame_bytes)
            if len(frame) < frame_bytes:
                if (time.monotonic() - last_audio >= 2.0
                        and not TTS_PLAYING.is_set()
                        and time.monotonic() - LAST_TTS_END >= 5.0):
                    print("MIC_STALLED_RESTART", flush=True)
                    source.close()
                    capture.stop()
                    if STOP.is_set():
                        break
                    path = capture.start()
                    source = path.open("rb", buffering=0)
                    source.seek(0, 2)
                    preroll.clear()
                    active = []
                    speech_frames = silence_frames = 0
                    noise = 1.0
                    last_audio = time.monotonic()
                    print("XIAOYOU_READY", flush=True)
                time.sleep(0.03)
                continue
            last_audio = time.monotonic()
            level = rms(frame)
            if not active:
                noise = noise * 0.97 + min(level, noise * 2 + 30) * 0.03
            threshold = max(float(os.getenv("VAD_THRESHOLD", "8")), noise * 4.0 + 5)
            is_speech = level >= threshold
            if not active:
                preroll.append(frame)
                if is_speech:
                    speech_frames += 1
                    if speech_frames >= 1:
                        active = list(preroll)
                        silence_frames = 0
                else:
                    speech_frames = 0
            else:
                active.append(frame)
                silence_frames = 0 if is_speech else silence_frames + 1
                if (silence_frames >= 3 and len(active) >= 7) or len(active) >= 120:
                    end = max(0, len(active) - silence_frames + 2)
                    audio = b"".join(active[:end])
                    active = []
                    preroll.clear()
                    speech_frames = silence_frames = 0
                    if len(audio) >= frame_bytes * 4:
                        yield audio
    finally:
        source.close()


def handle_signal(_signum, _frame) -> None:
    STOP.set()


def fixed_scene_answer(question: str, scenarios, max_age_seconds: float = 5.0):
    """Return a fixed answer for a high-priority scene, or None.

    A visible answer requires both active native follow and a fresh face result;
    the state alone is intentionally insufficient to claim that a person is seen.
    """
    def normalize(value: str) -> str:
        return re.sub(r"[\s。！？!?，,、~～：:；;‘’'\"（）()]", "", value.lower())
    normalized = normalize(question)
    if not isinstance(scenarios, list):
        return None
    snapshot = {}
    try:
        snapshot = json.loads(Path("/tmp/xiaoyou_gaze_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    fresh = (time.time() - float(snapshot.get("updated_at", 0))) <= max_age_seconds
    visible = (snapshot.get("gaze_state") == "NATIVE_VIEW_FOLLOW_ACTIVE"
               and snapshot.get("face_id") is not None and fresh)
    for scene in sorted(scenarios, key=lambda item: int(item.get("priority", 0)), reverse=True):
        triggers = scene.get("triggers", scene.get("trigger", []))
        if isinstance(triggers, str):
            triggers = [triggers]
        normalized_triggers = [normalize(str(trigger)) for trigger in triggers]
        matched = any(
            normalized == trigger
            or (min(len(normalized), len(trigger)) >= 5
                and SequenceMatcher(None, normalized, trigger).ratio() >= 0.86)
            for trigger in normalized_triggers
        )
        # Accept natural spoken variants while keeping these scenes fixed and
        # never falling back to Qwen for the two recital titles.
        scene_id = scene.get("scene_id", "")
        if scene_id == "libai_jing_ye_si":
            matched = matched or ("静夜思" in normalized and ("朗读" in normalized or "朗诵" in normalized or "念" in normalized))
        elif scene_id == "libai_jiang_jin_jiu":
            matched = matched or ("将进酒" in normalized and ("朗读" in normalized or "朗诵" in normalized or "念" in normalized))
        if matched:
            return {
                "scene_id": scene.get("scene_id"),
                "answer": scene.get("answer_visible" if visible else "answer_not_visible",
                                  scene.get("fixed_answer", "")),
                "emotion": scene.get("emotion", []),
                "recitation": bool(scene.get("recitation")),
                "visible": visible,
                "gaze_state": snapshot.get("gaze_state"),
                "face_id": snapshot.get("face_id"),
            }
    return None


def main() -> int:
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    character = load_character(ROOT / "characters")
    core = DialogueCore(KnowledgeBase(character.knowledge_path), QwenClient(character.persona_prompt))
    live_web = LiveWeb()
    modes = ModeManager(ROOT / "config" / "styles.json")
    core.style_instruction = modes.instruction
    capture = DemoCapture()
    expressions = ExpressionBridge()
    try:
        # manager_container already killed walker-behavior, so no vendor ROS
        # service calls are needed on the hot path.
        with ThreadPoolExecutor(max_workers=2) as prep:
            volume = prep.submit(set_robot_volume)
            microphone = prep.submit(capture.start)
            set_tts_output_volume()
            volume.result()
            microphone.result()
        expressions.start()
        # Gaze is owned exclusively by the independent host_motion_player /
        # native View Follower chain.  Do not send the legacy A003 action here:
        # it can compete with View Follower for the head controller.
        # Native behavior is stopped by the manager for strict audio
        # isolation. Facial expressions remain driven by our SDK helper.
        intro_zh = character.opening.get("zh", "")
        intro_en = character.opening.get("en", "")
        print("XIAOYOU_INTRO", flush=True)
        expressions.start_answer(intro_zh, force="A023")
        intro_instruction = character.tts_instruction or INTRO_INSTRUCTION
        say(intro_zh, split_sentences=False, instructions=intro_instruction, voice_config=character.voice)
        time.sleep(0.35)
        if intro_en:
            say(intro_en, split_sentences=False, instructions=intro_instruction, voice_config=character.voice)
        expressions.stop_answer()
        print("XIAOYOU_READY", flush=True)
        for pcm in utterances(capture):
            if STOP.is_set():
                break
            try:
                question = transcribe(pcm)
                if not question:
                    continue
                normalized = question.strip().strip("。！？!?，,、~～ ")
                if normalized in {"嗯", "啊", "哦", "呃", "额", "有", "好", "喂"} or len(normalized) < 2:
                    print(json.dumps({"ignored_asr": question, "reason": "too_short"}, ensure_ascii=False), flush=True)
                    continue
                print(json.dumps({"asr": question}, ensure_ascii=False), flush=True)
                answer_started = time.monotonic()
                switched = modes.switch_from_text(question)
                if switched:
                    core.clear_history()
                    core.style_instruction = modes.instruction
                    answer = modes.opening
                else:
                    fixed = fixed_scene_answer(question, character.fixed_scenarios)
                    if fixed is not None:
                        answer = fixed["answer"]
                        print(json.dumps({"fixed_scene": fixed["scene_id"],
                                          "qwen_bypassed": True,
                                          "visual_confirmed": fixed["visible"],
                                          "gaze_state": fixed["gaze_state"],
                                          "face_id": fixed["face_id"]}, ensure_ascii=False), flush=True)
                        # Fixed showcase answers remain fixed, but their document
                        # emotion tags must not force unnatural/scary actions.
                        # Use the previously validated default expression policy.
                        expressions.start_once(answer, force=showcase_motion(fixed.get("emotion")))
                        instruction = (recitation_instruction(answer) if fixed.get("recitation")
                                       else character.tts_instruction)
                        say(answer, instructions=instruction, split_sentences=False, voice_config=character.voice)
                        expressions.stop_answer()
                    else:
                        live_context = live_web.context(question)
                        if live_context:
                            print(json.dumps({"live_web": live_context}, ensure_ascii=False), flush=True)
                        answer = answer_and_speak_stream(core, question, live_context, expressions,
                                                         character.voice, character.tts_instruction)
                print(json.dumps({"answer": answer}, ensure_ascii=False), flush=True)
                print(json.dumps({"answer_ready_ms": round((time.monotonic()-answer_started)*1000)}), flush=True)
                if switched:
                    expressions.start_answer(answer)
                    say(answer, instructions=character.tts_instruction, voice_config=character.voice)
                    expressions.stop_answer()
            except Exception as exc:
                print("TURN_ERROR " + type(exc).__name__ + ": " + str(exc), file=sys.stderr, flush=True)
    finally:
        STOP.set()
        try:
            interrupt()
        except Exception:
            pass
        expressions.stop()
        capture.stop()
        # The manager restores native mode after the child process exits.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
