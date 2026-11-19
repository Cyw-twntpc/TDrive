import json
import os
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

class SettingsManager:
    """Manages application settings stored in a physical JSON file."""
    
    DEFAULT_SETTINGS = {
        "language": "zh-TW",
        "theme": "light",
        "defaultDownloadPath": "",
        "useDefaultDownloadPath": False
    }

    def __init__(self, settings_dir="file/user_data", settings_filename="settings.json"):
        # Resolve the absolute path based on the current working directory
        self.settings_dir = os.path.abspath(os.path.join(os.getcwd(), settings_dir))
        self.settings_file = os.path.join(self.settings_dir, settings_filename)
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        """Ensures the settings directory and file exist, initializing with defaults if necessary."""
        if not os.path.exists(self.settings_dir):
            try:
                os.makedirs(self.settings_dir)
            except Exception as e:
                logger.error(f"Failed to create settings directory {self.settings_dir}: {e}")
                
        if not os.path.exists(self.settings_file):
            logger.info("Settings file not found. Creating default settings file.")
            self.restore_defaults()

    def get_all(self):
        """Returns all current settings as a dictionary."""
        try:
            if not os.path.exists(self.settings_file):
                self._ensure_file_exists()
            with open(self.settings_file, "r", encoding="utf-8") as f:
                settings = json.load(f)
                
            # Merge with defaults to ensure missing keys are present
            result = self.DEFAULT_SETTINGS.copy()
            result.update(settings)
            return result
        except Exception as e:
            logger.error(f"Error reading settings file: {e}")
            return self.DEFAULT_SETTINGS.copy()

    def get(self, key, default=None):
        """Gets a specific setting value."""
        settings = self.get_all()
        return settings.get(key, default if default is not None else self.DEFAULT_SETTINGS.get(key))

    def set(self, key, value):
        """Updates a specific setting value and saves to file."""
        settings = self.get_all()
        settings[key] = value
        self._save(settings)

    def _save(self, settings):
        """Writes the settings dictionary to the physical file."""
        try:
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Error saving settings file: {e}")

    def restore_defaults(self):
        """Restores all settings to their default values and saves to file."""
        self._save(self.DEFAULT_SETTINGS.copy())
        logger.info("Settings restored to default values.")


def get_localized_text(key: str, fallback: str = "", locale_dir: Optional[str] = None) -> str:
    """Resolve a dotted i18n key using the active language setting and locale JSON files."""
    if not key:
        return fallback

    try:
        lang = SettingsManager().get("language", "zh-TW") or "zh-TW"
    except Exception as e:
        logger.warning(f"Failed to read language setting for '{key}': {e}")
        lang = "zh-TW"

    base_dir = Path(locale_dir) if locale_dir else Path(__file__).resolve().parents[2] / "web" / "locales"
    candidates = [base_dir / f"{lang}.json"]
    if lang != "zh-TW":
        candidates.append(base_dir / "zh-TW.json")

    for locale_path in candidates:
        if not locale_path.is_file():
            continue
        try:
            with locale_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            current = data
            for part in key.split("."):
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    current = None
                    break
            if isinstance(current, str):
                return current
        except Exception as e:
            logger.warning(f"Failed to load localized key '{key}' from {locale_path}: {e}")

    return fallback
