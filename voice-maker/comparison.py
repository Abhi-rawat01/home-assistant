import os
import time
import requests
import json
from tts_block import TTSBlock

# Models to compare
MODELS = {
    "Turbo v2.5": "eleven_turbo_v2_5",
    "Flash v2.5": "eleven_flash_v2_5",
}

PROMPTS = [
    "The ElevenLabs Flash model is engineered for the fastest possible response times, making it ideal for real-time conversational AI where Every millisecond counts. It maintains high naturalness while significantly reducing the time to first byte, allowing for seamless interactions that feel fluid and human-like in dynamic environments.",
    "ElevenLabs Turbo is another powerful model designed for speed, but with a different architectural focus. It excels at balancing emotional range and processing efficiency. While Flash is often the fastest, Turbo provides a distinct vocal stability that some developers prefer for consistent long-form narration without sacrificing much in the way of performance.",
    "When comparing these two for a home assistant like Wanda, the goal is to minimize the gap between the user finishing their sentence and the assistant starting to speak. By testing both models with these longer paragraphs, we can see which one handles higher character counts more efficiently while maintaining the premium voice quality we expect."
]

def compare():
    tts = TTSBlock()
    results = []
    
    # Create comparison folder
    compare_folder = "comparison_results"
    if not os.path.exists(compare_folder):
        os.makedirs(compare_folder)
    
    print("="*80)
    print(f"{'Model Name':<18} | {'Latency':<10} | {'Status':<10} | {'Prompt Preview'}")
    print("-" * 80)
    
    for model_name, model_id in MODELS.items():
        # Temporarily switch model in tts block
        tts.model_id = model_id
        
        for i, prompt in enumerate(PROMPTS):
            start_time = time.time()
            audio_data = tts.generate_speech(prompt)
            latency = time.time() - start_time
            
            status = "✓ OK" if audio_data else "❌ FAIL"
            
            # Save audio for manual checking
            if audio_data:
                safe_name = f"{model_name.replace(' ', '_')}_p{i+1}.mp3"
                with open(os.path.join(compare_folder, safe_name), "wb") as f:
                    f.write(audio_data)
            
            preview = prompt[:30] + "..." if len(prompt) > 30 else prompt
            print(f"{model_name:<18} | {latency:<10.2f}s | {status:<10} | {preview}")
            
            results.append({
                "model": model_name,
                "latency": latency,
                "status": status,
                "prompt": prompt
            })
            
    print("-" * 80)
    print(f"Results saved in: {compare_folder}/")
    
    # Simple summary stats
    print("\nAVERAGE LATENCY SUMMARY:")
    for model_name in MODELS.keys():
        model_latencies = [r['latency'] for r in results if r['model'] == model_name and r['status'] == "✓ OK"]
        if model_latencies:
            avg = sum(model_latencies) / len(model_latencies)
            print(f" • {model_name:<15}: {avg:.2f}s")

if __name__ == "__main__":
    compare()
