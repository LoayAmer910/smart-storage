import json
import os

# תצורת מיקומי אחסון (8.5). קובץ קטן, עצמאי, ללא תלות במודולים אחרים -
# database_manager/archive_manager/main נשענים עליו, לא להפך.
#
# data_root: השורש שמכיל archives/, temp/ ואת smartarchive.db.
# workspace_root: אם None, ברירת המחדל היא data_root/workspace.
#
# הקובץ עצמו (_CONFIG_FILE) תמיד נקרא ביחס לתיקיית העבודה הנוכחית, בדיוק
# כמו data_root המחדל עצמו - כך שכל sandbox/בדיקה עם cwd משלה מקבלת תצורה נקייה.

_CONFIG_FILE = "storagearch_config.json"

_DEFAULT_DATA_ROOT = "SmartArchiveData"

_state = {
    "data_root": _DEFAULT_DATA_ROOT,
    "workspace_root": None,
}


def load():
    global _state
    _state = {"data_root": _DEFAULT_DATA_ROOT, "workspace_root": None}
    if os.path.isfile(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as config_file:
                data = json.load(config_file)
            if isinstance(data.get("data_root"), str) and data["data_root"].strip():
                _state["data_root"] = data["data_root"]
            if isinstance(data.get("workspace_root"), str) and data["workspace_root"].strip():
                _state["workspace_root"] = data["workspace_root"]
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_state)


def save(data_root=None, workspace_root=None):
    if data_root is not None:
        _state["data_root"] = data_root
    if workspace_root is not None:
        _state["workspace_root"] = workspace_root

    temp_path = _CONFIG_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as config_file:
        json.dump(_state, config_file, indent=2)
    os.replace(temp_path, _CONFIG_FILE)
    return dict(_state)


def current():
    return dict(_state)


def data_root():
    return _state["data_root"]


def archives_dir():
    return os.path.join(data_root(), "archives")


def workspace_dir():
    return _state["workspace_root"] or os.path.join(data_root(), "workspace")


def temp_dir():
    return os.path.join(data_root(), "temp")


def log_dir():
    return os.path.join(data_root(), "logs")


def db_path():
    return os.path.join(data_root(), "smartarchive.db")
