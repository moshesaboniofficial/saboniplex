import os
import json
import time
import shutil
import re
import math
import difflib
from typing import Optional
from typing import Tuple
from typing import Dict
from typing import Any
from typing import List

import requests
import tdjson
from saboniplex_settings import load_settings


def _env_get(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    if val is not None:
        return str(val)
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                val, _ = winreg.QueryValueEx(key, name)
                return str(val)
        except Exception:
            pass
    return default


# ========= CONFIG =========
API_ID = int(_env_get("SABONIPLEX_API_ID", "0") or "0")
API_HASH = _env_get("SABONIPLEX_API_HASH", "")
GROUP_TITLE = "SaboniPlex"

# איפה TDLib ישמור DB וקבצים זמניים (לא ה-Plex)
TDLIB_DB_DIR = r"C:\SaboniPlex\tdlib_db"
TDLIB_FILES_DIR = r"C:\SaboniPlex\tdlib_files"

# לאן להוריד פיזית לפני שאתה מסדר ל-Plex (אפשר גם ישירות ל-F:\Incoming אם תרצה)
DOWNLOAD_DIR = r"C:\SaboniPlex\Incoming"

# ==========================

MOVIES_DIR = r"F:\Movies"
KIDS_MOVIES_DIR = r"F:\Kids Movies"
ISRAELI_MOVIES_DIR = r"F:\Israeli Movies"
TV_DIR     = r"F:\Series"
NEEDS_REVIEW_DIR = r"F:\Movies\_Needs Review"
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".webm"}

TMDB_API_KEY = _env_get("SABONIPLEX_TMDB_API_KEY", "")
TMDB_TIMEOUT = 12
_tmdb_session = requests.Session()
TMDB_CACHE_TTL_SEC = 6 * 60 * 60
_tmdb_cache: Dict[str, Tuple[float, dict]] = {}
DECISIONS_LOG_PATH = r"C:\SaboniPlex\classification_decisions.jsonl"
OVERRIDES_PATH = r"C:\SaboniPlex\classification_overrides.json"

OPENAI_API_KEY = _env_get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = _env_get("SABONIPLEX_AI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
OPENAI_TIMEOUT_SEC = float(_env_get("SABONIPLEX_AI_TIMEOUT_SEC", "8") or "8")
GROQ_API_KEY = _env_get("GROQ_API_KEY", "").strip()
GROQ_MODEL = _env_get("SABONIPLEX_GROQ_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"
GROQ_TIMEOUT_SEC = float(_env_get("SABONIPLEX_GROQ_TIMEOUT_SEC", "8") or "8")
MOVE_ACTIONS_LOG_PATH = r"C:\SaboniPlex\move_actions.jsonl"

# Local safety overrides: block known-bad TMDB TV mappings by cleaned query.
# If TMDB consistently resolves a Hebrew show title to the wrong English series,
# add it here to force fallback to the parsed filename/caption show name.
TMDB_TV_QUERY_BLOCKLIST = {
    "כללי המשחק",
}


def td_send(client_id: int, obj: dict):
    tdjson.td_send(client_id, json.dumps(obj).encode("utf-8"))


def td_receive(timeout: float = 1.0) -> Optional[dict]:
    raw = tdjson.td_receive(timeout)
    if not raw:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except Exception:
        return None


def td_execute(obj: dict) -> Optional[dict]:
    raw = tdjson.td_execute(json.dumps(obj).encode("utf-8"))
    if not raw:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except Exception:
        return None


def ensure_dirs():
    os.makedirs(TDLIB_DB_DIR, exist_ok=True)
    os.makedirs(TDLIB_FILES_DIR, exist_ok=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(MOVIES_DIR, exist_ok=True)
    os.makedirs(KIDS_MOVIES_DIR, exist_ok=True)
    os.makedirs(ISRAELI_MOVIES_DIR, exist_ok=True)
    os.makedirs(TV_DIR, exist_ok=True)
    os.makedirs(NEEDS_REVIEW_DIR, exist_ok=True)
    log_parent = os.path.dirname(DECISIONS_LOG_PATH)
    if log_parent:
        os.makedirs(log_parent, exist_ok=True)
    overrides_parent = os.path.dirname(OVERRIDES_PATH)
    if overrides_parent:
        os.makedirs(overrides_parent, exist_ok=True)
    if not os.path.exists(OVERRIDES_PATH):
        with open(OVERRIDES_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
    move_parent = os.path.dirname(MOVE_ACTIONS_LOG_PATH)
    if move_parent:
        os.makedirs(move_parent, exist_ok=True)


def set_tdlib_parameters(client_id: int):
    if not API_ID or not API_HASH:
        raise RuntimeError("Missing SABONIPLEX_API_ID/SABONIPLEX_API_HASH")
    td_send(client_id, {
        "@type": "setTdlibParameters",
        "database_directory": TDLIB_DB_DIR,
        "files_directory": TDLIB_FILES_DIR,
        "use_message_database": True,
        "use_secret_chats": False,
        "api_id": API_ID,
        "api_hash": API_HASH,
        "system_language_code": "en",
        "device_model": "SaboniPlex",
        "system_version": "Windows",
        "application_version": "1.0",
        "enable_storage_optimizer": True,
    })


YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
TV_S_E_RE = re.compile(r"\bS(?P<s>\d{1,2})\s*[-._]?\s*E(?P<e>\d{1,3})\b", re.IGNORECASE)
TV_X_RE   = re.compile(r"\b(?P<s>\d{1,2})\s*x\s*(?P<e>\d{1,3})\b", re.IGNORECASE)
SEP = r"[\s._-]"
TV_HE_SHORT_RE = re.compile(rf"(?:^|{SEP})ע{SEP}*(?P<s>\d{{1,2}}){SEP}*פ{SEP}*(?P<e>\d{{1,3}})(?:{SEP}|$)")
TV_HE_LONG_RE  = re.compile(rf"(?:^|{SEP})עונה{SEP}*(?P<s>\d{{1,2}}){SEP}*פרק{SEP}*(?P<e>\d{{1,3}})(?:{SEP}|$)")


TITLE_LINE_LABEL_RE = re.compile(r"^\s*(?:name|title|שם)\s*[:\-]\s*(?P<title>.+?)\s*$", re.IGNORECASE)
YEAR_LINE_LABEL_RE = re.compile(r"^\s*(?:year|שנה)\s*[:\-]\s*(?P<year>19\d{2}|20\d{2})\s*$", re.IGNORECASE)
NOISE_LINE_RE = re.compile(
    r"^\s*(?:quality|genre|summary|plot|subtitle|subtitles|audio|source|codec|size|"
    r"איכות|ז[׳'`\"]?אנר|תקציר|תרגום|כתוביות|דיבוב|מדובב|קודד|הועלה|פורסם)\b",
    re.IGNORECASE,
)
BRACKET_RELEASE_TAG_RE = re.compile(r"\[(?:yts|yify|rarbg|eztv|ettv|psa|amzn|nf|web)[^\]]*\]", re.IGNORECASE)
TRAILING_RELEASE_TOKEN_RE = re.compile(
    r"(?:\b(?:web(?:[ ._-]?(?:dl|rip))?|bluray|brrip|bdrip|hdrip|hdr|uhd|"
    r"x264|x265|h[ .]?264|h[ .]?265|hevc|av1|10bit|repack|proper|remux|"
    r"aac(?:\s*\d(?:[ ._]\d)?)?|ac3(?:\s*\d(?:[ ._]\d)?)?|eac3(?:\s*\d(?:[ ._]\d)?)?|"
    r"ddp(?:\s*\d(?:[ ._]\d)?)?|dd(?:\s*\d(?:[ ._]\d)?)?|dts(?:[ ._-]hd)?|truehd|atmos|"
    r"yts|yify|rarbg|eztv|ettv|psa|amzn|nf|djt|edith)\b[\s._-]*)+$",
    re.IGNORECASE,
)


def windows_safe_filename(name: str, max_len: int = 120) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = "Unknown"
    return name[:max_len].rstrip(" .")


def strip_known_ext(filename: str) -> str:
    base, ext = os.path.splitext(filename)
    if ext.lower() in VIDEO_EXTS:
        return base
    return filename


def ensure_unique(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while True:
        p = f"{base} ({i}){ext}"
        if not os.path.exists(p):
            return p
        i += 1


def extract_year(text: str) -> Optional[int]:
    m = YEAR_RE.search(text or "")
    if not m:
        return None
    y = int(m.group(1))
    return y if 1900 <= y <= 2100 else None


def cleanup_query(text: str) -> str:
    t = text or ""
    t = re.split(r"\bתקציר\b", t, maxsplit=1)[0]
    # strip common promo/noise phrases that often appear in Telegram captions
    t = re.sub(r"\b(אחרון\s*לעונה|אחרון\s*לעונה\s*!+|פרק\s*אחרון)\b", " ", t)
    t = re.sub(r"\b(איכות\s*[:：]?|קודד\s*ו?הועלה|הועלה\s*בבלעדיות|בלעדיות|אימפריית\s*הקולנוע\s*הטלגרמי)\b", " ", t)
    t = re.sub(r"\b(צפייה\s*מהנה|צפייה\s*נעימה|להורדה|להעלאה|לינק|קישור)\b", " ", t)
    t = re.sub(r"#\S+", " ", t)
    # normalize common filename separators early (so prefix stripping works even on '_' names)
    t = re.sub(r"[_\.\-]+", " ", t)
    t = BRACKET_RELEASE_TAG_RE.sub(" ", t)
    # strip credit/uploader suffix like "ע\"י פלוני" (often appears at end)
    t = re.sub(r"(?:^|\s)ע\s*\.?\s*י\.?\s+.*$", " ", t)
    # remove common source/channel prefixes
    if "|" in t:
        t = t.split("|")[-1]
    t = re.sub(r"\b(זירה\s*מדיה|zira\s*media)\b", " ", t, flags=re.IGNORECASE)
    # strip common distributor prefixes like "נתי מדיה" / "שבי מדיה" / "דב סרטים" at start
    t = re.sub(r"^\s*[\u0590-\u05FFA-Za-z0-9]+\s+(מדיה|סרטים)\b", " ", t)
    # strip common uploader/release tags that break TMDB matching
    t = re.sub(r"\b(גוזלן)\b", " ", t)
    t = re.sub(r"\b(תרגום\s*מובנה|מדובב|דיבוב|כתוביות|תרגום|עברית)\b", " ", t)
    t = re.sub(r"(?:^|\s)ת\s*\.?\s*מ\.?\s*(?:\s|$)", " ", t)
    t = re.sub(r"\b(mp4|mkv|avi|mov|wmv|m4v|webm)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"[💥🔥✅⭐️]+", " ", t)
    t = re.sub(r"\b(1080p|720p|2160p|4k|web|web[- ]?dl|web[- ]?rip|bluray|brrip|hdr|dv|x264|x265|h\.?264|h\.?265|hevc|av1|repack)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(ac3|aac|dts|ddp(?:\d(?:\.\d)?)?|truehd|atmos)\s*\d?(?:[ ._]\d)?\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(yts|yify|rarbg|eztv|ettv|psa|amzn|nf|djt|edith)\b", " ", t, flags=re.IGNORECASE)
    # remove trailing release-group tags like "-DJT" / " H264-DJT"
    t = t.strip()
    t = TRAILING_RELEASE_TOKEN_RE.sub(" ", t)
    t = re.sub(r"(?:^|[\s._-])[A-Z]{2,6}\s*$", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_caption_title_year(caption_text: str) -> Tuple[str, Optional[int]]:
    title = ""
    year = extract_year(caption_text or "")
    lines = [ln.strip(" \t-:|") for ln in (caption_text or "").splitlines()]

    for line in lines:
        if not line:
            continue
        m_year = YEAR_LINE_LABEL_RE.match(line)
        if m_year:
            try:
                year = int(m_year.group("year"))
            except Exception:
                pass
            continue
        m_title = TITLE_LINE_LABEL_RE.match(line)
        if m_title:
            candidate = cleanup_query(m_title.group("title"))
            if candidate:
                return candidate, year

    for line in lines[:6]:
        if not line or NOISE_LINE_RE.match(line):
            continue
        cleaned = cleanup_query(line)
        if not cleaned:
            continue
        letters = re.findall(r"[A-Za-z\u0590-\u05ff]", cleaned)
        if len(letters) < 3:
            continue
        title = cleaned
        break

    return title, year


def parse_tv(text: str) -> Optional[Tuple[str, int, int]]:
    t = (text or "").strip()

    m = TV_S_E_RE.search(t)
    if m:
        show = t[:m.start()].strip(" -._|[]()") or t[m.end():].strip(" -._|[]()") or t
        show = windows_safe_filename(cleanup_query(show) or show, 80)
        return show, int(m.group("s")), int(m.group("e"))

    m = TV_X_RE.search(t)
    if m:
        show = t[:m.start()].strip(" -._|[]()") or t[m.end():].strip(" -._|[]()") or t
        show = windows_safe_filename(cleanup_query(show) or show, 80)
        return show, int(m.group("s")), int(m.group("e"))

    m = TV_HE_LONG_RE.search(t)
    if m:
        show = t[:m.start()].strip(" -._|[]()") or t[m.end():].strip(" -._|[]()") or t
        show = windows_safe_filename(cleanup_query(show) or show, 80)
        return show, int(m.group("s")), int(m.group("e"))

    m = TV_HE_SHORT_RE.search(t)
    if m:
        show = t[:m.start()].strip(" -._|[]()") or t[m.end():].strip(" -._|[]()") or t
        show = windows_safe_filename(cleanup_query(show) or show, 80)
        return show, int(m.group("s")), int(m.group("e"))

    return None


def season_folder_name(season: int) -> str:
    return f"Season {season:02d}"


def tv_filename(show: str, season: int, episode: int, ext: str) -> str:
    return f"{show} - S{season:02d}E{episode:02d}{ext}"


def tmdb_get(endpoint: str, params: dict) -> Optional[dict]:
    if not TMDB_API_KEY:
        log_print("TMDB disabled: missing SABONIPLEX_TMDB_API_KEY")
        return None
    url = f"https://api.themoviedb.org/3/{endpoint}"
    params = dict(params)
    params["api_key"] = TMDB_API_KEY

    cache_key = endpoint + "|" + json.dumps(params, sort_keys=True, ensure_ascii=False)
    now = time.time()
    cached = _tmdb_cache.get(cache_key)
    if cached:
        ts, payload = cached
        if now - ts <= TMDB_CACHE_TTL_SEC:
            return payload

    delay = 0.4
    for _ in range(4):
        try:
            r = _tmdb_session.get(url, params=params, timeout=TMDB_TIMEOUT)
            if r.status_code == 200:
                payload = r.json()
                _tmdb_cache[cache_key] = (now, payload)
                return payload

            # rate limit / transient errors -> backoff
            if r.status_code in (429, 500, 502, 503, 504):
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        delay = max(delay, float(ra))
                    except Exception:
                        pass
        except Exception:
            pass

        time.sleep(delay)
        delay = min(3.0, delay * 1.7)

    return None


def _norm_for_match(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^0-9a-z\u0590-\u05ff]+", "", s)
    return s


def _similarity(a: str, b: str) -> float:
    a2 = _norm_for_match(a)
    b2 = _norm_for_match(b)
    if not a2 or not b2:
        return 0.0
    if a2 == b2:
        return 1.0
    return difflib.SequenceMatcher(None, a2, b2).ratio()


def tmdb_find_movie(query: str) -> Optional[Dict[str, Any]]:
    q = cleanup_query(strip_known_ext(query or ""))
    if not q:
        return None
    year = extract_year(q)
    name_only = YEAR_RE.sub("", q).strip()

    def pick_best_movie(results: list) -> Tuple[Optional[dict], float]:
        if not results:
            return None, 0.0
        best = None
        best_score = 0.0
        for r in results[:8]:
            if not isinstance(r, dict):
                continue
            title = r.get("title") or ""
            original = r.get("original_title") or ""
            sim = max(_similarity(name_only, title), _similarity(name_only, original))
            year_boost = 0.0
            if year:
                rd = (r.get("release_date") or "")
                if rd.startswith(str(year)):
                    year_boost = 0.18
            score = sim + year_boost
            if score > best_score:
                best_score = score
                best = r

        # reject very weak matches
        if not best:
            return None, 0.0
        if best_score < 0.52 and not (year and best_score >= 0.40):
            return None, 0.0
        return best, float(best_score)

    # Try a couple of query variants; Hebrew filenames often include sequel numbers like "1".
    candidates: List[str] = []
    if name_only:
        candidates.append(name_only)
    name_no_nums = re.sub(r"\b\d+\b", " ", name_only).strip()
    if name_no_nums and name_no_nums != name_only:
        candidates.append(name_no_nums)

    best: Optional[dict] = None
    best_score = 0.0
    for cand in candidates:
        base_params = {"query": cand, "include_adult": "false"}
        if year:
            base_params["year"] = year

        data_en = tmdb_get("search/movie", {**base_params, "language": "en-US"}) or {}
        b, s = pick_best_movie(data_en.get("results") or [])

        if not b:
            data_he_search = tmdb_get("search/movie", {**base_params, "language": "he-IL"}) or {}
            b, s = pick_best_movie(data_he_search.get("results") or [])

        if b and s > best_score:
            best = b
            best_score = s

    if not best:
        return None

    movie_id = best.get("id")
    if not movie_id:
        return None

    details_en = tmdb_get(f"movie/{movie_id}", {"language": "en-US"}) or {}
    en_title = details_en.get("title") or details_en.get("original_title") or best.get("title") or best.get("original_title")

    release_date = details_en.get("release_date") or best.get("release_date") or ""
    yy = release_date[:4] if len(release_date) >= 4 else (str(year) if year else "")

    genres: List[str] = []
    for g in (details_en.get("genres") or []):
        if isinstance(g, dict) and g.get("name"):
            genres.append(str(g.get("name")))

    production_countries: List[str] = []
    for c in (details_en.get("production_countries") or []):
        if isinstance(c, dict) and c.get("iso_3166_1"):
            production_countries.append(str(c.get("iso_3166_1")))

    original_language = details_en.get("original_language") or best.get("original_language")

    he_title = None
    data_he = tmdb_get(f"movie/{movie_id}", {"language": "he-IL"})
    if data_he:
        he_title = data_he.get("title") or None
    if not he_title:
        he_title = best.get("title") if (best.get("original_language") == "he") else None

    if not en_title:
        return None

    return {
        "en_title": en_title,
        "original_title": details_en.get("original_title") or best.get("original_title") or "",
        "year": yy,
        "he_title": he_title,
        "movie_id": movie_id,
        "genres": genres,
        "original_language": original_language,
        "production_countries": production_countries,
    }


def _looks_like_hebrew(text: str) -> bool:
    return bool(re.search(r"[\u0590-\u05ff]", text or ""))


def _looks_kids_by_text(text: str) -> bool:
    t = (text or "").lower()
    kids_keywords = ["kids", "kid", "family", "animation", "pixar", "disney", "ילדים", "אנימציה", "משפחה", "דיסני", "פיקסאר"]
    return any(k in t for k in kids_keywords)


def _looks_israeli_by_text(text: str) -> bool:
    t = (text or "").lower()
    # "Hebrew"/"עברית" often means subtitle language, not Israeli production.
    return any(k in t for k in ["ישראלי", "israeli", "סרט ישראלי", "הפקה ישראלית"])


def _append_decision_log(payload: Dict[str, Any]):
    try:
        rec = dict(payload)
        rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(DECISIONS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _append_move_action(payload: Dict[str, Any]):
    try:
        rec = dict(payload)
        rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(MOVE_ACTIONS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def undo_last_move() -> bool:
    try:
        if not os.path.exists(MOVE_ACTIONS_LOG_PATH):
            return False
        with open(MOVE_ACTIONS_LOG_PATH, "r", encoding="utf-8") as f:
            rows = [ln.strip() for ln in f if ln.strip()]
        if not rows:
            return False
        last = json.loads(rows[-1])
        if last.get("action") != "move":
            return False
        src = str(last.get("src") or "")
        dst = str(last.get("dst") or "")
        if not src or not dst or not os.path.exists(dst):
            return False
        parent = os.path.dirname(src)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.move(dst, src)
        _append_move_action({"action": "undo_move", "src": src, "dst": dst, "status": "ok"})
        return True
    except Exception:
        return False


def _candidate_queries(filename_text: str, caption_text: str) -> List[str]:
    out: List[str] = []
    title_from_caption, year_from_caption = _extract_caption_title_year(caption_text or "")
    if title_from_caption:
        out.append(f"{title_from_caption} {year_from_caption}".strip() if year_from_caption else title_from_caption)
        out.append(title_from_caption)
    for raw in [caption_text or "", filename_text or ""]:
        cleaned = cleanup_query(strip_known_ext(raw))
        if cleaned:
            out.append(cleaned)
        if raw:
            out.append(strip_known_ext(raw).strip())
    dedup: List[str] = []
    seen = set()
    for v in out:
        key = (v or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        dedup.append(v.strip())
    return dedup


def _ai_extract_json_text(txt: str) -> Optional[Dict[str, Any]]:
    if not txt:
        return None
    try:
        m = re.search(r"\{.*\}", txt, flags=re.S)
        payload = json.loads(m.group(0) if m else txt)
        if isinstance(payload, dict):
            return payload
    except Exception:
        return None
    return None


def _ai_completion_json(prompt: str, *, max_tokens: int = 220) -> Optional[Dict[str, Any]]:
    if GROQ_API_KEY:
        try:
            r = _tmdb_session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": "Return JSON only, no markdown."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": int(max_tokens),
                },
                timeout=GROQ_TIMEOUT_SEC,
            )
            if r.status_code == 200:
                data = r.json()
                txt = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
                payload = _ai_extract_json_text(txt)
                if payload:
                    return payload
                log_print("AI Groq returned non-JSON response")
            else:
                log_print(f"AI Groq error {r.status_code}: {r.text[:220]}")
        except Exception as ex:
            log_print(f"AI Groq error: {ex}")

    if not OPENAI_API_KEY:
        return None
    try:
        r = _tmdb_session.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": OPENAI_MODEL, "input": prompt, "max_output_tokens": int(max_tokens), "temperature": 0},
            timeout=OPENAI_TIMEOUT_SEC,
        )
        if r.status_code != 200:
            log_print(f"AI OpenAI error {r.status_code}: {r.text[:220]}")
            return None
        data = r.json()
        txt = data.get("output_text") or ""
        payload = _ai_extract_json_text(txt)
        if payload:
            return payload
        log_print("AI OpenAI returned non-JSON response")
    except Exception as ex:
        log_print(f"AI OpenAI error: {ex}")
        return None
    return None


def _ai_guess_movie_query(filename_text: str, caption_text: str) -> Optional[str]:
    prompt = (
        "Extract a likely MOVIE title query for TMDB from noisy Telegram text.\n"
        "Return JSON only: {\"query\":\"...\",\"year\":\"... or empty\"}.\n"
        "If this looks like TV episode data return empty query.\n"
        f"filename: {filename_text}\n"
        f"caption: {caption_text}\n"
    )
    try:
        payload = _ai_completion_json(prompt, max_tokens=120) or {}
        q = (payload.get("query") or "").strip()
        y = (payload.get("year") or "").strip()
        if not q:
            return None
        if y and re.fullmatch(r"(19\d{2}|20\d{2})", y):
            return f"{q} {y}".strip()
        return q
    except Exception:
        return None


def _ai_resolve_media_metadata(filename_text: str, caption_text: str) -> Optional[Dict[str, Any]]:
    prompt = (
        "You classify one Telegram media item for Plex.\n"
        "Return JSON only with keys:\n"
        "{\"kind\":\"movie|series|unknown\",\"title_query\":\"...\",\"year\":\"YYYY or empty\","
        "\"season\":0,\"episode\":0,\"is_kids\":true|false,\"is_israeli\":true|false,\"confidence\":0..1}\n"
        "Use kind=series only when episode markers are clear, and then include season/episode numbers.\n"
        f"filename: {filename_text}\n"
        f"caption: {caption_text}\n"
    )
    payload = _ai_completion_json(prompt, max_tokens=180)
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("kind") or "unknown").strip().lower()
    if kind not in {"movie", "series", "unknown"}:
        kind = "unknown"
    title_query = str(payload.get("title_query") or "").strip()
    year = str(payload.get("year") or "").strip()
    if year and not re.fullmatch(r"(19\d{2}|20\d{2})", year):
        year = ""
    try:
        conf = float(payload.get("confidence"))
    except Exception:
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    try:
        season = max(0, int(payload.get("season") or 0))
    except Exception:
        season = 0
    try:
        episode = max(0, int(payload.get("episode") or 0))
    except Exception:
        episode = 0
    return {
        "kind": kind,
        "title_query": title_query,
        "year": year,
        "season": season,
        "episode": episode,
        "is_kids": bool(payload.get("is_kids")),
        "is_israeli": bool(payload.get("is_israeli")),
        "confidence": conf,
    }


def tmdb_find_movie_smart(filename_text: str, caption_text: str) -> Optional[Dict[str, Any]]:
    candidates = _candidate_queries(filename_text, caption_text)
    best: Optional[Dict[str, Any]] = None
    best_score = 0.0

    for q in candidates:
        found = tmdb_find_movie(q)
        if not found:
            continue
        cleaned_q = cleanup_query(q)
        score = max(
            _similarity(cleaned_q, found.get("en_title") or ""),
            _similarity(cleaned_q, found.get("he_title") or ""),
            _similarity(cleaned_q, found.get("original_title") or ""),
        )
        query_year = extract_year(q)
        if query_year and str(found.get("year") or "").strip() == str(query_year):
            score += 0.12
        if score > best_score:
            best_score = score
            best = found
        if score >= 0.90:
            return found

    if best:
        return best

    ai_q = _ai_guess_movie_query(filename_text, caption_text)
    if ai_q:
        found = tmdb_find_movie(ai_q)
        if found:
            found = dict(found)
            found["_ai_query_used"] = True
        return found
    return None


def _classify_movie_root_with_reason(found: Optional[Dict[str, Any]], combined_text: str) -> Tuple[str, str, float]:
    if found:
        root = pick_movie_root(found, combined_text)
        if root == KIDS_MOVIES_DIR:
            return root, "tmdb_genre_or_kids_keyword", 0.92
        if root == ISRAELI_MOVIES_DIR:
            return root, "tmdb_country_or_language", 0.93
        return root, "tmdb_movie_default", 0.88

    if _looks_kids_by_text(combined_text):
        return KIDS_MOVIES_DIR, "fallback_kids_keywords", 0.66
    if _looks_israeli_by_text(combined_text):
        return ISRAELI_MOVIES_DIR, "fallback_israeli_keywords", 0.67
    # Hebrew text alone is common in captions/subtitle notes and should not force Israeli folder.
    if _looks_like_hebrew(combined_text):
        return MOVIES_DIR, "fallback_hebrew_text_non_israeli", 0.56
    return MOVIES_DIR, "fallback_default_movies", 0.55


def _classify_movie_root_with_ai_hint(
    found: Optional[Dict[str, Any]],
    combined_text: str,
    ai_meta: Optional[Dict[str, Any]],
) -> Tuple[str, str, float]:
    if found:
        return _classify_movie_root_with_reason(found, combined_text)
    if ai_meta and float(ai_meta.get("confidence") or 0.0) >= 0.70:
        if ai_meta.get("is_kids"):
            return KIDS_MOVIES_DIR, "ai_hint_kids", 0.78
        if ai_meta.get("is_israeli"):
            return ISRAELI_MOVIES_DIR, "ai_hint_israeli", 0.79
    return _classify_movie_root_with_reason(None, combined_text)


def _load_overrides() -> List[Dict[str, Any]]:
    try:
        if not os.path.exists(OVERRIDES_PATH):
            return []
        with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []
    except Exception:
        return []


def _match_override(combined_text: str) -> Optional[Dict[str, Any]]:
    text = cleanup_query(combined_text or "").lower()
    if not text:
        return None
    for item in _load_overrides():
        pattern = str(item.get("match") or "").strip().lower()
        if not pattern:
            continue
        if pattern in text:
            return item
    return None


def pick_movie_root(found: Dict[str, Any], raw_text: str) -> str:
    t = (raw_text or "").lower()

    kids_keywords = ["ילדים", "kid", "kids", "family", "animation", "אנימציה", "דיסני", "pixar"]
    if any(k in t for k in kids_keywords):
        return KIDS_MOVIES_DIR

    genres = {(g or "").strip().lower() for g in (found.get("genres") or [])}
    if "family" in genres or "animation" in genres:
        return KIDS_MOVIES_DIR

    production_countries = {str(c).upper() for c in (found.get("production_countries") or []) if c}
    if "IL" in production_countries:
        return ISRAELI_MOVIES_DIR

    if (found.get("original_language") or "").lower() == "he":
        return ISRAELI_MOVIES_DIR

    return MOVIES_DIR


def plex_movie_name(en_title: str, year: str, movie_id: Optional[int], with_tmdb_tag: bool) -> str:
    base = f"{en_title} ({year})" if (en_title and year) else (en_title or "")
    base = windows_safe_filename(base, 120)
    if with_tmdb_tag and movie_id:
        return windows_safe_filename(f"{base} {{tmdb-{int(movie_id)}}}", 120)
    return base


def tmdb_find_tv(query: str) -> Optional[str]:
    def looks_like(q1: str, q2: str) -> bool:
        if not q1 or not q2:
            return False
        return _similarity(q1, q2) >= 0.66

    q = cleanup_query(query)
    if not q:
        return None
    q = TV_S_E_RE.sub("", q)
    q = TV_X_RE.sub("", q)
    q = TV_HE_SHORT_RE.sub("", q)
    q = TV_HE_LONG_RE.sub("", q)
    q = q.strip(" -._")

    if q in TMDB_TV_QUERY_BLOCKLIST:
        return None

    # English search first
    data_en = tmdb_get("search/tv", {"query": q, "language": "en-US"}) or {}
    for r in (data_en.get("results") or [])[:8]:
        if not isinstance(r, dict):
            continue
        name_local = r.get("name") or r.get("original_name") or ""
        if not looks_like(q, name_local):
            continue
        tv_id = r.get("id")
        if tv_id:
            details_en = tmdb_get(f"tv/{tv_id}", {"language": "en-US"}) or {}
            return details_en.get("name") or details_en.get("original_name") or r.get("original_name") or r.get("name")
        return r.get("original_name") or r.get("name")

    # Hebrew fallback (validate against Hebrew-localized title)
    data_he = tmdb_get("search/tv", {"query": q, "language": "he-IL"}) or {}
    for r in (data_he.get("results") or [])[:10]:
        if not isinstance(r, dict):
            continue
        name_local = r.get("name") or ""
        if not looks_like(q, cleanup_query(name_local)):
            continue
        tv_id = r.get("id")
        if tv_id:
            details_en = tmdb_get(f"tv/{tv_id}", {"language": "en-US"}) or {}
            return details_en.get("name") or details_en.get("original_name") or r.get("original_name") or r.get("name")
        return r.get("original_name") or r.get("name")

    return None


def plex_move(src_path: str, original_name: str, caption_text: Optional[str] = None) -> Optional[Dict[str, Any]]:
    settings = load_settings()
    dry_run = bool(settings.get("dry_run"))
    dedupe_by_size = bool(settings.get("dedupe_by_size", True))
    use_tmdb_id_tag = bool(settings.get("plex_use_tmdb_id_tag", True))
    strict_tmdb_match = bool(settings.get("strict_tmdb_match", True))
    base = os.path.basename(original_name or os.path.basename(src_path))

    # extension should follow the actual file on disk
    _, src_ext = os.path.splitext(src_path)
    _, base_ext = os.path.splitext(base)
    ext = (src_ext or base_ext or ".mp4").lower()
    if ext not in VIDEO_EXTS:
        ext = ".mp4"

    raw_text = strip_known_ext(base)

    caption_raw = strip_known_ext(str(caption_text or "").strip())

    tv_info = None
    tv_query_text = raw_text
    if caption_raw:
        tv_info = parse_tv(caption_raw)
        if tv_info:
            tv_query_text = caption_raw

    if not tv_info:
        tv_info = parse_tv(raw_text)
        tv_query_text = raw_text

    display_name: Optional[str] = None

    decision: Dict[str, Any] = {
        "source_path": src_path,
        "original_name": original_name,
        "caption_text": caption_text or "",
        "kind": "",
        "tmdb_used": False,
        "ai_used": False,
        "ai_confidence": 0.0,
        "confidence": 0.0,
        "reason": "",
        "dest_path": "",
    }

    if tv_info:
        show_guess, season, episode = tv_info
        # Prefer TMDB lookup by the parsed show name only (avoids matching on promo/caption noise).
        show_tmdb = tmdb_find_tv(show_guess) or tmdb_find_tv(tv_query_text)
        show = show_tmdb or show_guess
        show = windows_safe_filename(show or show_guess, 80)
        dest_dir = os.path.join(TV_DIR, show, season_folder_name(season))
        os.makedirs(dest_dir, exist_ok=True)
        final_name = windows_safe_filename(tv_filename(show, season, episode, ext), 140)
        display_name = strip_known_ext(final_name)
        out_path = ensure_unique(os.path.join(dest_dir, final_name))
        decision.update(
            {
                "kind": "series",
                "tmdb_used": bool(show_tmdb),
                "confidence": 0.92 if show_tmdb else 0.78,
                "reason": "parsed_season_episode",
                "series_name": show,
                "season": season,
                "episode": episode,
            }
        )
    else:
        mix = f"{raw_text} {caption_raw}".strip()
        ov = _match_override(mix)
        if ov:
            kind = str(ov.get("kind") or "").strip().lower()
            forced_name = windows_safe_filename(str(ov.get("name") or "").strip(), 120) or "Unknown"
            forced_year = str(ov.get("year") or "").strip()
            if kind == "series":
                season = int(ov.get("season") or 1)
                episode = int(ov.get("episode") or 1)
                show = windows_safe_filename(forced_name, 80)
                dest_dir = os.path.join(TV_DIR, show, season_folder_name(season))
                os.makedirs(dest_dir, exist_ok=True)
                final_name = windows_safe_filename(tv_filename(show, season, episode, ext), 140)
                display_name = strip_known_ext(final_name)
                out_path = ensure_unique(os.path.join(dest_dir, final_name))
                decision.update(
                    {
                        "kind": "series",
                        "tmdb_used": False,
                        "confidence": 0.99,
                        "reason": "override_series",
                        "series_name": show,
                        "season": season,
                        "episode": episode,
                    }
                )
            else:
                nice = f"{forced_name} ({forced_year})" if forced_year else forced_name
                root_key = str(ov.get("root") or "movies").strip().lower()
                if root_key == "kids":
                    root_dir = KIDS_MOVIES_DIR
                elif root_key == "israeli":
                    root_dir = ISRAELI_MOVIES_DIR
                elif root_key == "series":
                    root_dir = TV_DIR
                else:
                    root_dir = MOVIES_DIR
                movie_folder = os.path.join(root_dir, nice)
                os.makedirs(movie_folder, exist_ok=True)
                out_path = ensure_unique(os.path.join(movie_folder, f"{nice}{ext}"))
                display_name = nice
                decision.update(
                    {
                        "kind": "movie",
                        "tmdb_used": False,
                        "confidence": 0.99,
                        "reason": "override_movie",
                        "movie_name": nice,
                        "movie_year": forced_year,
                    }
                )
            found = None
        else:
            found = tmdb_find_movie_smart(raw_text, caption_raw)
            ai_meta = None
            ai_series_handled = False
            if found and found.get("_ai_query_used"):
                decision["ai_used"] = True
            if not found:
                ai_meta = _ai_resolve_media_metadata(raw_text, caption_raw)
                if ai_meta:
                    decision["ai_used"] = True
                    decision["ai_confidence"] = float(ai_meta.get("confidence") or 0.0)
                ai_q = ""
                if ai_meta:
                    ai_q = str(ai_meta.get("title_query") or "").strip()
                    ai_y = str(ai_meta.get("year") or "").strip()
                    if ai_q and ai_y and re.fullmatch(r"(19\d{2}|20\d{2})", ai_y):
                        ai_q = f"{ai_q} {ai_y}".strip()
                if ai_meta and ai_meta.get("kind") == "series":
                    ai_conf = float(ai_meta.get("confidence") or 0.0)
                    season = int(ai_meta.get("season") or 0)
                    episode = int(ai_meta.get("episode") or 0)
                    show_guess = windows_safe_filename(str(ai_meta.get("title_query") or "").strip(), 80)
                    if ai_conf >= 0.86 and show_guess and season > 0 and episode > 0:
                        show_tmdb = tmdb_find_tv(show_guess) or show_guess
                        show = windows_safe_filename(show_tmdb or show_guess, 80)
                        dest_dir = os.path.join(TV_DIR, show, season_folder_name(season))
                        os.makedirs(dest_dir, exist_ok=True)
                        final_name = windows_safe_filename(tv_filename(show, season, episode, ext), 140)
                        display_name = strip_known_ext(final_name)
                        out_path = ensure_unique(os.path.join(dest_dir, final_name))
                        decision.update(
                            {
                                "kind": "series",
                                "tmdb_used": bool(show_tmdb and show_tmdb != show_guess),
                                "ai_used": True,
                                "ai_confidence": ai_conf,
                                "confidence": 0.84,
                                "reason": "ai_series_episode",
                                "series_name": show,
                                "season": season,
                                "episode": episode,
                            }
                        )
                        ai_series_handled = True
                if ai_q and not ai_series_handled:
                    found = tmdb_find_movie(ai_q)

            if ai_series_handled:
                pass
            elif found:
                en_title = windows_safe_filename(found.get("en_title") or "", 110)
                yy = (found.get("year") or "").strip()
                movie_id = found.get("movie_id")
                nice = plex_movie_name(en_title, yy, movie_id, use_tmdb_id_tag)
                if not nice:
                    nice = windows_safe_filename(cleanup_query(raw_text) or raw_text, 120)
            else:
                cleaned = cleanup_query(raw_text) or raw_text
                # In strict mode, unresolved TMDB match is routed to review queue
                # instead of polluting the library with guessed names.
                if strict_tmdb_match:
                    review_base = windows_safe_filename(cleaned, 120) or "Unknown Movie"
                    nice = review_base
                    ai_conf = float((ai_meta or {}).get("confidence") or 0.0)
                    movie_folder = os.path.join(NEEDS_REVIEW_DIR, nice)
                    os.makedirs(movie_folder, exist_ok=True)
                    out_path = ensure_unique(os.path.join(movie_folder, f"{nice}{ext}"))
                    display_name = nice
                    decision.update(
                        {
                            "kind": "movie",
                            "tmdb_used": False,
                            "ai_used": bool(ai_meta),
                            "ai_confidence": ai_conf,
                            "confidence": max(0.20, min(0.69, ai_conf)),
                            "reason": str(decision.get("reason") or "strict_tmdb_no_match_needs_review"),
                            "movie_name": nice,
                            "movie_year": "",
                            "movie_id": None,
                        }
                    )
                else:
                    y = extract_year(cleaned)
                    name_wo_year = YEAR_RE.sub("", cleaned).strip()
                    if y and name_wo_year:
                        nice = windows_safe_filename(f"{name_wo_year} ({y})", 120)
                    else:
                        nice = windows_safe_filename(cleaned, 120)

            if ai_series_handled:
                pass
            elif found:
                display_name = nice
                root_dir, reason, conf = _classify_movie_root_with_reason(found, mix)
                movie_folder = os.path.join(root_dir, nice)
                os.makedirs(movie_folder, exist_ok=True)
                out_path = ensure_unique(os.path.join(movie_folder, f"{nice}{ext}"))
                decision.update(
                    {
                        "kind": "movie",
                        "tmdb_used": True,
                        "confidence": conf,
                        "reason": reason,
                        "movie_name": nice,
                        "movie_year": (found.get("year") if found else "") or "",
                        "movie_id": (found.get("movie_id") if found else None),
                        "ai_used": bool(ai_meta) or bool(found.get("_ai_query_used")),
                        "ai_confidence": float((ai_meta or {}).get("confidence") or 0.0),
                    }
                )
            elif not strict_tmdb_match:
                display_name = nice
                root_dir, reason, conf = _classify_movie_root_with_ai_hint(None, mix, ai_meta)
                movie_folder = os.path.join(root_dir, nice)
                os.makedirs(movie_folder, exist_ok=True)
                out_path = ensure_unique(os.path.join(movie_folder, f"{nice}{ext}"))
                decision.update(
                    {
                        "kind": "movie",
                        "tmdb_used": False,
                        "confidence": conf,
                        "reason": f"{reason}_no_tmdb",
                        "movie_name": nice,
                        "movie_year": "",
                        "movie_id": None,
                        "ai_used": bool(ai_meta),
                        "ai_confidence": float((ai_meta or {}).get("confidence") or 0.0),
                    }
                )

    try:
        if dedupe_by_size and os.path.exists(src_path):
            try:
                src_size = int(os.path.getsize(src_path))
            except Exception:
                src_size = 0
            if src_size > 0:
                target_folder = os.path.dirname(out_path)
                if os.path.isdir(target_folder):
                    for n in os.listdir(target_folder):
                        cand = os.path.join(target_folder, n)
                        if not os.path.isfile(cand):
                            continue
                        try:
                            if int(os.path.getsize(cand)) == src_size:
                                decision["reason"] = str(decision.get("reason") or "") + "|dedupe_size_match"
                                decision["dest_path"] = cand
                                decision["status"] = "duplicate"
                                _append_decision_log(decision)
                                return {
                                    "display_name": display_name,
                                    "reason": decision.get("reason"),
                                    "confidence": decision.get("confidence"),
                                    "kind": decision.get("kind"),
                                    "dest_path": cand,
                                    "tmdb_used": decision.get("tmdb_used"),
                                    "ai_used": decision.get("ai_used"),
                                    "ai_confidence": decision.get("ai_confidence"),
                                }
                        except Exception:
                            pass

        if dry_run:
            decision["status"] = "dry_run"
            decision["dest_path"] = out_path
            _append_decision_log(decision)
            _append_move_action({"action": "dry_run", "src": src_path, "dst": out_path, "status": "ok"})
            return {
                "display_name": display_name,
                "reason": str(decision.get("reason") or "") + "|dry_run",
                "confidence": decision.get("confidence"),
                "kind": decision.get("kind"),
                "dest_path": out_path,
                "tmdb_used": decision.get("tmdb_used"),
                "ai_used": decision.get("ai_used"),
                "ai_confidence": decision.get("ai_confidence"),
            }

        moved = False
        for _ in range(3):
            try:
                shutil.move(src_path, out_path)
                moved = True
                break
            except PermissionError:
                time.sleep(0.5)

        if not moved:
            raise PermissionError("File locked during move")

        log_print(f"Plex ✅ {out_path}")
        decision["dest_path"] = out_path
        decision["status"] = "ok"
        _append_decision_log(decision)
        _append_move_action({"action": "move", "src": src_path, "dst": out_path, "status": "ok"})
        return {
            "display_name": display_name,
            "reason": decision.get("reason"),
            "confidence": decision.get("confidence"),
            "kind": decision.get("kind"),
            "dest_path": out_path,
            "tmdb_used": decision.get("tmdb_used"),
            "ai_used": decision.get("ai_used"),
            "ai_confidence": decision.get("ai_confidence"),
        }
    except Exception as e:
        log_print(f"MOVE ERROR: {e}")
        log_print(f"SRC: {src_path}")
        log_print(f"DST: {out_path}")
        decision["dest_path"] = out_path
        decision["status"] = "error"
        decision["error"] = str(e)
        _append_decision_log(decision)
        return None


def auth_loop(client_id: int):
    """
    Minimal authorization state machine.
    """
    log_print("TDLib: waiting for authorization...")

    while True:
        upd = td_receive(2.0)
        if not upd:
            # force TDLib to emit current auth state (some builds need polling)
            td_send(client_id, {"@type": "getAuthorizationState"})
            continue

        t = upd.get("@type")
        if t != "updateAuthorizationState":
            continue

        state = (upd.get("authorization_state") or {}).get("@type")
        # print("AUTH STATE:", state)

        if state == "authorizationStateWaitTdlibParameters":
            set_tdlib_parameters(client_id)
            td_send(client_id, {"@type": "checkDatabaseEncryptionKey", "encryption_key": ""})

        elif state == "authorizationStateWaitPhoneNumber":
            phone = input("Enter phone number (e.g. +972...): ").strip()
            td_send(client_id, {"@type": "setAuthenticationPhoneNumber", "phone_number": phone})

        elif state == "authorizationStateWaitCode":
            code = input("Enter login code from Telegram: ").strip()
            td_send(client_id, {"@type": "checkAuthenticationCode", "code": code})

        elif state == "authorizationStateWaitPassword":
            pwd = input("Enter 2FA password: ").strip()
            td_send(client_id, {"@type": "checkAuthenticationPassword", "password": pwd})

        elif state == "authorizationStateReady":
            log_print("TDLib: authorized ✅")
            return

        elif state == "authorizationStateClosed":
            raise RuntimeError("TDLib authorization closed")


def get_chat_id_by_title(client_id: int, title: str) -> Optional[int]:
    """
    Pulls chat list and tries to find a chat by exact title.
    """
    td_send(client_id, {"@type": "getChats", "chat_list": {"@type": "chatListMain"}, "limit": 200})
    deadline = time.time() + 10

    chat_ids = []
    while time.time() < deadline:
        r = td_receive(1.0)
        if not r:
            continue
        if r.get("@type") == "chats":
            chat_ids = r.get("chat_ids") or []
            break

    for cid in chat_ids:
        td_send(client_id, {"@type": "getChat", "chat_id": cid})
        deadline2 = time.time() + 2
        while time.time() < deadline2:
            r2 = td_receive(0.5)
            if not r2:
                continue
            if r2.get("@type") == "chat" and r2.get("id") == cid:
                if (r2.get("title") or "") == title:
                    return cid
                break
    return None


def start_listening_chat(client_id: int, chat_id: int):
    # Ensure we actually receive updates
    td_send(client_id, {"@type": "setOption", "name": "online", "value": {"@type": "optionValueBoolean", "value": True}})
    td_send(client_id, {"@type": "openChat", "chat_id": chat_id})


def request_download(client_id: int, file_id: int, priority: int = 32):
    td_send(client_id, {
        "@type": "downloadFile",
        "file_id": file_id,
        "priority": priority,
        "offset": 0,
        "limit": 0,
        "synchronous": False
    })


def fmt_size_mb(mb: float) -> str:
    if mb >= 1024:
        return f"{mb/1024:.2f} GB"
    return f"{mb:.0f} MB"


def render_bar(pct: float, width: int = 28) -> str:
    pct = max(0.0, min(100.0, pct))
    fill = int(round((pct / 100.0) * width))
    return "█" * fill + "░" * (width - fill)


STATUS_WIDTH = 170


# Optional UI sinks (for GUI usage). If not set, prints to console as before.
_LOG_SINK = None
_STATUS_SINK = None


def set_ui_sinks(log_sink=None, status_sink=None):
    global _LOG_SINK, _STATUS_SINK
    _LOG_SINK = log_sink
    _STATUS_SINK = status_sink


def status_print(line: str):
    if _STATUS_SINK:
        try:
            _STATUS_SINK(line)
            return
        except Exception:
            pass
    print("\r" + line.ljust(STATUS_WIDTH), end="", flush=True)


def log_print(msg: str):
    if _LOG_SINK:
        try:
            _LOG_SINK(msg)
            return
        except Exception:
            pass
    print("\r" + (" " * STATUS_WIDTH) + "\r", end="", flush=True)
    print(msg)


def main():
    ensure_dirs()
    client_id = tdjson.td_create_client_id()

    # reduce TDLib native logs
    td_execute({"@type": "setLogVerbosityLevel", "new_verbosity_level": 1})

    # kickstart auth updates
    td_send(client_id, {"@type": "getAuthorizationState"})

    auth_loop(client_id)

    chat_id = get_chat_id_by_title(client_id, GROUP_TITLE)
    if not chat_id:
        raise RuntimeError(f'Chat "{GROUP_TITLE}" not found')

    log_print(f"Listening to: {GROUP_TITLE} (chat_id={chat_id})")
    start_listening_chat(client_id, chat_id)

    # Map: file_id -> {name, caption}
    active = {}
    download_queue = []   # queue of file_ids

    # Concurrency modes:
    # - single (default): maximize speed for a single file
    # - throughput: maximize total throughput
    speed_mode = (os.environ.get("SABONIPLEX_SPEED_MODE", "single") or "single").strip().lower()
    default_concurrency = 1 if speed_mode == "single" else 10
    try:
        v = int(os.environ.get("SABONIPLEX_MAX_CONCURRENT_DOWNLOADS", str(default_concurrency)))
    except Exception:
        v = default_concurrency
    max_concurrent_downloads = max(1, min(12, v))

    downloading = set()  # file_ids started and not yet completed
    primary_file_id = None  # for console status line

    def active_download_count() -> int:
        return len(downloading)

    def choose_primary():
        nonlocal primary_file_id
        if primary_file_id in downloading:
            return
        if downloading:
            primary_file_id = next(iter(downloading))
        else:
            primary_file_id = None

    def maybe_start_more():
        nonlocal primary_file_id
        while active_download_count() < max_concurrent_downloads and download_queue:
            fid2 = download_queue.pop(0)
            if fid2 not in active:
                continue
            downloading.add(fid2)
            if primary_file_id is None:
                primary_file_id = fid2
            request_download(client_id, fid2, priority=32)

    last_print = {}
    last_bytes = {}
    last_time = {}
    speed_ema = {}

    while True:
        u = td_receive(1.0)
        if not u:
            continue

        ut = u.get("@type")

        # 1) New messages
        if ut == "updateNewMessage":
            msg = (u.get("message") or {})
            if msg.get("chat_id") != chat_id:
                continue

            content = msg.get("content") or {}
            ctype = content.get("@type")

            # Document / Video
            file_obj = None
            suggested = None
            caption_text = ""

            cap = content.get("caption") or {}
            if isinstance(cap, dict):
                caption_text = str(cap.get("text") or "")

            if ctype == "messageDocument":
                doc = content.get("document") or {}
                file_obj = doc.get("document")
                suggested = (doc.get("file_name") or "video")
            elif ctype == "messageVideo":
                vid = content.get("video") or {}
                file_obj = vid.get("video")
                suggested = (vid.get("file_name") or "video")

            if not file_obj:
                continue

            file_id = file_obj.get("id")
            if not file_id:
                continue

            log_print(f"New media: {suggested} -> downloading...")
            active[file_id] = {"name": suggested, "caption": caption_text}
            if active_download_count() < max_concurrent_downloads:
                downloading.add(file_id)
                if primary_file_id is None:
                    primary_file_id = file_id
                request_download(client_id, file_id, priority=32)
                log_print(
                    f"Started: {suggested} (active={active_download_count()}/{max_concurrent_downloads}, queue={len(download_queue)})"
                )
            else:
                download_queue.append(file_id)
                log_print(
                    f"Queued: {suggested} (active={active_download_count()}/{max_concurrent_downloads}, queue={len(download_queue)})"
                )

        # 2) File progress/completion
        elif ut == "updateFile":
            f = u.get("file") or {}
            file_id = f.get("id")
            if not file_id or file_id not in active:
                continue

            # If TDLib starts downloading a queued file due to internal priorities, ensure we track it.
            local = f.get("local") or {}
            if bool(local.get("is_downloading_active")) and file_id not in downloading:
                downloading.add(file_id)
                choose_primary()


            total_size = int(f.get("size") or 0)
            downloaded_size = int(local.get("downloaded_size") or 0)

            now = time.time()
            prev_t = last_time.get(file_id, now)
            prev_b = last_bytes.get(file_id, downloaded_size)

            dt = max(1e-6, now - prev_t)
            db = max(0, downloaded_size - prev_b)
            speed_raw = db / dt
            prev_speed = speed_ema.get(file_id, speed_raw)
            speed = 0.85 * prev_speed + 0.15 * speed_raw
            speed_ema[file_id] = speed

            # print at most twice per second (single status line -> primary only)
            if file_id == primary_file_id and now - last_print.get(file_id, 0) >= 0.5 and total_size > 0:
                pct = (downloaded_size / total_size) * 100.0
                try:
                    filename = (active.get(file_id) or {}).get("name") or "file"
                except Exception:
                    filename = "file"
                done_mb = downloaded_size / 1024 / 1024
                total_mb = total_size / 1024 / 1024
                left_mb = max(0.0, total_mb - done_mb)
                speed_mb = speed / 1024 / 1024

                eta_sec = int((max(0, total_size - downloaded_size) / speed)) if speed > 0 else -1
                eta_txt = f"{eta_sec//60:02d}:{eta_sec%60:02d}" if eta_sec >= 0 else "--:--"

                bar = render_bar(pct, 28)
                done_txt = fmt_size_mb(done_mb)
                total_txt = fmt_size_mb(total_mb)
                left_txt = fmt_size_mb(left_mb)

                q_len = 0
                try:
                    q_len = len(download_queue)  # if queue exists
                except:
                    q_len = 0

                progress_line = (
                    f"[{bar}] {pct:6.1f}% | "
                    f"{done_txt}/{total_txt} | "
                    f"left {left_txt} | "
                    f"{speed_mb:6.2f} MB/s | "
                    f"ETA {eta_txt}"
                )

                if q_len:
                    progress_line += f" | Q:{q_len}"

                if max_concurrent_downloads:
                    progress_line += f" | A:{active_download_count()}/{max_concurrent_downloads}"

                # overwrite same console line (padding clears previous longer line)
                status_print(progress_line)
                last_print[file_id] = now

            last_time[file_id] = now
            last_bytes[file_id] = downloaded_size
            if local.get("is_downloading_completed"):
                status_print("")  # clear the status line once
                src = local.get("path")
                meta = active.pop(file_id) or {}
                name = meta.get("name")
                caption = meta.get("caption")

                downloading.discard(file_id)
                if primary_file_id == file_id:
                    primary_file_id = None
                choose_primary()

                if src and os.path.exists(src):
                    plex_move(src, name, caption)
                else:
                    log_print("Download completed but file path missing")

                # start more queued downloads if slots available
                maybe_start_more()

        # (אופציונלי) שקט לוג
        # else:
        #     pass

        # Opportunistic: if we have room and queue exists, fill it.
        if download_queue and active_download_count() < max_concurrent_downloads:
            maybe_start_more()


if __name__ == "__main__":
    main()
