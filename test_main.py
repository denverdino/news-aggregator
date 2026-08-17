import unittest

from main import deduplicate_items


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


if __name__ == "__main__":
    unittest.main()
