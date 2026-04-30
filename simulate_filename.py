import argparse
import json
import os
from typing import Any, Dict, Optional

from tdlib_saboniplex_maxspeed import (
    MOVIES_DIR,
    TV_DIR,
    VIDEO_EXTS,
    cleanup_query,
    extract_year,
    parse_tv,
    pick_movie_root,
    season_folder_name,
    strip_known_ext,
    tmdb_find_movie,
    tmdb_find_tv,
    tv_filename,
    windows_safe_filename,
)


def _guess_ext(original_name: str, src_path: Optional[str] = None) -> str:
    _, ext_src = os.path.splitext(src_path or "")
    _, ext_base = os.path.splitext(original_name or "")
    ext = (ext_src or ext_base or ".mp4").lower()
    if ext not in VIDEO_EXTS:
        return ".mp4"
    return ext


def simulate(original_name: str, caption_text: str = "", src_path: Optional[str] = None, *, use_tmdb: bool = True) -> Dict[str, Any]:
    base = os.path.basename(original_name or "") or (original_name or "")
    ext = _guess_ext(base, src_path)

    raw_text = strip_known_ext(base)
    caption_raw = strip_known_ext((caption_text or "").strip())

    tv_info = None
    tv_query_text = raw_text

    if caption_raw:
        tv_info = parse_tv(caption_raw)
        if tv_info:
            tv_query_text = caption_raw

    if not tv_info:
        tv_info = parse_tv(raw_text)
        tv_query_text = raw_text

    cleaned_raw = cleanup_query(raw_text)

    if tv_info:
        show_guess, season, episode = tv_info
        show = show_guess
        if use_tmdb:
            show = tmdb_find_tv(show_guess) or tmdb_find_tv(tv_query_text) or show_guess
        show = windows_safe_filename(show or show_guess, 80)
        dest_dir = os.path.join(TV_DIR, show, season_folder_name(season))
        final_name = windows_safe_filename(tv_filename(show, season, episode, ext), 140)
        display_name = strip_known_ext(final_name)

        return {
            "kind": "tv",
            "input": {"original_name": original_name, "caption": caption_text or "", "src_path": src_path},
            "parsed": {"show_guess": show_guess, "season": season, "episode": episode},
            "cleaned": {"raw_text": raw_text, "cleaned_raw_text": cleaned_raw},
            "plan": {"dest_dir": dest_dir, "final_name": final_name, "out_path": os.path.join(dest_dir, final_name)},
            "display_name": display_name,
            "tmdb": {"used": bool(use_tmdb), "resolved_show": show if use_tmdb else None},
        }

    found = tmdb_find_movie(cleaned_raw or raw_text) if use_tmdb else None
    if found:
        en_title = windows_safe_filename(found.get("en_title") or "", 110)
        yy = (found.get("year") or "").strip()
        nice = f"{en_title} ({yy})" if (en_title and yy) else (en_title or windows_safe_filename(cleaned_raw or raw_text, 120))
        root_dir = pick_movie_root(found, raw_text)
    else:
        nice = windows_safe_filename(cleaned_raw or raw_text, 120)
        root_dir = MOVIES_DIR

    movie_folder = os.path.join(root_dir, nice)
    final_name = f"{nice}{ext}"

    return {
        "kind": "movie",
        "input": {"original_name": original_name, "caption": caption_text or "", "src_path": src_path},
        "cleaned": {
            "raw_text": raw_text,
            "cleaned_raw_text": cleaned_raw,
            "year": extract_year(cleaned_raw),
        },
        "tmdb": {"used": bool(use_tmdb), "found": found},
        "plan": {"dest_dir": movie_folder, "final_name": final_name, "out_path": os.path.join(movie_folder, final_name)},
        "display_name": nice,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Dry-run: simulate how SaboniPlex would name/route a media file.")
    ap.add_argument("name", help="Original filename (or full path)")
    ap.add_argument("--caption", default="", help="Optional Telegram caption")
    ap.add_argument("--src-path", default=None, help="Optional real downloaded path (used to infer extension)")
    ap.add_argument("--no-tmdb", action="store_true", help="Do not call TMDB; use filename only")
    ap.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = ap.parse_args()

    payload = simulate(args.name, caption_text=args.caption, src_path=args.src_path, use_tmdb=not args.no_tmdb)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
