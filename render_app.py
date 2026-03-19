import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parent / "llm-provider1" / "key_manager.py"
SPEC = importlib.util.spec_from_file_location("llm_provider_key_manager", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load key manager module from {MODULE_PATH}")

MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

app = MODULE.app
application = app
