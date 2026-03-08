import os
import requests
import time
from datetime import datetime
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

# Load env from parent directory
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(env_path)

class APIHealthChecker:
    def __init__(self):
        # Extract keys from .env
        raw_keys = os.getenv("ELEVENLABS_KEYS", "").split(",")
        self.keys = [k.strip() for k in raw_keys if k.strip()]
        
        # ANSI Colors for terminal
        self.GREEN = "\033[92m"
        self.RED = "\033[91m"
        self.YELLOW = "\033[93m"
        self.CYAN = "\033[96m"
        self.RESET = "\033[0m"
        self.BOLD = "\033[1m"

    def get_key_status(self, api_key):
        """Fetch subscription and character limit details for a key."""
        try:
            url = "https://api.elevenlabs.io/v1/user/subscription"
            start_check = time.time()
            r = requests.get(url, headers={"xi-api-key": api_key}, timeout=8)
            latency = (time.time() - start_check) * 1000 # ms
            
            if r.status_code == 200:
                data = r.json()
                limit = data.get("character_limit", 0)
                count = data.get("character_count", 0)
                remaining = limit - count
                tier = data.get("tier", "unknown")
                
                # Handle reset days
                reset_ts = data.get("next_character_count_reset_unix", 0)
                if reset_ts:
                    days_left = (reset_ts - time.time()) / (24 * 3600)
                    reset_info = f"{int(days_left)}d left" if days_left > 1 else "1d left"
                else:
                    reset_info = "N/A"
                
                health_pct = (remaining / limit * 100) if limit > 0 else 0
                
                status_icon = f"{self.GREEN}ACTIVE{self.RESET}"
                if health_pct < 10: status_icon = f"{self.YELLOW}LOW{self.RESET}"
                if remaining < 100: status_icon = f"{self.RED}EXHAUSTED{self.RESET}"
                
                return {
                    "key": f"{api_key[:6]}...{api_key[-4:]}",
                    "tier": tier.upper(),
                    "remaining": f"{remaining:,}",
                    "health": f"{int(health_pct)}%",
                    "reset": reset_info,
                    "status": status_icon,
                    "latency": f"{int(latency)}ms"
                }
            elif r.status_code == 401:
                return {"key": f"{api_key[:6]}...{api_key[-4:]}", "status": f"{self.RED}INVALID{self.RESET}", "tier": "-", "remaining": "-", "health": "-", "reset": "-", "latency": "-"}
            else:
                return {"key": f"{api_key[:6]}...{api_key[-4:]}", "status": f"{self.RED}ERROR {r.status_code}{self.RESET}", "tier": "-", "remaining": "-", "health": "-", "reset": "-", "latency": "-"}
                
        except Exception as e:
            return {"key": f"{api_key[:6]}...{api_key[-4:]}", "status": f"{self.RED}FAILED{self.RESET}", "tier": "-", "remaining": "-", "health": "-", "reset": "-", "latency": "-"}

    def run(self):
        os.system('cls' if os.name == 'nt' else 'clear')
        print(f"{self.BOLD}{self.CYAN}" + "="*95)
        print(" " * 30 + "ELEVENLABS API KEY HEALTH MONITOR")
        print("="*95 + f"{self.RESET}\n")

        if not self.keys:
            print(f"{self.RED}No keys found in .env file!{self.RESET}")
            return

        print(f"{self.BOLD}{'API KEY':<16} | {'TIER':<10} | {'REMAINING':<12} | {'HEALTH':<8} | {'RESET DAYS':<12} | {'LATENCY':<8} | {'STATUS'}")
        print("-" * 95 + f"{self.RESET}")

        # Check keys in parallel for speed
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(self.get_key_status, self.keys))

        # Statistics
        active_count = 0
        total_remaining = 0
        
        for res in results:
            print(f"{res['key']:<16} | {res['tier']:<10} | {res['remaining']:<12} | {res['health']:<8} | {res['reset']:<12} | {res['latency']:<8} | {res['status']}")
            if "ACTIVE" in res['status'] or "LOW" in res['status']:
                active_count += 1
                try:
                    total_remaining += int(res['remaining'].replace(',', ''))
                except: pass

        print(f"\n{self.BOLD}{self.CYAN}" + "-"*95)
        print(f" SUMMARY: {active_count}/{len(self.keys)} Keys Operational | Total Credits Available: {total_remaining:,}")
        print("-"*95 + f"{self.RESET}\n")

if __name__ == "__main__":
    # Enable ANSI escape sequences on Windows
    if os.name == 'nt':
        os.system('')
    
    checker = APIHealthChecker()
    checker.run()
