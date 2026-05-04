import unittest
from unittest.mock import patch

from tdlib_saboniplex_maxspeed import (
    _ai_extract_json_text,
    _ai_resolve_media_metadata,
    _candidate_queries,
    cleanup_query,
    parse_tv,
    tmdb_find_movie_smart,
)


class TestFilenameParsing(unittest.TestCase):
    def test_parse_tv_standard(self):
        r = parse_tv("Some.Show.S02E05.1080p")
        self.assertIsNotNone(r)
        show, season, episode = r
        self.assertEqual(season, 2)
        self.assertEqual(episode, 5)
        self.assertTrue("Some" in show)

    def test_parse_tv_x_format(self):
        r = parse_tv("Another Show 3x11 WEB-DL")
        self.assertIsNotNone(r)
        _, season, episode = r
        self.assertEqual(season, 3)
        self.assertEqual(episode, 11)

    def test_cleanup_removes_quality_tags(self):
        s = cleanup_query("Movie.Name.2024.1080p.WEB-DL.x265")
        self.assertIn("2024", s)
        self.assertNotIn("1080p", s.lower())
        self.assertNotIn("web", s.lower())
        self.assertNotIn("x265", s.lower())

    def test_cleanup_removes_release_noise(self):
        s = cleanup_query("Project Hail Mary 2026 1080p WEB AC3 H264-DJT")
        self.assertEqual(s, "Project Hail Mary 2026")

        s2 = cleanup_query("Apex.2026.1080p.WEBRip.x264.AAC5.1-[YTS.BZ]")
        self.assertEqual(s2, "Apex 2026")

    def test_candidate_queries_extracts_caption_title_and_year(self):
        caption = "שם: Into the Blue\nשנה: 2005\nאיכות: 1080P"
        queries = _candidate_queries("junk-file-name.mkv", caption)
        self.assertIn("Into the Blue 2005", queries)
        self.assertIn("Into the Blue", queries)

    def test_tmdb_find_movie_smart_prefers_caption_title_match(self):
        found_bad = {"en_title": "Blue", "he_title": "", "original_title": "Blue", "year": "2005"}
        found_good = {"en_title": "Into the Blue", "he_title": "", "original_title": "Into the Blue", "year": "2005"}

        def fake_tmdb_find_movie(query: str):
            if query == "Into the Blue 2005":
                return found_good
            if query == "junk file name":
                return found_bad
            return None

        with patch("tdlib_saboniplex_maxspeed.tmdb_find_movie", side_effect=fake_tmdb_find_movie):
            result = tmdb_find_movie_smart("junk-file-name.mkv", "שם: Into the Blue\nשנה: 2005")

        self.assertEqual(result, found_good)

    def test_ai_extract_json_text_from_wrapped_response(self):
        payload = _ai_extract_json_text("random text {\"kind\":\"movie\",\"confidence\":0.9} trailing")
        self.assertIsNotNone(payload)
        self.assertEqual(payload.get("kind"), "movie")

    def test_ai_resolve_media_metadata_normalizes_output(self):
        fake = {
            "kind": "movie",
            "title_query": "Apex",
            "year": "2026",
            "season": 0,
            "episode": 0,
            "is_kids": False,
            "is_israeli": False,
            "confidence": 0.87,
        }
        with patch("tdlib_saboniplex_maxspeed._ai_completion_json", return_value=fake):
            out = _ai_resolve_media_metadata("Apex.2026.1080p.mkv", "Apex 2026")

        self.assertIsNotNone(out)
        self.assertEqual(out.get("kind"), "movie")
        self.assertEqual(out.get("title_query"), "Apex")
        self.assertEqual(out.get("year"), "2026")
        self.assertGreaterEqual(float(out.get("confidence") or 0.0), 0.8)

    def test_ai_resolve_media_metadata_keeps_series_numbers(self):
        fake = {
            "kind": "series",
            "title_query": "Some Show",
            "year": "",
            "season": 2,
            "episode": 5,
            "is_kids": False,
            "is_israeli": False,
            "confidence": 0.91,
        }
        with patch("tdlib_saboniplex_maxspeed._ai_completion_json", return_value=fake):
            out = _ai_resolve_media_metadata("weird-name.mkv", "Some Show season 2 episode 5")

        self.assertEqual(out.get("kind"), "series")
        self.assertEqual(out.get("season"), 2)
        self.assertEqual(out.get("episode"), 5)


if __name__ == "__main__":
    unittest.main()
