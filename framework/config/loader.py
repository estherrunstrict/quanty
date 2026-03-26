"""Config loader — YAML params + .env secrets."""
import os
import logging
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)


def load_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """Load strategy parameters from config.yaml."""
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error(f"Config file not found: {config_path}")
        raise
    except yaml.YAMLError as e:
        logger.error(f"Config parse error: {e}")
        raise


def load_secrets(env_path: str = ".env") -> Dict[str, str]:
    """Load secrets from .env file. Also checks environment variables."""
    secrets = {}

    # Load from .env file if it exists
    if os.path.exists(env_path):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except ImportError:
            # Manual .env parsing as fallback
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, value = line.split("=", 1)
                        os.environ[key.strip()] = value.strip()

    # Read required env vars
    required = [
        "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_CANO", "KIS_ACNT_PRDT_CD",
        "KIS_URL_BASE", "DISCORD_WEBHOOK_URL",
    ]
    optional = ["UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY"]

    for key in required:
        val = os.environ.get(key)
        if not val:
            logger.warning(f"Missing required env var: {key}")
        secrets[key] = val or ""

    for key in optional:
        secrets[key] = os.environ.get(key, "")

    return secrets
