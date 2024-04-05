import requests
import json
import os
import time
from datetime import datetime, timedelta
from urllib.parse import quote
import hashlib
from openai import AzureOpenAI
import os
import trafilatura
import html
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging

log_level_name = os.getenv('LOG_LEVEL', 'WARNING').upper()
log_level = logging.getLevelName(log_level_name)
# Validate the log_level
if not isinstance(log_level, int):
    raise ValueError(f"Invalid log level: {log_level_name}")

# Configure logging
logging.basicConfig(level=log_level)


def generate_summary(text, model="gpt-35-turbo", temperature=0.7, max_tokens=100):
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
    client = AzureOpenAI(
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("OPENAI_API_KEY"),
        api_version="2024-02-01"
    )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": f"Summarize the following text:\n\n{text}"},
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
    # url = 'https://www.theregister.com/2024/03/28/databricks_dbrx_llm/'
    downloaded = trafilatura.fetch_url(url)
    if downloaded is None:
        return ""
    text = trafilatura.extract(downloaded)
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


def read_keywords_from_file(file_path):
    keywords = []
    try:
        with open(file_path, 'r') as file:
            for line in file:
                keywords.append(line.strip())
    except Exception as e:
        raise e

    return keywords


def send_html_email(subject, html_content, to_email):
    # Your Gmail credentials
    gmail_user = 'test.denverdino@gmail.com'
    gmail_password = os.getenv("GMAIL_PASSWORD")  # Use your app password here

    # Set up the email
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = gmail_user
    msg['To'] = to_email

    # Attach the HTML content
    part2 = MIMEText(html_content, 'html')
    msg.attach(part2)

    try:
        # Send the email
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, to_email, msg.as_string())
            logging.info("News email is sent successfully!")
    except Exception as e:
        logging.info(f"Failed to send HTML email: {e}")


if __name__ == "__main__":
    exec_path = os.path.abspath(__file__)
    exec_dir = os.path.dirname(exec_path)
    file_path = os.path.join(exec_dir, "keywords.txt")

    # Create the cache directory if it doesn't exist
    cache_path = os.path.join(exec_dir, "cache")
    if not os.path.exists(cache_path):
        os.makedirs(cache_path)

    try:
        keywords = read_keywords_from_file(file_path)
    except Exception as e:
        logging.error(f"Error reading file: {e}")
        exit(1)

    hits = fetch_stories_with_keywords(keywords, 1)
    for item in hits:
        url = item['url']
        item['summary'] = ""
        print(f"Title: {item['title']}\nURL: {url}")
        try:
            summary = generate_summary_with_cache(url, cache_path=cache_path)
            print(f"Summary: {summary}\n")
            item['summary'] = summary
        except Exception as e:
            logging.error(f"Error fetching content: {e}")

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
    for item in hits:
        title_encoded = html.escape(item["title"])
        summary_encoded = html.escape(item["summary"])
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
    to_email = "denverdino@gmail.com"
    subject = "Your Hacker News Digest"
    send_html_email(subject, html_content, to_email)
