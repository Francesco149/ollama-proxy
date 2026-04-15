import os
import logging
import importlib.util
from typing import Any, Dict

# Attempt to import tomllib (Python 3.11+) or tomli
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

logger = logging.getLogger("[config_loader]")

# Module-level variable to hold the configuration
# This ensures the file is only read once upon the first import
CONFIG: Dict[str, Any] = {}

def _load_config() -> Dict[str, Any]:
    """
    Loads the configuration from the specified TOML file.
    """
    config_path = os.environ.get("OLLAMA_PROXY_CONFIG", "/opt/ai-lab/ollama-proxy/config.toml")

    if tomllib is None:
        logger.error("No TOML parser found. Please install 'tomli' (for Python < 3.11).")
        raise ImportError("A TOML parser (tomllib or tomli) is required.")

    if not os.path.exists(config_path):
        logger.warning(f"Configuration file not found at: {config_path}")
        return {}

    try:
        with open(config_path, "rb") as f:
            logger.info(f"Loading configuration from {config_path}")
            return tomllib.load(f)
    except Exception as e:
        logger.error(f"Failed to parse configuration file {config_path}: {e}")
        return {}

# Initialize the global CONFIG dictionary
CONFIG = _load_config()

def get_config() -> Dict[str, Any]:
    """
    Returns the loaded configuration dictionary.
    """
    return CONFIG
