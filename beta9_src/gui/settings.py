import json
import os

import storage_config

# הגדרות ברמת ה-GUI בלבד (לא לוגיקת ארכיון/DB) - קובץ קטן שנודד יחד עם data_root הנוכחי.
_SETTINGS_FILE_NAME = "gui_settings.json"

_DEFAULTS = {
    "default_max_archive_size": 2147483648,  # 2GB, תואם את ברירת המחדל ב-database_manager
}


def _settings_path():
    return os.path.join(storage_config.data_root(), _SETTINGS_FILE_NAME)


def load_settings():
    settings_path = _settings_path()
    if os.path.isfile(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as settings_file:
                data = json.load(settings_file)
            merged = dict(_DEFAULTS)
            merged.update({key: value for key, value in data.items() if key in _DEFAULTS})
            return merged
        except (json.JSONDecodeError, OSError):
            return dict(_DEFAULTS)
    return dict(_DEFAULTS)


def save_settings(settings):
    settings_path = _settings_path()
    folder = os.path.dirname(settings_path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temp_path = settings_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as settings_file:
        json.dump(settings, settings_file, indent=2)
    os.replace(temp_path, settings_path)
