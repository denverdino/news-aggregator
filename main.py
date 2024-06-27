import requests
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import hashlib
from openai import OpenAI
import trafilatura
import html
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging
import praw
import re
import feedparser
import yaml
import argparse


log_level_name = os.getenv('LOG_LEVEL', 'WARNING').upper()
log_level = logging.getLevelName(log_level_name)

# Validate the log_level
if not isinstance(log_level, int):
    raise ValueError(f"Invalid log level: {log_level_name}")

# Configure logging
logging.basicConfig(level=log_level)


def parse_yaml_config(file_path):
    with open(file_path, 'r') as file:
        config = yaml.safe_load(file)

    return config


def generate_summary(text, model="qwen-turbo", temperature=0.7, max_tokens=100):
    """
    Generates a summary for the given text using the specified model.

    Parameters:
    - text (str): The text to summarize.
    - model (str): The model to use for summarization. Default is "gpt-35-turbo".
    - temperature (float): Controls randomness in the output. Lower values mean less random outputs.
    - max_tokens (int): The maximum number of tokens to generate in the output.

    Returns:
    - str: The generated summary.
    """

    if text == "":
        return ""

    # client = AzureOpenAI(
    #     azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    #     api_key=os.getenv("OPENAI_API_KEY"),
    #     api_version="2024-02-01"
    # )

    client = OpenAI(
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",  # 填写DashScope服务endpoint
    )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a helpful assistant for news aggregation."},
            {"role": "user", "content": f"Summarize the following text:\n\n{text}"},
        ],
        max_tokens=max_tokens
    )
    summary = response.choices[0].message.content.strip()
    return summary


def generate_summary_with_cache(url, cache_path, max_characters=3000):
    # Create a hash of the story_id to distribute files into folders
    story_hash = hashlib.md5(url.encode()).hexdigest()
    # Take the first 2 characters for the folder name
    folder_name = story_hash[:2]
    folder_path = os.path.join(cache_path, folder_name)

    # Create the folder if it doesn't exist
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)

    # Define the summary file path
    summary_file_path = os.path.join(folder_path, f"{story_hash}_summary.txt")

    # Check if the summary file exists
    if os.path.exists(summary_file_path):
        # Read the summary from the file
        with open(summary_file_path, 'r') as file:
            summary = file.read()
        logging.info(f"Reading summary for {url} from cache ...")
        return summary

    text = extract_content(url)
    # Truncate text to max_characters
    text = text[:max_characters]
    # Generate the summary
    summary = generate_summary(text)
    # Save the summary to the file
    with open(summary_file_path, 'w') as file:
        file.write(summary)
    return summary


def extract_content(url):
    # The URL you want to extract information from
    text = ""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded is None:
            result = trafilatura.extract(downloaded)
            if not result is None:
                text = result
    except Exception as e:
        logging.error(
            "Error fetching content for url '%s': %s", url, e)
    return text


def fetch_stories_with_keywords(keywords, days_ago):
    aggregated_result = []
    story_id_set = set()

    for keyword in keywords:
        query = quote(keyword)
        timestamp = int(
            (datetime.now() - timedelta(days=days_ago)).timestamp())

        request_url = f"https://hn.algolia.com/api/v1/search?query={query}&tags=story&restrictSearchableAttributes=title,story_text&typoTolerance=false&numericFilters=created_at_i>{timestamp}"
        logging.info("Requesting URL: %s", request_url)

        try:
            response = requests.get(request_url)
            response.raise_for_status()
            result = response.json()

            for hit in result['hits']:
                if hit.get('url') and hit['story_id'] not in story_id_set:
                    aggregated_result.append(hit)
                    story_id_set.add(hit['story_id'])

        except Exception as e:
            logging.error(
                "Error fetching stories for keyword '%s': %s", keyword, e)
            continue

    return aggregated_result


def read_lines_from_file(file_path):
    lines = []
    try:
        with open(file_path, 'r') as file:
            for line in file:
                lines.append(line.strip())
    except Exception as e:
        raise e

    return lines


def initialize_reddit():
    # Reddit API credentials
    client_id = os.environ.get('REDDIT_CLIENT_ID')
    client_secret = os.environ.get('REDDIT_CLIENT_SECRET')
    user_agent = 'script:news-aggregator:v1.0 (by u/denverdino)'

    # Initialize PRAW with your credentials
    reddit = praw.Reddit(client_id=client_id,
                         client_secret=client_secret,
                         user_agent=user_agent)
    return reddit


def fetch_posts_from_reddit(reddit, subreddit_names):
    results = []
    # The subreddit you want to search in, keyword, and the time frame
    # 'kubernetes+LocalLLaMA'

    # Initialize a subreddit instance
    subreddit = reddit.subreddit('+'.join(subreddit_names))

    # Calculate 24 hours ago
    one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)

    # Regular expression to match image file extensions
    image_pattern = re.compile(r'\.(jpg|jpeg|png|gif)$', re.IGNORECASE)

    # Fetch new link posts in the last 24 hours
    for submission in subreddit.new(limit=200):
        post_time = datetime.fromtimestamp(
            submission.created_utc, tz=timezone.utc)
        if post_time > one_day_ago and not submission.is_self and not submission.spoiler and not submission.over_18:
            # Check if the URL is an image or a relative link
            if not image_pattern.search(submission.url) and not submission.url.startswith('/'):
                results.append({
                    "url": submission.url,
                    "title": submission.title
                })
    return results


def get_posts_from_feeds(rss_url, current_datetime, delta, category=None, keywords=None, max_characters=1024):
    feed = feedparser.parse(rss_url)

    items = []

    for entry in feed.entries:
        post_title = entry.title
        post_link = entry.link
        post_date = entry.get('published_parsed', None)
        if post_date is None:
            post_datetime = datetime.now()
        else:
            post_datetime = datetime(
                post_date.tm_year, post_date.tm_mon, post_date.tm_mday,
                post_date.tm_hour, post_date.tm_min, post_date.tm_sec
            )

        # Calculate the difference between the two dates
        difference = abs(current_datetime - post_datetime)

        if (difference > delta):
            continue

        # Check if the entry matches the specified category
        if category and 'category' in entry:
            entry_categories = [tag.term.lower() for tag in entry.tags]
            if category.lower() not in entry_categories:
                continue

        summary = entry.get('summary', "")
        if summary == "":
            post_summary = ""
        else:
            post_summary = trafilatura.extract(summary)
            if post_summary is None:
                post_summary = summary
            else:
                post_summary = post_summary[:max_characters]

        # Filter by keywords in the summary
        if keywords:
            if not any(re.search(r'\b' + re.escape(keyword) + r'\b', post_summary, re.IGNORECASE) for keyword in keywords):
                continue

        print(f"Title: {post_title}\nURL: {post_link}")

        items.append({
            "title": post_title,
            "url": post_link,
            "date": post_datetime.date(),
            "summary": post_summary
        })

    return items


def send_html_email(subject, html_content, emails):
    # Your Gmail credentials
    gmail_user = 'test.denverdino@gmail.com'
    gmail_password = os.getenv("GMAIL_PASSWORD")  # Use your app password here

    # Set up the email
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = gmail_user
    msg['To'] = ','.join(emails)

    # Attach the HTML content
    part2 = MIMEText(html_content, 'html')
    msg.attach(part2)

    try:
        # Send the email
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, emails, msg.as_string())
            logging.info("News email is sent successfully!")
    except Exception as e:
        logging.error(f"Failed to send HTML email: {e}")


if __name__ == "__main__":
    exec_path = os.path.abspath(__file__)
    exec_dir = os.path.dirname(exec_path)
    # get config file path from arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, help='Path to the config file')
    config_file_path = parser.parse_args().config

    if config_file_path is None:
        config_file_path = os.path.join(exec_dir, "config.yaml")

    # Create the cache directory if it doesn't exist
    cache_path = os.path.join(exec_dir, "cache")
    if not os.path.exists(cache_path):
        os.makedirs(cache_path)

    try:
        config = parse_yaml_config(config_file_path)
        keywords = config.get('hackernews')
        subreddit_names = config.get('reddit')
        feeds = config.get('feeds')
        emails = config['emails']
        subject = config['subject']
    except Exception as e:
        logging.error(f"Error reading config file: {e}")
        exit(1)

    aggregated_items = []
    if keywords is not None:
        items = fetch_stories_with_keywords(keywords, 1)
        for item in items:
            url = item['url']
            item['summary'] = ""
            print(f"Title: {item['title']}\nURL: {url}")
            try:
                summary = generate_summary_with_cache(
                    url, cache_path=cache_path)
                print(f"Summary: {summary}\n")
                item['summary'] = summary
            except Exception as e:
                logging.error(f"Error fetching content for {url}: {e}")

        aggregated_items += items

    if subreddit_names is not None:
        reddit = initialize_reddit()
        items2 = fetch_posts_from_reddit(reddit, subreddit_names)
        for item in items2:
            url = item['url']
            item['summary'] = ""
            print(f"Title: {item['title']}\nURL: {url}")
            try:
                summary = generate_summary_with_cache(
                    url, cache_path=cache_path)
                print(f"Summary: {summary}\n")
                item['summary'] = summary
            except Exception as e:
                logging.error(f"Error fetching content for {url}: {e}")

        aggregated_items += items2

    if feeds is not None:
        current_date = datetime.now()
        delta = timedelta(days=1)
        for feed in feeds:
            rss_url = feed['url']
            category = feed.get('category')
            keywords_string = feed.get('keywords')
            if keywords_string is not None:
                keywords = keywords_string.split(',')
            else:
                keywords = None
            print(f"Fetching feed {rss_url} ...\n")
            aggregated_items += get_posts_from_feeds(
                rss_url, current_date, delta=delta, category=category, keywords=keywords)

    # Base HTML template before the list
    html_content = """
<html>
<head>
    <meta charset="UTF-8">
    <title>Recommended News</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f9f9f9;
        }
        .container {
            max-width: 600px;
            margin: auto;
            background: white;
            padding: 20px;
            box-shadow: 0 0 10px rgba(0, 0, 0, 0.1);
        }
        h2 {
            color: #333;
            border-bottom: 1px solid #d3d3d3;
            padding-bottom: 10px;
        }
        .news-item {
            margin-bottom: 15px;
        }
        .news-title a {
            color: #000;
            text-decoration: none;
            font-weight: bold;
            font-size: 16px
        }
        .news-url {
            color: #666;
            margin-top: 5px;
            font-size: 14px;
        }
        .news-summary {
            color: #333;
            margin-top: 5px;
            font-size: 16px
        }
    </style>
</head>
<body>
    <div class="container">
        <h2>Recommended News</h2>
    """

    # Append each news item to the HTML content, with HTML encoding
    for item in aggregated_items:
        title_encoded = html.escape(item["title"])
        summary_encoded = item["summary"]
        html_content += f"""
        <div class="news-item">
            <div class="news-title">
                <a href="{item["url"]}" target="_blank">{title_encoded}</a>
            </div>
            <div class="news-url">
                {item["url"]}
            </div>
            <div class="news-summary">
                {summary_encoded}
            </div>
        </div>
        """
    # Close the list and the HTML document
    html_content += """
    </div>
</body>
</html>
    """
    logging.info(html_content)
    send_html_email(subject, html_content, emails)
