import hashlib
import os
import tempfile
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
        self.assertIn("Reddit app access", " ".join(logs.output))
        self.assertIn("subreddit permissions", " ".join(logs.output))

    def test_non_forbidden_errors_do_not_suggest_permission_changes(self):
        reddit = Mock()
        reddit.subreddit.return_value.new.return_value = self.listing(
            error=ResponseException(Mock(status_code=503)))
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(fetch_posts_from_reddit(reddit, ["Kubernetes"]), [])
        self.assertIn("503", " ".join(logs.output))
        self.assertNotIn("Reddit app access", " ".join(logs.output))

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
        feed_item = {"title": "RSS story", "url": "https://example.com/rss", "summary": "RSS <summary> & details"}
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
        self.assertIn("RSS &lt;summary&gt; &amp; details", body)
        self.assertNotIn("RSS <summary>", body)


class ArticleExtractionTest(unittest.TestCase):
    url = "https://example.com/story"

    def test_http_failures_do_not_retry_with_another_downloader(self):
        for status in (401, 403, 404, 410, 429, 500):
            with self.subTest(status=status), \
                    patch("main.session.get", return_value=Mock(ok=False, status_code=status)) as get, \
                    patch("main.trafilatura.fetch_url") as fallback, \
                    patch("main.trafilatura.extract") as extract, \
                    self.assertLogs(level="WARNING") as logs:
                self.assertEqual(aggregator.extract_content(self.url), "")

                get.assert_called_once_with(self.url, timeout=20)
                fallback.assert_not_called()
                extract.assert_not_called()
                self.assertEqual(len(logs.records), 1)
                self.assertEqual(logs.records[0].levelname, "WARNING")
                self.assertIn(str(status), logs.output[0])

    def test_http_200_block_pages_are_not_extracted(self):
        cases = [
            ({"cf-mitigated": "challenge"}, "<html><body>Challenge</body></html>"),
            ({}, "<html><title>Just a moment...</title><body>Challenge</body></html>"),
            ({}, "<html><title>You&#39;re using a too-old browser</title><body>Blocked</body></html>"),
            ({}, "<html><title>Access Denied</title><body>Blocked</body></html>"),
        ]
        for headers, body in cases:
            with self.subTest(body=body), \
                    patch("main.session.get", return_value=Mock(ok=True, headers=headers, text=body)), \
                    patch("main.trafilatura.extract") as extract, \
                    self.assertLogs(level="WARNING") as logs:
                self.assertEqual(aggregator.extract_content(self.url), "")
                extract.assert_not_called()
                self.assertIn(self.url, logs.output[0])

    def test_normal_article_can_discuss_access_restrictions(self):
        body = "<html><title>Access denied errors explained</title><body>Just a moment... browser filtering</body></html>"
        with patch("main.session.get", return_value=Mock(ok=True, headers={}, text=body)), \
                patch("main.trafilatura.extract", return_value="Article text") as extract:
            self.assertEqual(aggregator.extract_content(self.url), "Article text")
        extract.assert_called_once_with(body)

    def test_empty_response_is_not_sent_to_extractor(self):
        with patch("main.session.get", return_value=Mock(ok=True, headers={}, text="")), \
                patch("main.trafilatura.extract") as extract:
            self.assertEqual(aggregator.extract_content(self.url), "")
        extract.assert_not_called()

    def test_network_failure_is_a_warning_and_does_not_retry(self):
        with patch("main.session.get", side_effect=RuntimeError("network")), \
                patch("main.trafilatura.fetch_url") as fallback, \
                self.assertLogs(level="WARNING") as logs:
            self.assertEqual(aggregator.extract_content(self.url), "")
        fallback.assert_not_called()
        self.assertEqual(logs.records[0].levelname, "WARNING")

    def test_extraction_failure_returns_empty_content(self):
        with patch("main.session.get", return_value=Mock(ok=True, headers={}, text="<p>Short</p>")), \
                patch("main.trafilatura.extract", return_value=None):
            self.assertEqual(aggregator.extract_content(self.url), "")


class SummaryCacheTest(unittest.TestCase):
    url = "https://example.com/story"

    def test_empty_content_is_not_summarized_or_cached(self):
        with tempfile.TemporaryDirectory() as cache, \
                patch("main.extract_content", return_value="") as extract, \
                patch("main.generate_summary") as summarize:
            for _ in range(2):
                self.assertEqual(aggregator.generate_summary_with_cache(self.url, cache), "")
            self.assertEqual(extract.call_count, 2)
            summarize.assert_not_called()
            self.assertFalse(any(files for _, _, files in os.walk(cache)))

    def test_http_403_reaches_cache_without_creating_an_empty_file(self):
        with tempfile.TemporaryDirectory() as cache, \
                patch("main.session.get", return_value=Mock(ok=False, status_code=403)) as get, \
                patch("main.trafilatura.fetch_url") as fallback, \
                patch("main.generate_summary") as summarize, \
                self.assertLogs(level="WARNING") as logs:
            for _ in range(2):
                self.assertEqual(aggregator.generate_summary_with_cache(self.url, cache), "")
            self.assertEqual(get.call_count, 2)
            self.assertFalse(any(files for _, _, files in os.walk(cache)))
            fallback.assert_not_called()
            summarize.assert_not_called()
        self.assertEqual([record.levelname for record in logs.records], ["WARNING", "WARNING"])

    def test_empty_generated_summary_is_not_cached(self):
        with tempfile.TemporaryDirectory() as cache, \
                patch("main.extract_content", return_value="Article text") as extract, \
                patch("main.generate_summary", side_effect=[" \n", "Recovered summary"]):
            self.assertEqual(aggregator.generate_summary_with_cache(self.url, cache), "")
            self.assertFalse(any(files for _, _, files in os.walk(cache)))
            self.assertEqual(aggregator.generate_summary_with_cache(self.url, cache), "Recovered summary")
            self.assertEqual(extract.call_count, 2)

    def test_old_empty_cache_is_retried_and_replaced(self):
        with tempfile.TemporaryDirectory() as cache:
            story_hash = hashlib.md5(self.url.encode()).hexdigest()
            folder = os.path.join(cache, story_hash[:2])
            os.makedirs(folder)
            path = os.path.join(folder, f"{story_hash}_summary.txt")
            with open(path, "w") as file:
                file.write(" \n")
            with patch("main.extract_content", return_value="Recovered article") as extract, \
                    patch("main.generate_summary", return_value="Recovered summary"):
                self.assertEqual(aggregator.generate_summary_with_cache(self.url, cache), "Recovered summary")
            extract.assert_called_once_with(self.url)
            with open(path) as file:
                self.assertEqual(file.read(), "Recovered summary")

    def test_successful_summary_is_reused(self):
        with tempfile.TemporaryDirectory() as cache, \
                patch("main.extract_content", return_value="Article text") as extract, \
                patch("main.generate_summary", return_value="Summary") as summarize:
            self.assertEqual(aggregator.generate_summary_with_cache(self.url, cache), "Summary")
            self.assertEqual(aggregator.generate_summary_with_cache(self.url, cache), "Summary")
            extract.assert_called_once_with(self.url)
            summarize.assert_called_once_with("Article text")


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

    def test_plain_text_summary_bypasses_html_extraction(self):
        summary = "Load GPT-OSS-20B into vLLM on an AKS H100 node with Run:ai Model Streamer and DACS."
        entry = self.entry("AKS", "https://example.com/aks", summary=summary,
                           updated_parsed=struct_time((2026, 8, 27, 6, 0, 0, 3, 239, -1)))
        with patch("main.session.get", return_value=self.response()), \
                patch("main.feedparser.parse", return_value=FeedParserDict(entries=[entry], bozo=False)), \
                patch("main.trafilatura.extract") as extract, \
                patch("main.trafilatura.html2txt") as strip_html:
            items = get_posts_from_feeds(
                "https://example.com/feed.xml", datetime(2026, 8, 27, 12), timedelta(days=1))
        self.assertEqual(items[0]["summary"], summary)
        extract.assert_not_called()
        strip_html.assert_not_called()

    def test_html_summary_is_converted_to_plain_text(self):
        entry = self.entry("HTML", "https://example.com/html",
                           summary="<p>Cloud Run &amp; <strong>Sandbox</strong>.</p>",
                           updated_parsed=struct_time((2026, 8, 27, 6, 0, 0, 3, 239, -1)))
        with patch("main.session.get", return_value=self.response()), \
                patch("main.feedparser.parse", return_value=FeedParserDict(entries=[entry], bozo=False)), \
                patch("main.trafilatura.extract") as extract, \
                patch("logging.Logger.error") as error:
            items = get_posts_from_feeds(
                "https://example.com/feed.xml", datetime(2026, 8, 27, 12), timedelta(days=1))
        error.assert_not_called()
        summary = items[0]["summary"]
        self.assertIn("Cloud Run", summary)
        self.assertIn("&", summary)
        self.assertIn("Sandbox", summary)
        self.assertNotIn("<", summary)
        self.assertNotIn("&amp;", summary)
        extract.assert_not_called()

    def test_plain_text_summary_obeys_character_limit(self):
        entry = self.entry("Long", "https://example.com/long", summary="A" * 100,
                           updated_parsed=struct_time((2026, 8, 27, 6, 0, 0, 3, 239, -1)))
        with patch("main.session.get", return_value=self.response()), \
                patch("main.feedparser.parse", return_value=FeedParserDict(entries=[entry], bozo=False)):
            items = get_posts_from_feeds(
                "https://example.com/feed.xml", datetime(2026, 8, 27, 12), timedelta(days=1),
                max_characters=10)
        self.assertEqual(items[0]["summary"], "A" * 10)

    def test_real_rss_parser_handles_plain_and_html_summaries(self):
        for description, expected in [
            ("Load GPT-OSS-20B with Run:ai Model Streamer.", "Load GPT-OSS-20B with Run:ai Model Streamer."),
            ("<p>Cloud Run &amp; <strong>Sandbox</strong>.</p>", "Cloud Run & Sandbox"),
        ]:
            content = (
                '<rss version="2.0"><channel><title>News</title>'
                '<link>https://example.com</link><description>News feed</description>'
                '<item><title>Story</title><link>https://example.com/story</link>'
                '<pubDate>Thu, 27 Aug 2026 06:00:00 GMT</pubDate>'
                f'<description><![CDATA[{description}]]></description>'
                '</item></channel></rss>'
            ).encode()
            with self.subTest(description=description), \
                    patch("main.session.get", return_value=self.response(content=content)), \
                    patch("logging.Logger.error") as error:
                items = get_posts_from_feeds(
                    "https://example.com/feed.xml", datetime(2026, 8, 27, 12), timedelta(days=1))
            self.assertEqual(len(items), 1)
            self.assertIn(expected, items[0]["summary"])
            self.assertNotIn("<p>", items[0]["summary"])
            error.assert_not_called()

    def test_html_page_is_not_silently_accepted_as_an_empty_feed(self):
        with patch("main.session.get", return_value=self.response()), \
                patch("main.feedparser.parse", return_value=FeedParserDict(entries=[], bozo=False, version="")), \
                self.assertLogs(level="WARNING") as logs:
            items = get_posts_from_feeds(
                "https://example.com/html", datetime(2026, 8, 27), timedelta(days=1))
        self.assertEqual(items, [])
        self.assertIn("not an RSS or Atom feed", logs.output[0])

    def test_valid_empty_feed_does_not_warn(self):
        with patch("main.session.get", return_value=self.response()), \
                patch("main.feedparser.parse", return_value=FeedParserDict(entries=[], bozo=False, version="rss20")), \
                patch("main.logging.warning") as warning:
            items = get_posts_from_feeds(
                "https://example.com/feed.xml", datetime(2026, 8, 27), timedelta(days=1))
        self.assertEqual(items, [])
        warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
