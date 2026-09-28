import unittest
from datetime import datetime, timedelta
from time import struct_time
from unittest.mock import Mock, patch

from feedparser import FeedParserDict

from main import deduplicate_items, get_posts_from_feeds


class DeduplicateItemsTest(unittest.TestCase):
    def test_removes_duplicate_urls(self):
        items = [
            {"title": "First", "url": "https://example.com/story", "summary": "One"},
            {"title": "Second", "url": "https://example.com/story", "summary": "Two"},
        ]

        self.assertEqual(deduplicate_items(items), [items[0]])

    def test_compares_urls_as_exact_strings(self):
        items = [
            {"title": "First", "url": "https://example.com/story/", "summary": "One"},
            {"title": "Second", "url": "https://example.com/story", "summary": "Two"},
        ]

        self.assertEqual(deduplicate_items(items), items)

    def test_keeps_identical_summaries_with_distinct_urls(self):
        items = [
            {"title": "First", "url": "https://example.com/1", "summary": "Same  description"},
            {"title": "Second", "url": "https://example.com/2", "summary": "Same  description"},
        ]

        self.assertEqual(deduplicate_items(items), items)

    def test_empty_summaries_do_not_make_distinct_items_duplicates(self):
        items = [
            {"title": "First", "url": "https://example.com/1", "summary": ""},
            {"title": "Second", "url": "https://example.com/2", "summary": ""},
        ]

        self.assertEqual(deduplicate_items(items), items)


class FeedDateFilteringTest(unittest.TestCase):
    @staticmethod
    def entry(title, url, **dates):
        return FeedParserDict({
            "title": title,
            "link": url,
            "summary": "",
            **dates,
        })

    @staticmethod
    def response(content=b"feed", ok=True, status_code=200):
        return Mock(content=content, ok=ok, status_code=status_code)

    def test_filters_atom_entries_using_updated_date(self):
        current = datetime(2026, 8, 27, 12)
        recent = struct_time((2026, 8, 27, 6, 0, 0, 3, 239, -1))
        old = struct_time((2026, 8, 25, 6, 0, 0, 1, 237, -1))
        entries = [
            self.entry("Recent", "https://example.com/recent",
                       updated_parsed=recent),
            self.entry("Old", "https://example.com/old",
                       updated_parsed=old),
        ]

        feed = FeedParserDict(entries=entries, bozo=False)
        with patch("main.session.get", return_value=self.response()) as get, \
                patch("main.feedparser.parse", return_value=feed) as parse:
            items = get_posts_from_feeds(
                "https://example.com/atom.xml", current, timedelta(days=1))

        self.assertEqual([item["title"] for item in items], ["Recent"])
        get.assert_called_once_with(
            "https://example.com/atom.xml", timeout=20)
        parse.assert_called_once_with(b"feed")

    def test_skips_undated_entries_instead_of_treating_them_as_current(self):
        entry = self.entry("Undated", "https://example.com/undated")

        feed = FeedParserDict(entries=[entry], bozo=False)
        with patch("main.session.get", return_value=self.response()), \
                patch("main.feedparser.parse", return_value=feed):
            items = get_posts_from_feeds(
                "https://example.com/atom.xml",
                datetime(2026, 8, 27), timedelta(days=1))

        self.assertEqual(items, [])

    def test_returns_no_items_when_feed_request_is_rejected(self):
        with patch("main.session.get",
                   return_value=self.response(ok=False, status_code=403)), \
                patch("main.feedparser.parse") as parse:
            items = get_posts_from_feeds(
                "https://example.com/atom.xml",
                datetime(2026, 8, 27), timedelta(days=1))

        self.assertEqual(items, [])
        parse.assert_not_called()

    def test_returns_no_items_when_feed_request_fails(self):
        with patch("main.session.get", side_effect=RuntimeError("network")):
            items = get_posts_from_feeds(
                "https://example.com/atom.xml",
                datetime(2026, 8, 27), timedelta(days=1))

        self.assertEqual(items, [])

if __name__ == "__main__":
    unittest.main()
