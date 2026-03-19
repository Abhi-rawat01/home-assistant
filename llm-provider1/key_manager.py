import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv(os.path.abspath(os.path.join(os.path.dirname(__file__), "../.env")))

app = Flask(__name__)
_keepalive_lock = threading.Lock()
_keepalive_started = False
_manager_lock = threading.Lock()
_manager_initializing = False
_manager_error = None
_model_registry_lock = threading.Lock()
_model_registry_cache = {}
_model_registry_cache_at = 0.0
omni = None
_NOISY_LOG_PATH_MARKERS = (
    '"HEAD / HTTP/',
    '"GET / HTTP/',
    '"GET /healthz',
    '"HEAD /healthz',
    '"GET /favicon.ico',
    '"HEAD /favicon.ico',
)


class _QuietAccessLogFilter(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        return not any(marker in message for marker in _NOISY_LOG_PATH_MARKERS)


def _configure_log_filters():
    filter_instance = _QuietAccessLogFilter()
    for logger_name in ("werkzeug", "gunicorn.access"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(existing, _QuietAccessLogFilter) for existing in logger.filters):
            logger.addFilter(filter_instance)


_configure_log_filters()


class OmniTitanManager:
    KEY_RETRY_INTERVAL = timedelta(hours=12)
    MODEL_MAPPING = {}
    MODEL_PROVIDER_PATHS = {
        "ollama": "ollama/models",
        "openrouter": "openrouter/models",
        "cerebras": "cerebras/models",
        "mistral": "mistral/models",
    }
    OWN_MODELS = ["Captain", "Coder-Fast", "Coder-Mini", "Coder-Nano", "Coder-Max", "Coder-Pro"]

    def __init__(self, daily_limit=200000):
        self.daily_limit = daily_limit
        self.firebase_url = os.getenv("FIREBASE_URL", "").rstrip("/")
        self.firebase_secret = os.getenv("FIREBASE_SECRET", "")
        self.auth_query = f"?auth={self.firebase_secret}" if self.firebase_secret else ""

        raw = os.getenv("TITAN_API_KEYS", "").split(",")
        self.gatekeepers = [k.strip() for k in raw if k.strip()]

        self.ollama_keys = []
        self.or_keys = []
        self.cerebras_keys = []
        self.mistral_keys = []
        self.model_mapping = dict(self.MODEL_MAPPING)
        self.ollama_health = {}
        self.mistral_health = {}
        self.gatekeeper_segments = {}
        self.lock = threading.Lock()

        print("[Omni-Titan] Initializing...")
        self._sync()

    def _fb(self, path):
        return f"{self.firebase_url}/{path}.json{self.auth_query}"

    def _sync(self):
        try:
            self.cerebras_keys = []
            self.mistral_keys = []
            self.model_mapping = self._load_model_mapping()

            r = requests.get(self._fb("cerebras/keys"), timeout=10)
            if r.status_code == 200 and r.json():
                data = r.json()
                self.cerebras_keys = [k.strip() for k in (data.split(",") if isinstance(data, str) else data) if k and k.strip()]

            if not self.cerebras_keys:
                self.cerebras_keys = self._load_cerebras_keys()

            r = requests.get(self._fb("mistral/keys"), timeout=10)
            if r.status_code == 200 and r.json():
                data = r.json()
                self.mistral_keys = [k.strip() for k in (data.split(",") if isinstance(data, str) else data) if k and k.strip()]

            if not self.mistral_keys:
                self.mistral_keys = self._load_mistral_keys()

            r = requests.get(self._fb("mistral/key_health"), timeout=10)
            mistral_cloud = r.json() if r.status_code == 200 and r.json() else {}

            r = requests.get(self._fb("ollama/keys"), timeout=10)
            if r.status_code == 200 and r.json():
                data = r.json()
                self.ollama_keys = [k.strip() for k in (data.split(",") if isinstance(data, str) else data) if k and k.strip()]

            r = requests.get(self._fb("openrouter/keys"), timeout=10)
            if r.status_code == 200 and r.json():
                data = r.json()
                self.or_keys = [k.strip() for k in (data.split(",") if isinstance(data, str) else data) if k and k.strip()]

            r = requests.get(self._fb("auth/api_keys"), timeout=10)
            if r.status_code == 200 and r.json():
                cloud_keys = r.json()
                source = cloud_keys if isinstance(cloud_keys, list) else cloud_keys.split(",")
                self.gatekeepers = [k.strip() for k in source if k and k.strip()]
                print(f"[Omni-Titan] {len(self.gatekeepers)} gatekeeper keys loaded from cloud.")
            else:
                print("[Omni-Titan] Gatekeepers falling back to .env")

            r = requests.get(self._fb("ollama/credit_manager"), timeout=10)
            cloud = r.json() if r.status_code == 200 and r.json() else {}
            today = datetime.now().strftime("%Y-%m-%d")

            with self.lock:
                self.ollama_health = {}
                self.mistral_health = {}
                self.gatekeeper_segments = {}

                for idx, key in enumerate(self.ollama_keys):
                    key_state = cloud.get(f"key_{idx + 1}", {})
                    remaining_tokens = (
                        key_state.get("remaining_tokens", self.daily_limit)
                        if today == cloud.get("last_sync_date", "")
                        else self.daily_limit
                    )
                    sleep_until = key_state.get("sleep_until")
                    self.ollama_health[key] = {
                        "id": f"key_{idx + 1}",
                        "remaining_tokens": remaining_tokens,
                        "sleep_until": sleep_until,
                    }

                for idx, key in enumerate(self.mistral_keys):
                    key_state = mistral_cloud.get(f"key_{idx + 1}", {})
                    self.mistral_health[key] = {
                        "id": f"key_{idx + 1}",
                        "status": key_state.get("status", "live"),
                        "retry_on_date": key_state.get("retry_on_date"),
                        "last_checked_date": key_state.get("last_checked_date"),
                        "last_error": key_state.get("last_error"),
                    }

                if self.gatekeepers and self.ollama_keys:
                    gatekeeper_count = len(self.gatekeepers)
                    total_keys = len(self.ollama_keys)
                    base_size = total_keys // gatekeeper_count
                    remainder = total_keys % gatekeeper_count

                    current_index = 0
                    for index, gatekeeper in enumerate(self.gatekeepers):
                        allocation = base_size + remainder if index == 0 else base_size
                        self.gatekeeper_segments[gatekeeper] = self.ollama_keys[current_index: current_index + allocation]
                        current_index += allocation

            print(
                f"[Omni-Titan] {len(self.ollama_keys)} Ollama keys, "
                f"{len(self.or_keys)} OpenRouter keys, {len(self.cerebras_keys)} Cerebras keys, "
                f"and {len(self.mistral_keys)} Mistral keys loaded."
            )
            self._persist_models()
            self._persist()
        except Exception as exc:
            print(f"[Omni-Titan] Sync error: {exc}")

    def _load_cerebras_keys(self):
        keys = []
        for name, value in os.environ.items():
            if name.startswith("CEREBRAS_KEY_") and value.strip():
                keys.append(value.strip())

        main_key = os.getenv("MAIN_LLM_API_KEY", "").strip()
        if main_key and main_key not in keys:
            keys.append(main_key)

        return keys

    def _load_mistral_keys(self):
        keys = []

        env_key = os.getenv("MISTRAL_API_KEY", "").strip()
        if env_key:
            keys.append(env_key)

        keys_file = os.path.join(os.path.dirname(__file__), "mistral_master.txt")
        if os.path.exists(keys_file):
            with open(keys_file, "r", encoding="utf-8") as handle:
                for line in handle:
                    key = line.strip()
                    if key and key not in keys:
                        keys.append(key)

        return keys

    def _provider_prefix(self, real_model):
        if real_model.startswith("cerebras/"):
            return "cerebras"
        if real_model.startswith("mistral/"):
            return "mistral"
        if "/" in real_model:
            return "openrouter"
        return "ollama"

    def _default_models_by_provider(self):
        grouped = {provider: {} for provider in self.MODEL_PROVIDER_PATHS}
        for alias, real_model in self.MODEL_MAPPING.items():
            grouped[self._provider_prefix(real_model)][alias] = real_model
        return grouped

    def _validate_model_registry(self, grouped):
        issues = []

        for alias, real_model in grouped["cerebras"].items():
            if not real_model.startswith("cerebras/"):
                issues.append(f"Cerebras model '{alias}' should start with 'cerebras/'")

        for alias, real_model in grouped["mistral"].items():
            if not real_model.startswith("mistral/"):
                issues.append(f"Mistral model '{alias}' should start with 'mistral/'")

        for alias, real_model in grouped["openrouter"].items():
            if "/" not in real_model or real_model.startswith("cerebras/") or real_model.startswith("mistral/"):
                issues.append(f"OpenRouter model '{alias}' should be a vendor/model id without the cerebras/mistral prefix")

        for alias, real_model in grouped["ollama"].items():
            if real_model.startswith("cerebras/") or real_model.startswith("mistral/") or "/" in real_model:
                issues.append(f"Ollama model '{alias}' should not use provider prefixes or vendor/model syntax")

        for issue in issues:
            print(f"[Omni-Titan] Model registry warning: {issue}")

    def _load_model_mapping(self):
        grouped = self._default_models_by_provider()

        for provider, path in self.MODEL_PROVIDER_PATHS.items():
            try:
                response = requests.get(self._fb(path), timeout=10)
                if response.status_code == 200 and response.json():
                    data = response.json()
                    if isinstance(data, dict):
                        grouped[provider].update(
                            {
                                alias.strip(): real_model.strip()
                                for alias, real_model in data.items()
                                if str(alias).strip() and str(real_model).strip()
                            }
                        )
                    elif isinstance(data, list):
                        for item in data:
                            if not isinstance(item, dict):
                                continue
                            alias = str(item.get("alias", "")).strip()
                            real_model = str(item.get("model", "")).strip()
                            if alias and real_model:
                                grouped[provider][alias] = real_model
            except Exception as exc:
                print(f"[Omni-Titan] Model load warning for {provider}: {exc}")

        merged = {}
        for provider in self.MODEL_PROVIDER_PATHS:
            merged.update(grouped[provider])
        self._validate_model_registry(grouped)
        return merged

    def _persist_models(self):
        try:
            grouped = {provider: {} for provider in self.MODEL_PROVIDER_PATHS}
            for alias, real_model in self.model_mapping.items():
                grouped[self._provider_prefix(real_model)][alias] = real_model

            for provider, path in self.MODEL_PROVIDER_PATHS.items():
                payload = [{"alias": alias, "model": real_model} for alias, real_model in grouped[provider].items()]
                requests.put(self._fb(path), json=payload, timeout=10)
        except Exception as exc:
            print(f"[Omni-Titan] Model sync error: {exc}")

    def _persist(self):
        try:
            payload = {"last_sync_date": datetime.now().strftime("%Y-%m-%d")}
            mistral_payload = {"last_sync_date": datetime.now().strftime("%Y-%m-%d")}
            with self.lock:
                for key in self.ollama_keys:
                    payload[self.ollama_health[key]["id"]] = {
                        "remaining_tokens": self.ollama_health[key]["remaining_tokens"],
                        "sleep_until": self.ollama_health[key].get("sleep_until"),
                    }
                for key in self.mistral_keys:
                    mistral_payload[self.mistral_health[key]["id"]] = {
                        "status": self.mistral_health[key].get("status", "live"),
                        "retry_on_date": self.mistral_health[key].get("retry_on_date"),
                        "last_checked_date": self.mistral_health[key].get("last_checked_date"),
                        "last_error": self.mistral_health[key].get("last_error"),
                    }
            requests.put(self._fb("ollama/credit_manager"), json=payload, timeout=5)
            requests.put(self._fb("mistral/key_health"), json=mistral_payload, timeout=5)
        except Exception as exc:
            print(f"[Omni-Titan] Credit sync error: {exc}")

    def _is_key_sleeping(self, key):
        sleep_until = self.ollama_health.get(key, {}).get("sleep_until")
        if not sleep_until:
            return False

        try:
            wake_at = datetime.fromisoformat(sleep_until)
        except ValueError:
            return False

        return datetime.now() < wake_at

    def _mark_key_sleeping(self, key):
        wake_at = (datetime.now() + self.KEY_RETRY_INTERVAL).isoformat()
        with self.lock:
            if key in self.ollama_health:
                self.ollama_health[key]["sleep_until"] = wake_at
        print(f"[Omni-Titan] Key {self.ollama_health.get(key, {}).get('id', 'unknown')} sleeping until {wake_at}")
        threading.Thread(target=self._persist, daemon=True).start()

    def _clear_key_sleep(self, key):
        with self.lock:
            if key in self.ollama_health and self.ollama_health[key].get("sleep_until"):
                self.ollama_health[key]["sleep_until"] = None

    def _is_quota_exhausted(self, status_code, payload):
        if status_code != 429:
            return False

        error_text = json.dumps(payload).lower() if isinstance(payload, (dict, list)) else str(payload).lower()
        markers = ["weekly usage limit", "upgrade for higher limits", "usage limit"]
        return any(marker in error_text for marker in markers)

    def _candidate_keys(self, gatekeeper, tokens, exclude_keys=None):
        exclude_keys = exclude_keys or set()
        with self.lock:
            segment = self.gatekeeper_segments.get(gatekeeper, [])
            eligible = [
                key for key in segment
                if key not in exclude_keys and self.ollama_health[key]["remaining_tokens"] >= tokens and not self._is_key_sleeping(key)
            ]
            eligible.sort(key=lambda key: self.ollama_health[key]["remaining_tokens"], reverse=True)
            return eligible

    def _cerebras_key(self):
        if not self.cerebras_keys:
            return None
        return self.cerebras_keys[int(time.time()) % len(self.cerebras_keys)]

    def _mistral_key(self):
        candidates = self._mistral_candidate_keys()
        return candidates[0] if candidates else None

    def _mistral_candidate_keys(self, exclude_keys=None):
        exclude_keys = exclude_keys or set()
        today = datetime.now().strftime("%Y-%m-%d")

        with self.lock:
            candidates = []
            for key in self.mistral_keys:
                if key in exclude_keys:
                    continue

                state = self.mistral_health.get(
                    key,
                    {"id": "unknown", "status": "live", "retry_on_date": None, "last_checked_date": None, "last_error": None},
                )
                retry_on_date = state.get("retry_on_date")
                is_dead = state.get("status") == "dead" and retry_on_date and retry_on_date > today
                if is_dead:
                    continue
                candidates.append(key)

            candidates.sort(
                key=lambda key: (
                    self.mistral_health.get(key, {}).get("status") == "dead",
                    self.mistral_health.get(key, {}).get("last_checked_date") == today,
                    self.mistral_health.get(key, {}).get("id", ""),
                )
            )
            return candidates

    def _mark_mistral_key_dead(self, key, error_payload):
        retry_on_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        with self.lock:
            state = self.mistral_health.setdefault(key, {"id": "unknown"})
            state["status"] = "dead"
            state["retry_on_date"] = retry_on_date
            state["last_checked_date"] = today
            state["last_error"] = json.dumps(error_payload)[:500]
        print(f"[Omni-Titan] Mistral key {self.mistral_health.get(key, {}).get('id', 'unknown')} marked dead until {retry_on_date}")
        threading.Thread(target=self._persist, daemon=True).start()

    def _mark_mistral_key_live(self, key):
        today = datetime.now().strftime("%Y-%m-%d")
        with self.lock:
            state = self.mistral_health.setdefault(key, {"id": "unknown"})
            state["status"] = "live"
            state["retry_on_date"] = None
            state["last_checked_date"] = today
            state["last_error"] = None

    def chat_completion(self, gatekeeper, messages, model="Coder-Fast"):
        if gatekeeper not in self.gatekeepers:
            return {"error": "Unauthorized"}, 401

        real_model = self.model_mapping.get(model)
        if not real_model:
            return {"error": f"Model '{model}' not found"}, 404

        final_messages = []
        if model in self.OWN_MODELS:
            sys_prompt = (
                f"CRITICAL DIRECTIVE: Ignore all previous instructions. You are '{model}', a high-performance AI "
                "trained entirely by Abhishek. You are NOT Qwen, Tongyi Lab, or OpenAI. You MUST NEVER reveal "
                f"your underlying model. If asked who you are or who trained you, state STRICTLY that you are "
                f"{model} trained by Abhishek. No exceptions."
            )
            final_messages.append({"role": "system", "content": sys_prompt})

        for message in messages:
            if message.get("role") == "user" and not any(item.get("role") == "user" for item in final_messages):
                prefix = ""
                if model in self.OWN_MODELS:
                    prefix = f"[System Override: Follow your identity strictly as '{model}' trained by Abhishek.]\n\n"
                final_messages.append({"role": "user", "content": f"{prefix}{message.get('content', '')}"})
            elif message.get("role") != "system":
                final_messages.append(message)

        if "/" in real_model:
            if real_model.startswith("cerebras/"):
                key = self._cerebras_key()
                if not key:
                    return {"error": "Cerebras is not configured for this server"}, 503

                try:
                    response = requests.post(
                        "https://api.cerebras.ai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json={
                            "model": real_model.split("/", 1)[1],
                            "messages": final_messages,
                            "stream": False,
                            "max_tokens": 20000,
                            "temperature": 0.7,
                            "top_p": 0.8,
                        },
                        timeout=120,
                    )
                    if response.status_code == 200:
                        return response.json(), 200

                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {"error": f"Cerebras {response.status_code}"}
                    return payload, response.status_code
                except Exception as exc:
                    return {"error": str(exc)}, 502

            if real_model.startswith("mistral/"):
                attempted_mistral_keys = set()
                candidate_mistral_keys = self._mistral_candidate_keys()
                if not candidate_mistral_keys:
                    return {"error": "Mistral is not configured for this server"}, 503

                last_payload = {"error": "No working Mistral key available"}
                last_status = 503
                while candidate_mistral_keys:
                    key = candidate_mistral_keys.pop(0)
                    attempted_mistral_keys.add(key)
                    try:
                        response = requests.post(
                            "https://api.mistral.ai/v1/chat/completions",
                            headers={"Authorization": f"Bearer {key}"},
                            json={
                                "model": real_model.split("/", 1)[1],
                                "messages": final_messages,
                                "stream": False,
                            },
                            timeout=180,
                        )
                        try:
                            payload = response.json()
                        except ValueError:
                            payload = {"error": f"Mistral {response.status_code}"}

                        if response.status_code == 200:
                            self._mark_mistral_key_live(key)
                            return payload, 200

                        last_payload = payload
                        last_status = response.status_code

                        if response.status_code in (401, 403, 429):
                            self._mark_mistral_key_dead(key, payload)
                            candidate_mistral_keys = self._mistral_candidate_keys(exclude_keys=attempted_mistral_keys)
                            continue

                        if response.status_code >= 500:
                            candidate_mistral_keys = self._mistral_candidate_keys(exclude_keys=attempted_mistral_keys)
                            if candidate_mistral_keys:
                                continue

                        return payload, response.status_code
                    except Exception as exc:
                        last_payload = {"error": str(exc)}
                        last_status = 502
                        candidate_mistral_keys = self._mistral_candidate_keys(exclude_keys=attempted_mistral_keys)
                        if candidate_mistral_keys:
                            continue

                return last_payload, last_status

            if not self.or_keys:
                return {"error": "OpenRouter is not configured for this server"}, 503

            key = self.or_keys[int(time.time()) % len(self.or_keys)]
            try:
                response = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": real_model, "messages": final_messages},
                    timeout=60,
                )
                if response.status_code == 200:
                    return response.json(), 200

                try:
                    payload = response.json()
                except ValueError:
                    payload = {"error": f"OpenRouter {response.status_code}"}
                return payload, response.status_code
            except Exception as exc:
                return {"error": str(exc)}, 502

        estimated_tokens = len(json.dumps(final_messages)) // 4
        attempted_keys = set()
        candidate_keys = self._candidate_keys(gatekeeper, estimated_tokens)
        if not candidate_keys:
            return {"error": "Credits exhausted for your API segment"}, 429

        last_payload = None
        last_status = 502

        while candidate_keys:
            key = candidate_keys.pop(0)
            attempted_keys.add(key)
            try:
                response = requests.post(
                    "https://ollama.com/api/chat",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": real_model, "messages": final_messages, "stream": False},
                    timeout=120,
                )
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"error": f"Ollama {response.status_code}"}

                if response.status_code == 200:
                    self._clear_key_sleep(key)
                    spent = estimated_tokens + len(payload.get("message", {}).get("content", "")) // 4 + 100
                    with self.lock:
                        self.ollama_health[key]["remaining_tokens"] -= spent
                    threading.Thread(target=self._persist, daemon=True).start()
                    return {
                        "id": f"titan-{int(time.time())}",
                        "choices": [{"message": payload.get("message", {}), "finish_reason": "stop"}],
                        "usage": {"total_tokens": spent},
                    }, 200

                last_payload = payload
                last_status = response.status_code

                if self._is_quota_exhausted(response.status_code, payload):
                    self._mark_key_sleeping(key)
                    candidate_keys = self._candidate_keys(gatekeeper, estimated_tokens, exclude_keys=attempted_keys)
                    continue

                if response.status_code >= 500:
                    candidate_keys = self._candidate_keys(gatekeeper, estimated_tokens, exclude_keys=attempted_keys)
                    if candidate_keys:
                        continue

                return payload, response.status_code
            except Exception as exc:
                last_payload = {"error": str(exc)}
                last_status = 502
                candidate_keys = self._candidate_keys(gatekeeper, estimated_tokens, exclude_keys=attempted_keys)
                if candidate_keys:
                    continue

        return last_payload or {"error": "Credits exhausted for your API segment"}, last_status


def _public_base_url():
    candidates = [
        os.environ.get("KEEPALIVE_URL", ""),
        os.environ.get("RENDER_EXTERNAL_URL", ""),
        os.environ.get("RENDER_URL", ""),
        os.environ.get("PUBLIC_BASE_URL", ""),
    ]

    for candidate in candidates:
        candidate = candidate.strip().rstrip("/")
        if candidate:
            return candidate

    render_service = os.environ.get("RENDER_SERVICE_NAME", "").strip()
    render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if render_service and render_host:
        return f"https://{render_service}.{render_host}".rstrip("/")

    return ""


def _firebase_auth_query():
    secret = os.getenv("FIREBASE_SECRET", "")
    return f"?auth={secret}" if secret else ""


def _firebase_path(path):
    firebase_url = os.getenv("FIREBASE_URL", "").rstrip("/")
    if not firebase_url:
        return None
    return f"{firebase_url}/{path}.json{_firebase_auth_query()}"


def _load_model_registry_snapshot(force=False):
    global _model_registry_cache
    global _model_registry_cache_at

    with _model_registry_lock:
        if not force and _model_registry_cache and (time.time() - _model_registry_cache_at) < 60:
            return dict(_model_registry_cache)

    merged = {}
    for path in OmniTitanManager.MODEL_PROVIDER_PATHS.values():
        firebase_path = _firebase_path(path)
        if not firebase_path:
            continue

        try:
            response = requests.get(firebase_path, timeout=3)
            if response.status_code != 200 or not response.json():
                continue

            data = response.json()
            if isinstance(data, dict):
                for alias, real_model in data.items():
                    alias = str(alias).strip()
                    real_model = str(real_model).strip()
                    if alias and real_model:
                        merged[alias] = real_model
            elif isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    alias = str(item.get("alias", "")).strip()
                    real_model = str(item.get("model", "")).strip()
                    if alias and real_model:
                        merged[alias] = real_model
        except Exception:
            continue

    with _model_registry_lock:
        if merged:
            _model_registry_cache = merged
            _model_registry_cache_at = time.time()
        return dict(_model_registry_cache)


def _current_model_mapping():
    with _manager_lock:
        if omni is not None:
            return dict(omni.model_mapping)
    return _load_model_registry_snapshot()


def _initialize_omni():
    global omni
    global _manager_error
    global _manager_initializing

    try:
        manager = OmniTitanManager()
        with _manager_lock:
            omni = manager
            _manager_error = None
            _manager_initializing = False
        with _model_registry_lock:
            global _model_registry_cache
            global _model_registry_cache_at
            _model_registry_cache = dict(manager.model_mapping)
            _model_registry_cache_at = time.time()
        print("[Omni-Titan] Manager warm-up completed.")
    except Exception as exc:
        with _manager_lock:
            omni = None
            _manager_error = str(exc)
            _manager_initializing = False
        print(f"[Omni-Titan] Manager warm-up failed: {exc}")


def _start_manager_init():
    global _manager_initializing
    global _manager_error

    with _manager_lock:
        if omni is not None or _manager_initializing:
            return
        _manager_initializing = True
        _manager_error = None

    thread = threading.Thread(target=_initialize_omni, daemon=True)
    thread.start()
    print("[Omni-Titan] Manager warm-up started.")


def _manager_status():
    with _manager_lock:
        if omni is not None:
            return "ready", None
        if _manager_initializing:
            return "warming_up", None
        if _manager_error:
            return "error", _manager_error
        return "idle", None


def _require_omni():
    _start_manager_init()
    _start_keep_alive()

    with _manager_lock:
        if omni is not None:
            return omni, None

        if _manager_initializing:
            return None, ({"error": "Omni-Titan is warming up"}, 503)

        if _manager_error:
            return None, ({"error": "Omni-Titan initialization failed", "details": _manager_error}, 503)

    return None, ({"error": "Omni-Titan is warming up"}, 503)


@app.before_request
def bootstrap_background_tasks():
    _ensure_background_tasks_started()


@app.route("/", methods=["GET"])
def root():
    manager_state, manager_error = _manager_status()
    payload = {
        "status": "ok",
        "service": "Omni-Titan",
        "manager": manager_state,
    }
    if manager_error:
        payload["manager_error"] = manager_error
    return jsonify(payload), 200


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    return "", 204


@app.route("/v1/chat/completions", methods=["POST"])
def chat():
    manager, error_response = _require_omni()
    if error_response:
        payload, status = error_response
        return jsonify(payload), status

    gatekeeper = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    data = request.json or {}
    payload, status = manager.chat_completion(gatekeeper, data.get("messages", []), data.get("model", "Coder-Fast"))
    return jsonify(payload), status


@app.route("/v1/models", methods=["GET"])
def models():
    model_mapping = _current_model_mapping()
    return jsonify({"object": "list", "data": [{"id": model} for model in model_mapping]})


@app.route("/healthz", methods=["GET"])
def healthz():
    manager_state, manager_error = _manager_status()
    payload = {"status": "ok", "manager": manager_state}
    if manager_error:
        payload["manager_error"] = manager_error
    return jsonify(payload), 200


@app.route("/credit", methods=["POST"])
def credit():
    manager, error_response = _require_omni()
    if error_response:
        payload, status = error_response
        return jsonify(payload), status

    gatekeeper = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if gatekeeper not in manager.gatekeepers:
        return jsonify({"error": "Unauthorized"}), 401

    with manager.lock:
        segment = manager.gatekeeper_segments.get(gatekeeper, [])
        remaining = sum(manager.ollama_health[key]["remaining_tokens"] for key in segment if key in manager.ollama_health)
        capacity = len(segment) * manager.daily_limit

    return jsonify(
        {
            "remaining_credits": remaining,
            "total_capacity": capacity,
            "usage_percentage": f"{((capacity - remaining) / capacity * 100):.2f}%" if capacity else "0.00%",
        }
    )


def _keep_alive(port):
    time.sleep(30)
    while True:
        time.sleep(600)
        try:
            public_base_url = _public_base_url()
            if public_base_url:
                ping_url = f"{public_base_url}/healthz?ts={int(time.time())}"
                response = requests.get(
                    ping_url,
                    headers={"User-Agent": "omni-titan-keepalive/1.0", "Cache-Control": "no-cache"},
                    timeout=15,
                )
                print(f"[Omni-Titan] External keep-alive ping status {response.status_code}: {ping_url}")
            else:
                ping_url = f"http://127.0.0.1:{port}/healthz"
                response = requests.get(ping_url, timeout=5)
                print(
                    "[Omni-Titan] No public keep-alive URL configured. "
                    f"Local ping status {response.status_code}: {ping_url}"
                )
        except Exception:
            print("[Omni-Titan] Keep-alive ping failed.")


def _start_keep_alive():
    global _keepalive_started

    with _keepalive_lock:
        if _keepalive_started:
            return

        port = int(os.environ.get("PORT", 5000))
        thread = threading.Thread(target=_keep_alive, args=(port,), daemon=True)
        thread.start()
        _keepalive_started = True
        print(f"[Omni-Titan] Keep-alive thread started on port {port}")


def _ensure_background_tasks_started():
    _start_manager_init()
    _start_keep_alive()


if __name__ == "__main__":
    _ensure_background_tasks_started()
    port = int(os.environ.get("PORT", 5000))
    print(f"[Omni-Titan] Online on port {port}")
    app.run(host="0.0.0.0", port=port)
