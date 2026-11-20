import unittest
from datetime import datetime, timedelta, timezone
from time import struct_time
from unittest.mock import Mock, patch

from feedparser import FeedParserDict
from prawcore.exceptions import Forbidden, RequestException, ResponseException

import main as aggregator
from main import deduplicate_items, fetch_posts_from_reddit, get_posts_from_feeds


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


class RedditFetchingTest(unittest.TestCase):
    @staticmethod
    def submission(**overrides):
        return Mock(**{
            "created_utc": datetime.now(timezone.utc).timestamp(),
            "is_self": False,
            "spoiler": False,
            "over_18": False,
            "url": "https://example.com/story",
            "title": "Recent story",
            **overrides,
        })

    @staticmethod
    def listing(posts=(), error=None):
        # PRAW performs HTTP requests lazily, including on later pages.
        yield from posts
        if error is not None:
            raise error

    def test_forbidden_during_iteration_does_not_abort_collection(self):
        reddit = Mock()
        reddit.subreddit.return_value.new.return_value = self.listing(
            error=Forbidden(Mock(status_code=403)))

        with self.assertLogs(level="WARNING") as logs:
            items = fetch_posts_from_reddit(reddit, ["Kubernetes"])

        self.assertEqual(items, [])
        self.assertIn("Kubernetes", " ".join(logs.output))
        self.assertIn("403", " ".join(logs.output))

    def test_failed_subreddit_does_not_discard_other_subreddits(self):
        reddit = Mock()
        blocked, healthy = Mock(), Mock()
        blocked.new.return_value = self.listing(
            error=Forbidden(Mock(status_code=403)))
        healthy.new.return_value = self.listing([self.submission()])
        reddit.subreddit.side_effect = {"blocked": blocked, "healthy": healthy}.__getitem__

        with self.assertLogs(level="WARNING"):
            items = fetch_posts_from_reddit(reddit, ["blocked", "healthy"])

        self.assertEqual(items, [{"url": "https://example.com/story", "title": "Recent story"}])
        healthy.new.assert_called_once_with(limit=200)

    def test_keeps_collected_posts_when_a_later_page_fails(self):
        for error in (Forbidden(Mock(status_code=403)),
                      ResponseException(Mock(status_code=503)),
                      RequestException(OSError("network"), (), {})):
            with self.subTest(error=type(error).__name__):
                reddit = Mock()
                reddit.subreddit.return_value.new.return_value = self.listing(
                    [self.submission()], error)

                with self.assertLogs(level="WARNING"):
                    items = fetch_posts_from_reddit(reddit, ["Kubernetes"])

                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["title"], "Recent story")

    def test_preserves_post_filters(self):
        reddit = Mock()
        reddit.subreddit.return_value.new.return_value = self.listing([
            self.submission(),
            self.submission(created_utc=(datetime.now(timezone.utc) - timedelta(days=2)).timestamp()),
            self.submission(is_self=True),
            self.submission(spoiler=True),
            self.submission(over_18=True),
            self.submission(url="https://example.com/image.JPG"),
            self.submission(url="/relative"),
        ])

        self.assertEqual(fetch_posts_from_reddit(reddit, ["Kubernetes"]), [
            {"url": "https://example.com/story", "title": "Recent story"},
        ])

    def test_empty_subreddit_list_does_not_make_requests(self):
        reddit = Mock()
        self.assertEqual(fetch_posts_from_reddit(reddit, []), [])
        reddit.subreddit.assert_not_called()

    def test_unexpected_programming_errors_are_not_suppressed(self):
        reddit = Mock()
        reddit.subreddit.return_value.new.return_value = self.listing(
            error=TypeError("unexpected bug"))
        with self.assertRaises(TypeError):
            fetch_posts_from_reddit(reddit, ["Kubernetes"])

    def test_main_continues_to_feeds_and_email_after_reddit_forbidden(self):
        reddit = Mock()
        reddit.subreddit.return_value.new.return_value = self.listing(
            error=Forbidden(Mock(status_code=403)))
        config = {
            "hackernews": ["Kubernetes"],
            "reddit": ["Kubernetes"],
            "feeds": [{"url": "https://example.com/feed.xml"}],
            "emails": ["test@example.com"],
            "subject": "Test digest",
        }
        hn_item = {"title": "HN story", "url": "https://example.com/hn"}
        feed_item = {"title": "RSS story", "url": "https://example.com/rss", "summary": "RSS summary"}
        with patch("sys.argv", ["main.py"]), \
                patch("main.os.path.exists", return_value=True), \
                patch("main.parse_yaml_config", return_value=config), \
                patch("main.fetch_stories_with_keywords", return_value=[hn_item]), \
                patch("main.generate_summary_with_cache", return_value="HN summary"), \
                patch("main.time.sleep"), \
                patch("main.initialize_reddit", return_value=reddit), \
                patch("main.get_posts_from_feeds", return_value=[feed_item]) as feeds, \
                patch("main.send_html_email") as send, \
                patch("builtins.print"), \
                self.assertLogs(level="WARNING"):
            aggregator.main()

        feeds.assert_called_once()
        send.assert_called_once()
        subject, body, recipients = send.call_args.args
        self.assertEqual(subject, "Test digest")
        self.assertEqual(recipients, ["test@example.com"])
        self.assertIn("HN story", body)
        self.assertIn("RSS story", body)


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
