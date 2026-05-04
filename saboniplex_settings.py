import json
import os
from typing import Any, Dict, List


SETTINGS_PATH = r"C:\SaboniPlex\settings.json"


def _parse_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


def _parse_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _split_csv(v: Any) -> List[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if v is None:
        return []
    return [x.strip() for x in str(v).split(",") if x.strip()]


def _load_json(path: str) -> Dict[str, Any]:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _default_settings() -> Dict[str, Any]:
    return {
        "group_titles": ["SaboniPlex"],
        "group_chat_ids": [],
        "speed_mode": "single",
        "max_concurrent_downloads": 1,
        "accept_recent_seconds": 300,
        "dry_run": False,
        "dedupe_by_size": True,
        "plex_use_tmdb_id_tag": False,
        "strict_tmdb_match": True,
        "notifications_enabled": True,
        "notify_on_error": True,
        "notify_on_complete": False,
    }


def ensure_settings_file(path: str = SETTINGS_PATH) -> None:
    if os.path.exists(path):
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_default_settings(), f, ensure_ascii=False, indent=2)


def load_settings(path: str = SETTINGS_PATH) -> Dict[str, Any]:
    ensure_settings_file(path)
    base = _default_settings()
    cfg = _load_json(path)
    env = os.environ

    group_titles = _split_csv(env.get("SABONIPLEX_GROUP_TITLES")) or _split_csv(cfg.get("group_titles")) or ["SaboniPlex"]
    chat_ids_raw = _split_csv(env.get("SABONIPLEX_GROUP_CHAT_IDS")) or _split_csv(cfg.get("group_chat_ids"))
    group_chat_ids: List[int] = []
    for x in chat_ids_raw:
        try:
            group_chat_ids.append(int(str(x).strip()))
        except Exception:
            pass
    speed_mode = str(env.get("SABONIPLEX_SPEED_MODE", cfg.get("speed_mode", "single"))).strip().lower() or "single"
    max_conc = _parse_int(env.get("SABONIPLEX_MAX_CONCURRENT_DOWNLOADS", cfg.get("max_concurrent_downloads", 1 if speed_mode == "single" else 10)), 1)

    out = {
        "group_titles": group_titles,
        "group_chat_ids": group_chat_ids,
        "speed_mode": speed_mode,
        "max_concurrent_downloads": max(1, min(12, max_conc)),
        "accept_recent_seconds": max(0, _parse_int(env.get("SABONIPLEX_ACCEPT_RECENT_SECONDS", cfg.get("accept_recent_seconds", 300)), 300)),
        "dry_run": _parse_bool(env.get("SABONIPLEX_DRY_RUN", cfg.get("dry_run", False)), False),
        "dedupe_by_size": _parse_bool(env.get("SABONIPLEX_DEDUPE_BY_SIZE", cfg.get("dedupe_by_size", True)), True),
        "plex_use_tmdb_id_tag": _parse_bool(env.get("SABONIPLEX_PLEX_USE_TMDB_ID_TAG", cfg.get("plex_use_tmdb_id_tag", False)), False),
        "strict_tmdb_match": _parse_bool(env.get("SABONIPLEX_STRICT_TMDB_MATCH", cfg.get("strict_tmdb_match", True)), True),
        "notifications_enabled": _parse_bool(env.get("SABONIPLEX_NOTIFICATIONS_ENABLED", cfg.get("notifications_enabled", True)), True),
        "notify_on_error": _parse_bool(env.get("SABONIPLEX_NOTIFY_ON_ERROR", cfg.get("notify_on_error", True)), True),
        "notify_on_complete": _parse_bool(env.get("SABONIPLEX_NOTIFY_ON_COMPLETE", cfg.get("notify_on_complete", False)), False),
    }
    return out
