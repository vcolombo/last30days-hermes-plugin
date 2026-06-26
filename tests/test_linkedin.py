import unittest
from unittest import mock

from lib.linkedin import (
    _extract_posts,
    _int_field,
    _parse_date,
    parse_linkedin_response,
    search_linkedin,
)


class TestParseLinkedinResponse(unittest.TestCase):
    def _make_post(self, **overrides):
        base = {
            "id": "urn:li:activity:123",
            "text": "Excited to share our latest product update.",
            "url": "https://www.linkedin.com/posts/example_123",
            "author": "Jane Doe",
            "date": "2026-06-01",
            "likes": 42,
            "comments": 5,
            "reposts": 2,
        }
        base.update(overrides)
        return base

    def test_basic_post_parses_all_fields(self):
        items = parse_linkedin_response({"posts": [self._make_post()]})
        self.assertEqual(1, len(items))
        item = items[0]
        self.assertEqual("urn:li:activity:123", item["id"])
        self.assertEqual("Excited to share our latest product update.", item["text"])
        self.assertEqual("https://www.linkedin.com/posts/example_123", item["url"])
        self.assertEqual("Jane Doe", item["author"])
        self.assertEqual("2026-06-01", item["date"])
        self.assertEqual(42, item["engagement"]["likes"])
        self.assertEqual(5, item["engagement"]["comments"])
        self.assertEqual(2, item["engagement"]["reposts"])

    def test_empty_posts_returns_empty_list(self):
        self.assertEqual([], parse_linkedin_response({"posts": []}))
        self.assertEqual([], parse_linkedin_response({}))

    def test_post_without_text_is_skipped(self):
        raw = self._make_post()
        del raw["text"]
        items = parse_linkedin_response({"posts": [raw]})
        self.assertEqual([], items)

    def test_non_dict_post_is_skipped(self):
        items = parse_linkedin_response({"posts": ["not-a-dict", None]})
        self.assertEqual([], items)

    def test_author_as_dict(self):
        raw = self._make_post(author={"name": "Jane Doe", "full_name": "Jane M Doe"})
        items = parse_linkedin_response({"posts": [raw]})
        self.assertEqual("Jane Doe", items[0]["author"])

    def test_author_dict_falls_back_to_full_name(self):
        raw = self._make_post(author={"full_name": "Jane M Doe"})
        items = parse_linkedin_response({"posts": [raw]})
        self.assertEqual("Jane M Doe", items[0]["author"])

    def test_author_missing_defaults_to_empty_string(self):
        raw = self._make_post()
        del raw["author"]
        items = parse_linkedin_response({"posts": [raw]})
        self.assertEqual("", items[0]["author"])

    def test_id_falls_back_to_generated_index(self):
        raw = self._make_post()
        del raw["id"]
        items = parse_linkedin_response({"posts": [raw]})
        self.assertEqual("LI1", items[0]["id"])

    def test_alternate_field_names(self):
        raw = {
            "postId": "p1",
            "content": "alternate field names",
            "postUrl": "https://example.com/p1",
            "authorName": "Alt Author",
            "postedAt": "2026-05-15T10:00:00Z",
            "likesCount": 10,
            "commentsCount": 3,
            "shares": 1,
        }
        items = parse_linkedin_response({"posts": [raw]})
        item = items[0]
        self.assertEqual("p1", item["id"])
        self.assertEqual("alternate field names", item["text"])
        self.assertEqual("https://example.com/p1", item["url"])
        self.assertEqual("Alt Author", item["author"])
        self.assertEqual("2026-05-15", item["date"])
        self.assertEqual(10, item["engagement"]["likes"])
        self.assertEqual(3, item["engagement"]["comments"])
        self.assertEqual(1, item["engagement"]["reposts"])


class TestExtractPosts(unittest.TestCase):
    def test_extracts_from_posts_key(self):
        self.assertEqual([{"a": 1}], _extract_posts({"posts": [{"a": 1}]}))

    def test_extracts_from_items_key(self):
        self.assertEqual([{"a": 1}], _extract_posts({"items": [{"a": 1}]}))

    def test_extracts_from_data_key(self):
        self.assertEqual([{"a": 1}], _extract_posts({"data": [{"a": 1}]}))

    def test_extracts_from_results_key(self):
        self.assertEqual([{"a": 1}], _extract_posts({"results": [{"a": 1}]}))

    def test_non_dict_response_returns_empty(self):
        self.assertEqual([], _extract_posts(["not", "a", "dict"]))
        self.assertEqual([], _extract_posts(None))

    def test_no_matching_key_returns_empty(self):
        self.assertEqual([], _extract_posts({"unexpected": [1, 2, 3]}))


class TestParseDate(unittest.TestCase):
    def test_extracts_date_from_iso_string(self):
        self.assertEqual("2026-06-01", _parse_date("2026-06-01T12:30:00Z"))

    def test_plain_date_string(self):
        self.assertEqual("2026-06-01", _parse_date("2026-06-01"))

    def test_none_returns_none(self):
        self.assertIsNone(_parse_date(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_date(""))

    def test_no_date_pattern_returns_none(self):
        self.assertIsNone(_parse_date("not a date"))


class TestIntField(unittest.TestCase):
    def test_returns_first_present_key(self):
        self.assertEqual(5, _int_field({"likes": 5, "likesCount": 10}, "likes", "likesCount"))

    def test_falls_back_to_second_key(self):
        self.assertEqual(10, _int_field({"likesCount": 10}, "likes", "likesCount"))

    def test_missing_all_keys_returns_zero(self):
        self.assertEqual(0, _int_field({}, "likes", "likesCount"))

    def test_non_numeric_value_returns_zero(self):
        self.assertEqual(0, _int_field({"likes": "not-a-number"}, "likes"))

    def test_string_numeric_value_is_coerced(self):
        self.assertEqual(7, _int_field({"likes": "7"}, "likes"))


class TestSearchLinkedin(unittest.TestCase):
    def test_no_token_returns_empty_without_network_call(self):
        with mock.patch("lib.linkedin.http.request") as mock_request:
            result = search_linkedin("AI agents", "2026-05-27", "2026-06-26", token="")
            self.assertEqual({"posts": []}, result)
            mock_request.assert_not_called()

    def test_successful_search_returns_capped_posts(self):
        raw_posts = [{"text": f"post {i}"} for i in range(20)]
        with mock.patch(
            "lib.linkedin.http.request", return_value={"posts": raw_posts}
        ):
            result = search_linkedin(
                "AI agents", "2026-05-27", "2026-06-26", depth="quick", token="fake-token"
            )
            # "quick" depth caps at 10 results per DEPTH_CONFIG
            self.assertEqual(10, len(result["posts"]))

    def test_http_error_returns_empty_with_error_message(self):
        from lib import http as http_module

        with mock.patch(
            "lib.linkedin.http.request",
            side_effect=http_module.HTTPError("rate limited", status_code=429),
        ):
            result = search_linkedin(
                "AI agents", "2026-05-27", "2026-06-26", token="fake-token"
            )
            self.assertEqual([], result["posts"])
            self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
