#!/usr/bin/env python3
"""Development-only TTS style A/B generator; never changes production config."""
import argparse, json, sys, time, wave
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from src.qwen_tts import QwenTTS

TEXT = "在下李白，字太白，号青莲居士。千年之后还能与你相逢，实在畅快。"
JINGYESI = "床前明月光，疑是地上霜。举头望明月，低头思故乡。"
JIANG = {
    "a_opening_grand": ("君不见，黄河之水天上来，奔流到海不复回。", "以沉稳、开阔、厚实的诗人语气朗诵，保持中低音和清晰咬字，先铺陈气势，不要一开始就喊。"),
    "b_build_proud": ("人生得意须尽欢，莫使金樽空对月。", "以自信、洒脱、逐步增强的语气朗诵，节奏有推进感，保持自然和清晰。"),
    "c_climax_excited": ("天生我材必有用，千金散尽还复来！", "以李白式豪迈、自信、洒脱的诗人语气朗诵。声音有力量和胸腔共鸣，情绪明显上扬但不嘶吼；这段是明显高潮。"),
    "d_ending_grand": ("与尔同销万古愁。", "以宏阔而收束的语气朗诵，逐渐回落，留下余韵，不保持最高强度。")
}

def write_wav(path, pcm, rate=24000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)

def run_one(profile, cfg, text, out):
    instructions = cfg.get("instruction", "")
    actual_model = "qwen3-tts-instruct-flash-realtime" if instructions else "qwen3-tts-flash-realtime"
    rate = float(cfg.get("speech_rate", cfg.get("speed", 1.15)))
    print(f"TTS_TEST_PROFILE={profile}", flush=True)
    print(f"TTS_TEST_MODEL={actual_model}", flush=True)
    print(f"TTS_TEST_VOICE={cfg.get('voice', 'Neil')}", flush=True)
    print(f"TTS_TEST_RATE={rate}", flush=True)
    print(f"TTS_TEST_INSTRUCTIONS_USED={'true' if bool(instructions) else 'false'}", flush=True)
    started = time.monotonic()
    first = None
    chunks = []
    for chunk in QwenTTS(cfg).stream_pcm(text, instructions=instructions):
        if first is None:
            first = time.monotonic()
            print(f"TTS_FIRST_PCM_MS={(first-started)*1000:.1f}", flush=True)
        chunks.append(chunk)
    pcm = b"".join(chunks)
    path = out / f"{profile}.wav"
    write_wav(path, pcm)
    total = (time.monotonic() - started) * 1000
    duration = len(pcm) / (24000 * 1 * 2)
    print(f"TTS_TOTAL_MS={total:.1f}", flush=True)
    print(f"TTS_WAV_DURATION_SEC={duration:.3f}", flush=True)
    print(f"TTS_WAV_BYTES={path.stat().st_size}", flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", choices=["baseline", "conversation_a", "conversation_b", "conversation_c", "jingyesi", "jiangjinjiu"])
    ap.add_argument("--out", default="tts_style_output")
    args = ap.parse_args()
    profiles = json.loads((Path(__file__).with_name("tts_style_profiles.json")).read_text())
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    if args.profile == "jiangjinjiu":
        for name, (text, instruction) in JIANG.items():
            cfg = {"model": "qwen3-tts-instruct-flash-realtime", "voice": "Neil", "speed": 1.0}
            run_one(f"jiangjinjiu_{name}", cfg | {"instruction": instruction}, text, out)
        return
    cfg = profiles[args.profile]
    text = JINGYESI if args.profile == "jingyesi" else TEXT
    run_one(args.profile, cfg, text, out)

if __name__ == "__main__": main()
