import os
import time
import pyaudio
import threading
from tts_block import TTSBlock

class TTSConsoleBot:
    def __init__(self):
        # Initialize the core TTS Engine
        self.tts = TTSBlock()
        
        # Audio setup for direct playback
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=44100,
            output=True
        )
        
        self.is_playing = False

    def stream_and_play(self, text):
        """Stream from the block and play each chunk immediately."""
        self.is_playing = True
        start_time = time.time()
        first_chunk = True
        
        try:
            for chunk in self.tts.stream_pcm_audio(text):
                if first_chunk:
                    latency = time.time() - start_time
                    print(f"\r✓ Result: Playing ({latency:.2f}s) | Score: {self.tts.get_score()}%", flush=True)
                    first_chunk = False
                self.stream.write(chunk)
        except Exception as e:
            print(f"\n❌ Playback error: {e}")
            
        self.is_playing = False

    def run(self):
        os.system('cls' if os.name == 'nt' else 'clear')
        print("="*50)
        print("      WANDA TEXT-TO-SPEECH BOT (v.BLACK)")
        print("="*50)
        print(f"Pool Health: {self.tts.get_score()}% | Active Keys: {len(self.tts.active_keys)}")
        print("Type your message and press Enter. (Type 'exit' to quit)\n")

        while True:
            try:
                text = input("Text > ").strip()
                
                if not text:
                    continue
                if text.lower() in ['exit', 'quit']:
                    break

                print("Generating...", end="", flush=True)
                
                # Start streaming and playing in a background thread
                threading.Thread(target=self.stream_and_play, args=(text,), daemon=True).start()

            except KeyboardInterrupt:
                break

        print("\nCleaning up...")
        self.stream.stop_stream()
        self.stream.close()
        self.pa.terminate()

if __name__ == "__main__":
    bot = TTSConsoleBot()
    bot.run()
