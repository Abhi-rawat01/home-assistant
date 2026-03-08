import os
import re
import time
import json
import requests
import threading
import subprocess
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

# Load environment logic
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(env_path)

class TTSBlock:
    def __init__(self):
        # Config
        self.voice_id = "EXAVITQu4vr4xnSDxMaL"  # Default: Sarah
        self.model_id = "eleven_flash_v2_5"
        self.credit_threshold = 200
        
        # Key Management
        self.raw_keys = os.getenv("ELEVENLABS_KEYS", "").split(",")
        self.keys = [k.strip() for k in self.raw_keys if k.strip()]
        
        self.active_keys = [] # List of dicts with credit info
        self.key_blacklist = {} # {key: expiry_time}
        self.pool_percentage = 0
        
        # State
        self.is_refreshing = False
        self.last_refresh = 0
        
        # Initialize
        self.refresh_key_pool()

    def _get_key_info(self, api_key):
        """Fetch subscription details for a single key."""
        try:
            url = "https://api.elevenlabs.io/v1/user/subscription"
            r = requests.get(url, headers={"xi-api-key": api_key}, timeout=5)
            if r.status_code == 200:
                data = r.json()
                limit = data.get("character_limit", 0)
                count = data.get("character_count", 0)
                credits = limit - count
                tier = data.get("tier", "free").lower()
                percent = (credits / limit * 100) if limit > 0 else 0
                reset_ts = data.get("next_character_count_reset_unix", 0)
                
                return {
                    "key": api_key,
                    "credits": credits,
                    "tier": tier,
                    "percent": percent,
                    "limit": limit,
                    "reset_ts": reset_ts
                }
        except:
            pass
        return None

    def refresh_key_pool(self):
        """Scans all keys and updates the active pool."""
        if self.is_refreshing: return
        self.is_refreshing = True
        
        print(f"[TTS-Block] Refreshing credit pool for {len(self.keys)} keys...")
        
        valid_pool = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(filter(None, executor.map(self._get_key_info, self.keys)))
        
        # Filter keys that have enough credits
        for info in results:
            if info['credits'] > self.credit_threshold:
                valid_pool.append(info)
        
        # Sort: Starter keys first (highest credit), then Free keys (highest credit)
        valid_pool.sort(key=lambda x: (0 if x['tier'] == 'starter' else 1, -x['credits']))
        
        self.active_keys = valid_pool
        
        # Calculate overall pool "Credit Score" (average percentage of free keys)
        free_keys = [k for k in results if k['tier'] == 'free']
        if free_keys:
            self.pool_percentage = sum(k['percent'] for k in free_keys) / len(free_keys)
        else:
            self.pool_percentage = 0
            
        self.last_refresh = time.time()
        self.is_refreshing = False
        print(f"[TTS-Block] Pool updated. Active keys: {len(self.active_keys)}. Credit Score: {int(self.pool_percentage)}%")

    def get_best_key(self):
        """Returns the best available key, checking blacklist."""
        for info in self.active_keys:
            key = info['key']
            if key in self.key_blacklist:
                if time.time() > self.key_blacklist[key]:
                    del self.key_blacklist[key]
                else:
                    continue
            return key
        
        # If no keys left, force a refresh
        self.refresh_key_pool()
        return self.active_keys[0]['key'] if self.active_keys else None

    def generate_speech(self, text, output_format="mp3_44100_128"):
        """Generates speech and returns raw audio bytes."""
        key = self.get_best_key()
        if not key:
            print("[TTS-Block] Error: No valid API keys available!")
            return None

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
        headers = {
            "xi-api-key": key,
            "Content-Type": "application/json"
        }
        payload = {
            "text": text,
            "model_id": self.model_id,
            "output_format": output_format,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.8
            }
        }

        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code == 200:
                return r.content
            elif r.status_code in [429, 401]:
                print(f"[TTS-Block] Key {key[:6]} limit reached. Blacklisting...")
                self.key_blacklist[key] = time.time() + 3600 # 1 hour
                return self.generate_speech(text) # Retry with next key
            else:
                print(f"[TTS-Block] ElevenLabs Error {r.status_code}: {r.text}")
        except Exception as e:
            print(f"[TTS-Block] Request failed: {e}")
            
        return None

    def stream_pcm_audio(self, text):
        """Streams audio from ElevenLabs and yields PCM chunks instantly."""
        key = self.get_best_key()
        if not key:
            print("[TTS-Block] Error: No valid API keys available!")
            return

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream"
        headers = {
            "xi-api-key": key,
            "Content-Type": "application/json"
        }
        payload = {
            "text": text,
            "model_id": self.model_id,
            "output_format": "mp3_44100_128",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.8
            }
        }

        try:
            r = requests.post(url, json=payload, headers=headers, stream=True, timeout=12)
            if r.status_code == 200:
                # Start ffmpeg process to convert stream on the fly
                process = subprocess.Popen(
                    ['ffmpeg', '-i', 'pipe:0', '-f', 's16le', '-ac', '1', '-ar', '44100', 'pipe:1'],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )

                # Thread to feed the incoming chunks into ffmpeg
                def feeder():
                    try:
                        for chunk in r.iter_content(chunk_size=1024):
                            if chunk:
                                process.stdin.write(chunk)
                                process.stdin.flush()
                        process.stdin.close()
                    except:
                        pass
                
                feeder_thread = threading.Thread(target=feeder, daemon=True)
                feeder_thread.start()

                # Yield PCM chunks as they are ready
                while True:
                    chunk = process.stdout.read(4096)
                    if not chunk:
                        break
                    yield chunk
                
                process.wait()
            elif r.status_code in [429, 401]:
                print(f"[TTS-Block] Key {key[:6]} limit reached/invalid. Blacklisting...")
                self.key_blacklist[key] = time.time() + 3600
                yield from self.stream_pcm_audio(text) # Retry
            else:
                print(f"[TTS-Block] ElevenLabs Error {r.status_code}: {r.text}")
        except Exception as e:
            print(f"[TTS-Block] Streaming failed: {e}")

    def get_pcm_audio(self, text):
        """Generates speech and returns full PCM bytes (blocks until done)."""
        full_pcm = b""
        for chunk in self.stream_pcm_audio(text):
            full_pcm += chunk
        return full_pcm if full_pcm else None

    def get_score(self):
        """Returns the estimated credit score (average health of free keys)."""
        return int(self.pool_percentage)

def main():
    print("--- TTS-Block Main Interface ---")
    tts = TTSBlock()
    print(f"Current Credit Score: {tts.get_score()}%")
    
    test_text = "Hello! I am the new speech generation block. My credit pool is healthy."
    print(f"Testing speech generation...")
    
    # Logic test (Reverted to credit-priority logic)
    key_used = tts.get_best_key()
    print(f"Logic selected key: {key_used[:6]}...{key_used[-4:]} (Highest credit priority)")
    
    audio = tts.generate_speech(test_text)
    if audio:
        with open("test_block.mp3", "wb") as f:
            f.write(audio)
        print("✓ Success! Saved 'test_block.mp3'")
        
        # Optional: Try conversion
        print("Testing PCM conversion...")
        pcm = tts.get_pcm_audio("Low latency conversion check.")
        if pcm:
            print(f"✓ Success! Generated {len(pcm)} bytes of PCM data.")
    else:
        print("❌ Failed to generate audio.")

if __name__ == "__main__":
    main()
