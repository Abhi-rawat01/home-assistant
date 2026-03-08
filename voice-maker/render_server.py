import os
import time
import requests
from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

# Load env
load_dotenv()

app = FastAPI(title="Wanda TTS Cloud Bridge")

class CloudTTSManager:
    def __init__(self):
        raw_keys = os.getenv("ELEVENLABS_KEYS", "").split(",")
        self.keys = [k.strip() for k in raw_keys if k.strip()]
        self.voice_id = "EXAVITQu4vr4xnSDxMaL"
        self.model_id = "eleven_flash_v2_5"
        self.active_keys = []
        self.refresh_key_pool()

    def _get_key_info(self, api_key):
        try:
            url = "https://api.elevenlabs.io/v1/user/subscription"
            r = requests.get(url, headers={"xi-api-key": api_key}, timeout=5)
            if r.status_code == 200:
                data = r.json()
                # Skip Free Tier for Flash model as it's not supported
                if data.get("tier", "free").lower() == "free":
                    return None
                return {"key": api_key, "credits": data.get("character_limit", 0) - data.get("character_count", 0)}
        except: pass
        return None

    def refresh_key_pool(self):
        with ThreadPoolExecutor(max_workers=5) as executor:
            self.active_keys = list(filter(None, executor.map(self._get_key_info, self.keys)))
        self.active_keys.sort(key=lambda x: -x['credits'])

    def get_best_key(self):
        if not self.active_keys:
            self.refresh_key_pool()
        return self.active_keys[0]['key'] if self.active_keys else None

tts_manager = CloudTTSManager()

@app.get("/")
def health_check():
    return {"status": "Wanda TTS Online", "keys_active": len(tts_manager.active_keys)}

@app.get("/stream")
async def stream_voice(text: str = Query(..., description="Text to speak")):
    key = tts_manager.get_best_key()
    if not key:
        return {"error": "No capable Starter/Paid keys available for Flash model"}

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{tts_manager.voice_id}/stream"
    headers = {
        "xi-api-key": key,
        "Content-Type": "application/json"
    }
    payload = {
        "text": text,
        "model_id": tts_manager.model_id,
        "output_format": "mp3_44100_128",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}
    }

    def generate():
        # Connect to ElevenLabs and pipe the response instantly
        with requests.post(url, json=payload, headers=headers, stream=True) as r:
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    yield chunk

    return StreamingResponse(generate(), media_type="audio/mpeg")

if __name__ == "__main__":
    import uvicorn
    # Render uses the PORT environment variable
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
