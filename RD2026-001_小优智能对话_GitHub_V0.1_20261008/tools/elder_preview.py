import wave
from src.qwen_tts import QwenTTS

TEXT = "在下李白，字太白。既已相逢，不妨与我聊诗、谈月、看山河。天生我材必有用，千金散尽还复来！"
PROFILES = {
    "late_li bai": "晚年男性诗人，明显成熟的年龄感，声线低沉厚重、温润沧桑，有深厚胸腔共鸣和岁月阅历；像走过万里山河、历尽人生之后的李白，沉稳从容，谈诗谈酒仍有豪迈力量。不要青年感、偶像感、客服腔、新闻播音腔，不要虚弱或沙哑。",
}
for name, instruction in PROFILES.items():
    pcm = b""
    cfg = {"model": "qwen3-tts-instruct-flash-realtime", "voice": "Ethan", "speed": 0.88}
    for chunk in QwenTTS(cfg).stream_pcm(TEXT, instructions=instruction):
        pcm += chunk
    with wave.open("/tmp/" + name + ".wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
    print(name, len(pcm), flush=True)
