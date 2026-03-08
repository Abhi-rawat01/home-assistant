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

app = FastAPI(title="Wanda TTS ElevenLabs Engine")

class WandaTTSEngine:
    def __init__(self):
        # Config
        self.voice_id = "EXAVITQu4vr4xnSDxMaL" # ElevenLabs Sarah
        self.firebase_url = "https://smart-switch010a-default-rtdb.asia-southeast1.firebasedatabase.app/tts/keys.json"
        
        # State
        self.el_keys = []
        self.active_el_keys = []
        self.key_blacklist = {}
        self.pool_score = 0
        
        # Initialize
        self.load_keys_from_firebase()
        self.refresh_key_pool()
        
        # Self-Ping to keep Render awake (Pings every 14 minutes)
        self.keep_awake_thread = threading.Thread(target=self._keep_awake, daemon=True)
        self.keep_awake_thread.start()

    def _keep_awake(self):
        """Pings the server itself to prevent Render's free tier from sleeping."""
        # Render provides RENDER_EXTERNAL_HOSTNAME automatically on most deployments
        hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME")
        if not hostname:
            # Fallback to service-name based URL if available
            srv_name = os.getenv("RENDER_SERVICE_NAME")
            if srv_name:
                hostname = f"{srv_name}.onrender.com"
        
        if not hostname:
            print("[Wanda-TTS] Hostname not found. Give manual override: 'RENDER_EXTERNAL_URL'")
            # Check for manual override if auto-detect fails
            hostname = os.getenv("RENDER_EXTERNAL_URL")
            if hostname:
                hostname = hostname.replace("https://", "").replace("http://", "").split("/")[0]

        if not hostname:
            print("[Wanda-TTS] Error: Could not detect self-URL. Service might sleep.")
            return

        url = f"https://{hostname}"
        print(f"[Wanda-TTS] Self-ping active for: {url}")
        
        while True:
            try:
                # Wait 14 minutes (Render sleeps after 15)
                time.sleep(14 * 60)
                r = requests.get(url, timeout=10)
                print(f"[Wanda-TTS] Keep-awake ping: {r.status_code}")
            except Exception as e:
                print(f"[Wanda-TTS] Keep-awake failed: {e}")

    def load_keys_from_firebase(self):
        """Fetch ElevenLabs keys from RTDB with optional authentication."""
        try:
            url = self.firebase_url
            secret = os.getenv("FIREBASE_SECRET")
            if secret:
                url += f"?auth={secret}"
                print("[Wanda-TTS] Using Secure Firebase Connection.")
            else:
                print("[Wanda-TTS] Using Public Firebase Connection (Warning: Insecure!)")

            r = requests.get(url, timeout=5)
            if r.status_code == 200 and r.json():
                data = r.json()
                self.el_keys = data if isinstance(data, list) else data.split(",")
                print(f"[Wanda-TTS] Loaded {len(self.el_keys)} keys from Firebase.")
            elif r.status_code == 401:
                print("[Wanda-TTS] Firebase Permission Denied. Check your FIREBASE_SECRET.")
            else:
                print(f"[Wanda-TTS] Firebase Error: {r.status_code}")
        except Exception as e:
            print(f"[Wanda-TTS] Firebase Load Error: {e}")
            self.el_keys = os.getenv("ELEVENLABS_KEYS", "").split(",")

    def _check_el_key(self, key):
        try:
            r = requests.get("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": key}, timeout=5)
            if r.status_code == 200:
                d = r.json()
                if d.get("tier", "free").lower() == "free": return None # Flash needs Paid
                return {
                    "key": key, 
                    "credits": d.get("character_limit", 0) - d.get("character_count", 0),
                    "percent": ( (d.get("character_limit", 0) - d.get("character_count", 0)) / d.get("character_limit", 1) * 100 )
                }
        except: pass
        return None

    def refresh_key_pool(self):
        """Identify which ElevenLabs keys are healthy (Starter+ Tier)."""
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(filter(None, executor.map(self._check_el_key, self.el_keys)))
        
        self.active_el_keys = sorted(results, key=lambda x: -x['credits'])
        
        # Calculate pool score (average of starting keys)
        if self.active_el_keys:
            self.pool_score = int(sum(k['percent'] for k in self.active_el_keys) / len(self.active_el_keys))
        
        print(f"[Wanda-TTS] Pool Active: {len(self.active_el_keys)} keys. Score: {self.pool_score}%")

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

# Singleton engine
engine = WandaTTSEngine()

@app.get("/")
def health():
    return {
        "status": "Wanda ElevenLabs Engine Live", 
        "el_pool": len(engine.active_el_keys),
        "score": engine.pool_score
    }

@app.get("/stream")
async def stream(text: str = Query(..., description="Speech Text")):
    """
    ElevenLabs streaming endpoint.
    """
    if engine.active_el_keys:
        return StreamingResponse(
            engine.stream_provider_el(text), 
            media_type="audio/mpeg",
            headers={"X-Pool-Score": str(engine.pool_score)}
        )
    else:
        return {"error": "No healthy ElevenLabs keys available"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
