"""Qwen TTS audio generation without vendor text-to-speech."""
import json
import os
import urllib.request
import base64
import uuid
import websocket


class QwenTTS:
    def __init__(self, voice_config=None):
        self.key = os.environ.get("DASHSCOPE_API_KEY", "")
        voice_config = voice_config or {}
        self.model = voice_config.get("model") or os.environ.get("QWEN_TTS_MODEL", "qwen3-tts-flash")
        self.voice = voice_config.get("voice") or os.environ.get("QWEN_TTS_VOICE", "Neil")
        self.speech_rate = float(voice_config.get("speech_rate", voice_config.get("speed", 1.15)))
        self.url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
        if not self.key:
            raise ValueError("DASHSCOPE_API_KEY is required for independent TTS")

    def synthesize(self, text: str) -> bytes:
        body = json.dumps({"model": self.model, "input": {
            "text": text[:1800], "voice": self.voice, "language_type": "Chinese"
        }, "parameters": {"speech_rate": self.speech_rate}}).encode()
        request = urllib.request.Request(self.url, body, headers={
            "Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        audio_url = (((result.get("output") or {}).get("audio") or {}).get("url"))
        if not audio_url:
            raise RuntimeError("Qwen TTS did not return an audio URL")
        with urllib.request.urlopen(audio_url, timeout=30) as response:
            audio = response.read()
        if not audio or not audio.startswith(b"RIFF"):
            raise RuntimeError("Qwen TTS returned invalid WAV audio")
        return audio

    def stream_pcm(self, text: str, instructions: str = ""):
        yield from self.stream_pcm_segments([text], instructions=instructions)

    def stream_pcm_segments(self, segments, instructions: str = ""):
        """Yield 24 kHz mono s16le PCM chunks as the realtime service emits them."""
        model = "qwen3-tts-instruct-flash-realtime" if instructions else self.model
        url = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=" + model
        ws = websocket.create_connection(
            url, header=["Authorization: Bearer " + self.key], timeout=30
        )
        try:
            def send(kind, **fields):
                ws.send(json.dumps({"event_id": "event_" + uuid.uuid4().hex,
                                    "type": kind, **fields}))

            while True:
                event = json.loads(ws.recv())
                if event.get("type") == "error":
                    raise RuntimeError("Realtime TTS: " + str(event.get("error")))
                if event.get("type") == "session.created":
                    break
            session = {"voice": self.voice, "mode": "commit",
                       "language_type": "Auto", "response_format": "pcm",
                       "sample_rate": 24000, "speech_rate": self.speech_rate}
            if instructions:
                session["instructions"] = instructions
            send("session.update", session=session)
            while True:
                event = json.loads(ws.recv())
                if event.get("type") == "error":
                    raise RuntimeError("Realtime TTS: " + str(event.get("error")))
                if event.get("type") == "session.updated":
                    break
            for text in segments:
                if not text.strip():
                    continue
                send("input_text_buffer.append", text=text)
                send("input_text_buffer.commit")
                received = False
                while True:
                    event = json.loads(ws.recv())
                    kind = event.get("type")
                    if kind == "error":
                        raise RuntimeError("Realtime TTS: " + str(event.get("error")))
                    if kind == "response.audio.delta":
                        received = True
                        yield base64.b64decode(event["delta"])
                    if kind == "response.done":
                        if not received:
                            raise RuntimeError("Realtime TTS returned no audio")
                        break
        finally:
            ws.close()
