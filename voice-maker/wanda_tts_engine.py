import os
import time
import requests
import threading
import subprocess
from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

# Load env for local testing
load_dotenv()

app = FastAPI(title="Wanda TTS Master Engine")

class WandaTTSEngine:
    def __init__(self):
        # Config
        self.voice_id = "EXAVITQu4vr4xnSDxMaL" # ElevenLabs Sarah
        self.dg_voice = "aura-asteria-en"       # Deepgram Asteria
        self.firebase_url = "https://smart-switch010a-default-rtdb.asia-southeast1.firebasedatabase.app/tts/keys.json"
        
        # State
        self.el_keys = []
        self.dg_key = os.getenv("DEEPGRAM_API_KEY")
        self.active_el_keys = []
        self.key_blacklist = {}
        
        # Initialize
        self.load_keys_from_firebase()
        self.refresh_key_pool()

    def load_keys_from_firebase(self):
        """Fetch ElevenLabs keys from RTDB."""
        try:
            r = requests.get(self.firebase_url, timeout=5)
            if r.status_code == 200 and r.json():
                data = r.json()
                self.el_keys = data if isinstance(data, list) else data.split(",")
                print(f"[Master-TTS] Loaded {len(self.el_keys)} keys from Firebase.")
        except Exception as e:
            print(f"[Master-TTS] Firebase Load Error: {e}")
            self.el_keys = os.getenv("ELEVENLABS_KEYS", "").split(",")

    def _check_el_key(self, key):
        try:
            r = requests.get("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": key}, timeout=5)
            if r.status_code == 200:
                d = r.json()
                if d.get("tier", "free").lower() == "free": return None # Flash needs Paid
                return {"key": key, "credits": d.get("character_limit", 0) - d.get("character_count", 0)}
        except: pass
        return None

    def refresh_key_pool(self):
        """Identify which ElevenLabs keys are healthy (Starter+ Tier)."""
        with ThreadPoolExecutor(max_workers=5) as executor:
            self.active_el_keys = list(filter(None, executor.map(self._check_el_key, self.el_keys)))
        self.active_el_keys.sort(key=lambda x: -x['credits'])
        print(f"[Master-TTS] Pool Active: {len(self.active_el_keys)} keys.")

    def get_el_key(self):
        for info in self.active_el_keys:
            if info['key'] not in self.key_blacklist or time.time() > self.key_blacklist[info['key']]:
                return info['key']
        return None

    def stream_provider_el(self, text):
        """Stream from ElevenLabs (Flash v2.5)."""
        key = self.get_el_key()
        if not key: return None
        
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream"
        payload = {
            "text": text,
            "model_id": "eleven_flash_v2_5",
            "output_format": "mp3_44100_128",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}
        }
        r = requests.post(url, json=payload, headers={"xi-api-key": key}, stream=True, timeout=10)
        if r.status_code == 200:
            for chunk in r.iter_content(chunk_size=1024):
                if chunk: yield chunk
        elif r.status_code in [429, 401]:
            self.key_blacklist[key] = time.time() + 3600
            yield from self.stream_provider_el(text)

    def stream_provider_dg(self, text):
        """Stream from Deepgram Aura (Ultra-fast backup)."""
        if not self.dg_key: return None
        url = f"https://api.deepgram.com/v1/speak?model={self.dg_voice}&encoding=mp3"
        r = requests.post(url, json={"text": text}, headers={"Authorization": f"Token {self.dg_key}"}, stream=True, timeout=10)
        if r.status_code == 200:
            for chunk in r.iter_content(chunk_size=4096):
                if chunk: yield chunk

# Singleton engine
engine = WandaTTSEngine()

@app.get("/")
def health():
    return {"status": "Wanda Master Engine Live", "el_pool": len(engine.active_el_keys), "dg_active": bool(engine.dg_key)}

@app.get("/stream")
async def stream(text: str = Query(..., description="Speech Text"), provider: str = "eleven"):
    """
    Unified streaming endpoint.
    provider="eleven" (Main - High Quality)
    provider="deepgram" (Ultra-Low Latency / Backup)
    """
    if provider == "eleven" and engine.active_el_keys:
        return StreamingResponse(engine.stream_provider_el(text), media_type="audio/mpeg")
    else:
        # Fallback or direct requested Deepgram
        return StreamingResponse(engine.stream_provider_dg(text), media_type="audio/mpeg")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
