import unittest
from datetime import datetime, timedelta
from time import struct_time
from unittest.mock import patch

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

        with patch("main.feedparser.parse",
                   return_value=type("Feed", (), {"entries": entries})()):
            items = get_posts_from_feeds(
                "https://example.com/atom.xml", current, timedelta(days=1))

        self.assertEqual([item["title"] for item in items], ["Recent"])

    def test_skips_undated_entries_instead_of_treating_them_as_current(self):
        entry = self.entry("Undated", "https://example.com/undated")

        with patch("main.feedparser.parse",
                   return_value=type("Feed", (), {"entries": [entry]})()):
            items = get_posts_from_feeds(
                "https://example.com/atom.xml",
                datetime(2026, 8, 27), timedelta(days=1))

        self.assertEqual(items, [])

if __name__ == "__main__":
    unittest.main()
