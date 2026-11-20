# News Aggregator

Aggregate your favorit news from Hacker News, Reddit and RSS feeds


Modify the `config.yaml` file with your preferences.

```
# install required packages
pip install -r requirements.txt


# export the environment vairables
export GMAIL_PASSWORD=XXX XXX
export REDDIT_CLIENT_ID=XXX XXX
export REDDIT_CLIENT_SECRET=XXX XXX
export DASHSCOPE_API_KEY=XXX XXX

python3 main.py
```

Reddit communities are fetched independently. If a Reddit request fails (for
example, with HTTP 403), the aggregator logs a warning with the community name,
keeps any posts already collected, and continues with the remaining communities,
RSS feeds, and email digest. A 403 warning still requires checking the Reddit
application's API access and the affected community's visibility; continuing
the digest does not restore Reddit access.

Run the offline regression tests with `python3 -m unittest -v`. The tests mock
external services and do not send email. GitHub Actions runs them before the
scheduled digests.
