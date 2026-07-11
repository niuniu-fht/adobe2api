import json
import threading
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "config.json"
LEGACY_CONFIG_FILE = DATA_DIR / "config.json"


class ConfigManager:
    def __init__(self):
        self._lock = threading.Lock()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Default config
        self.config = {
            "api_key": "projectx_webapp",
            "admin_username": "admin",
            "admin_password": "admin",
            "admin_session_secret": "adobe2api-change-this-session-secret",
            "public_base_url": "http://127.0.0.1:6001/",
            "proxy": "",
            "use_proxy": False,
            "generate_timeout": 300,
            "refresh_interval_hours": 15,
            "retry_enabled": True,
            "retry_max_attempts": 3,
            "retry_backoff_seconds": 1.0,
            "retry_on_status_codes": [429, 451, 500, 502, 503, 504],
            "retry_on_error_types": ["timeout", "connection", "proxy"],
            "token_rotation_strategy": "round_robin",
            "batch_concurrency": 5,
            "generated_max_size_mb": 1024,
            "generated_prune_size_mb": 200,
            "gpt_image_quality": "low",
            "gpt_image_model_qualities": {"gpt-image-2-high": "medium"},
        }
        self.load()

    def load(self):
        with self._lock:
            source = CONFIG_FILE if CONFIG_FILE.exists() else LEGACY_CONFIG_FILE
            if source.exists():
                try:
                    data = json.loads(source.read_text(encoding="utf-8"))
                    for k, v in data.items():
                        if k in self.config:
                            self.config[k] = v
                    if source == LEGACY_CONFIG_FILE and not CONFIG_FILE.exists():
                        CONFIG_FILE.write_text(
                            json.dumps(self.config, indent=2), encoding="utf-8"
                        )
                except Exception:
                    pass

    def save(self):
        with self._lock:
            CONFIG_FILE.write_text(json.dumps(self.config, indent=2), encoding="utf-8")

    def get(self, key, default=None):
        with self._lock:
            return self.config.get(key, default)

    def set(self, key, value):
        with self._lock:
            self.config[key] = value
        self.save()

    def get_all(self):
        with self._lock:
            return dict(self.config)

    def update_all(self, data: dict):
        with self._lock:
            for k, v in data.items():
                if k in self.config:
                    self.config[k] = v
        self.save()


config_manager = ConfigManager()
