import os
import time
import requests
import pyaudio
import subprocess
import threading

# Configuration
# Replace this with your actual Render URL (e.g., https://my-wanda-tts.onrender.com)
RENDER_URL = "http://localhost:8000" 

class WandaRemoteClient:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=44100,
            output=True
        )

    def speak(self, text):
        if not text.strip(): return
        
        print(f"\r📡 Cloud -> [Requesting: {text[:20]}...]", end="", flush=True)
        start_time = time.time()
        
        # Setup streaming connection to your Render server
        endpoint = f"{self.url}/stream"
        params = {"text": text}
        
        try:
            with requests.get(endpoint, params=params, stream=True, timeout=15) as r:
                if r.status_code != 200:
                    print(f"\n❌ Server Error: {r.status_code} - {r.text}")
                    return

                # Local conversion: Render Streams MP3 -> Client Converts to PCM with FFmpeg
                # This is the FASTEST way to travel cross-continent (low bandwidth)
                process = subprocess.Popen(
                    ['ffmpeg', '-i', 'pipe:0', '-f', 's16le', '-ac', '1', '-ar', '44100', 'pipe:1'],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )

                first_chunk = True
                def sender():
                    for chunk in r.iter_content(chunk_size=1024):
                        if chunk:
                            process.stdin.write(chunk)
                            process.stdin.flush()
                    process.stdin.close()

                threading.Thread(target=sender, daemon=True).start()

                # Play raw PCM data as it arrives from ffmpeg
                while True:
                    data = process.stdout.read(4096)
                    if not data: break
                    
                    if first_chunk:
                        latency = time.time() - start_time
                        print(f"\r✓ Result: Playing (Cloud Latency: {latency:.2f}s)", flush=True)
                        first_chunk = False
                        
                    self.stream.write(data)
                
                process.wait()

        except Exception as e:
            print(f"\n❌ Network Error: {e}")

    def run(self):
        print("="*50)
        print("      WANDA CLOUD CLIENT (v.REMOTE)")
        print("="*50)
        print(f"Server Target: {self.url}")
        print("Type text and press Enter. (Type 'exit' to quit)\n")

        while True:
            try:
                inp = input("Remote Text > ").strip()
                if not inp: continue
                if inp.lower() in ['exit', 'quit']: break
                
                self.speak(inp)
                
            except KeyboardInterrupt:
                break

if __name__ == "__main__":
    client = WandaRemoteClient(RENDER_URL)
    client.run()
