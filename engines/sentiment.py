"""
engines/sentiment.py — Fundamental & Sentiment Engine.

Fetches recent news for a coin and scores the market mood using VADER.

News sources (all free, no CryptoPanic required):
  1. FREE RSS feeds — CoinDesk, CoinTelegraph, Decrypt (no API key needed)
  2. NewsAPI fallback — coin-specific search (free developer plan)

A score below MIN_SENTIMENT_SCORE in config.py blocks all trades for that coin.
"""

import xml.etree.ElementTree as ET
from typing import Dict, List

import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from loguru import logger

import config

_analyzer = SentimentIntensityAnalyzer()

# ─── Free RSS feeds (no API key required) ────────────────────────────────────
# These are the top crypto news outlets — publicly available, always free.

_RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://news.bitcoin.com/feed/",
]


# ─── News fetching ────────────────────────────────────────────────────────────

def _fetch_rss_headlines(base_currency: str) -> List[str]:
    """
    Pull headlines from free crypto news RSS feeds and filter for those
    that mention the coin's ticker or name.
    No API key needed.
    """
    keyword = base_currency.upper()
    matched = []

    for feed_url in _RSS_FEEDS:
        try:
            resp = requests.get(
                feed_url,
                timeout=10,
                headers={"User-Agent": "SweptCoinAI/1.0"},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)

            # RSS items are under channel/item
            for item in root.iter("item"):
                title_el = item.find("title")
                desc_el = item.find("description")
                title = (title_el.text or "") if title_el is not None else ""
                desc = (desc_el.text or "") if desc_el is not None else ""
                combined = f"{title} {desc}"

                # Include if the coin ticker appears in the headline
                if keyword in combined.upper():
                    matched.append(f"{title}. {desc}".strip())

        except Exception as e:
            logger.debug(f"RSS feed failed [{feed_url}]: {e}")
            continue

    logger.debug(f"RSS: found {len(matched)} articles mentioning {keyword}")
    return matched


def _fetch_newsapi(base_currency: str) -> List[str]:
    """Fetch recent news headlines from NewsAPI for a given coin (free plan)."""
    if not config.NEWSAPI_KEY:
        return []
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": f"{base_currency} crypto",
                "apiKey": config.NEWSAPI_KEY,
                "pageSize": config.SENTIMENT_NEWS_LIMIT,
                "sortBy": "publishedAt",
                "language": "en",
            },
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        headlines = []
        for a in articles:
            title = a.get("title") or ""
            desc = a.get("description") or ""
            if title:
                headlines.append(f"{title}. {desc}")
        return headlines
    except Exception as e:
        logger.warning(f"NewsAPI sentiment fetch failed for {base_currency}: {e}")
        return []


def _fetch_google_news(coin_name: str, ticker: str) -> List[str]:
    """
    Fetch coin-specific headlines from Google News RSS.
    Searches by full coin name (e.g. 'Chainlink') for precise results.
    No API key required — aggregates hundreds of news sources.
    """
    url = "https://news.google.com/rss/search"
    params = {
        "q": f"{coin_name} cryptocurrency",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    }
    try:
        resp = requests.get(
            url, params=params, timeout=10,
            headers={"User-Agent": "SweptCoinAI/1.0"},
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        headlines = []
        for item in root.iter("item"):
            title_el = item.find("title")
            title = (title_el.text or "") if title_el is not None else ""
            if title:
                headlines.append(title)
        logger.debug(f"Google News: {len(headlines)} articles for {coin_name}")
        return headlines
    except Exception as e:
        logger.debug(f"Google News fetch failed [{coin_name}]: {e}")
        return []


def fetch_headlines(base_currency: str, coin_name: str = "") -> List[str]:
    """
    Collect headlines from all sources:
      1. Google News RSS — coin-specific search by full name (e.g. 'Chainlink')
      2. Free crypto RSS feeds — CoinDesk, CoinTelegraph, Decrypt, Bitcoin.com
      3. NewsAPI fallback — coin-specific keyword search
    Returns a deduplicated list up to SENTIMENT_NEWS_LIMIT.
    """
    headlines = []

    # Google News first — best coverage for established coins
    if coin_name:
        headlines += _fetch_google_news(coin_name, base_currency)

    # General crypto RSS feeds (ticker-filtered)
    headlines += _fetch_rss_headlines(base_currency)

    # Top up with NewsAPI if still sparse
    if len(headlines) < 5:
        headlines += _fetch_newsapi(base_currency)

    seen = set()
    unique = []
    for h in headlines:
        if h not in seen:
            seen.add(h)
            unique.append(h)
    return unique[: config.SENTIMENT_NEWS_LIMIT]


# ─── Scoring ──────────────────────────────────────────────────────────────────

def score_headlines(headlines: List[str]) -> float:
    """
    Run VADER on each headline and return the average compound score.
    Range: -1.0 (very negative) to +1.0 (very positive).
    Returns 0.0 if no headlines are available.
    """
    if not headlines:
        return 0.0
    scores = [_analyzer.polarity_scores(h)["compound"] for h in headlines]
    return round(sum(scores) / len(scores), 4)


# ─── Public interface ─────────────────────────────────────────────────────────

def get_sentiment(base_currency: str, coin_name: str = "") -> Dict:
    """
    Full sentiment check for one coin.

    Returns:
        {
            "base_currency": str,
            "score": float,          # -1.0 to +1.0
            "headline_count": int,
            "trade_allowed": bool,   # False if score < MIN_SENTIMENT_SCORE
            "reason": str,
        }
    """
    headlines = fetch_headlines(base_currency, coin_name=coin_name)
    score = score_headlines(headlines)
    trade_allowed = score >= config.MIN_SENTIMENT_SCORE

    # Sentiment direction label
    if score >= 0.15:
        sentiment_direction = "BULLISH"
    elif score <= -0.15:
        sentiment_direction = "BEARISH"
    else:
        sentiment_direction = "NEUTRAL"

    if trade_allowed:
        reason = (
            f"Score {score:.3f} ({sentiment_direction}) passes threshold "
            f"{config.MIN_SENTIMENT_SCORE} across {len(headlines)} articles"
        )
    else:
        reason = (
            f"Score {score:.3f} ({sentiment_direction}) is below threshold "
            f"{config.MIN_SENTIMENT_SCORE} — trade blocked"
        )

    logger.info(f"Sentiment [{base_currency}] score={score:.3f} {sentiment_direction} | {len(headlines)} articles | {'OK' if trade_allowed else 'BLOCKED'}")

    return {
        "base_currency": base_currency,
        "score": score,
        "sentiment_direction": sentiment_direction,
        "headline_count": len(headlines),
        "trade_allowed": trade_allowed,
        "reason": reason,
    }


def filter_by_sentiment(coins: List[Dict]) -> List[Dict]:
    """
    Given a list of coin dicts (each with 'base_currency'),
    return only those that pass the sentiment threshold.
    Attaches 'sentiment_score' and 'sentiment_direction' to EVERY coin
    (for reporting) regardless of whether it passes.
    If nothing passes (e.g. all neutral with 0 articles), pass them all
    — no bearish signal is itself a bullish-neutral signal.
    """
    approved = []
    for coin in coins:
        base = coin.get("base_currency", coin.get("symbol", "").replace("USDT", ""))
        name = coin.get("coin_name", "")
        result = get_sentiment(base, coin_name=name)
        coin["sentiment_score"] = result["score"]
        coin["sentiment_direction"] = result["sentiment_direction"]
        if result["trade_allowed"]:
            approved.append(coin)

    if not approved and coins:
        logger.warning(
            "Sentiment filter blocked all coins — no bearish signals found either. "
            "Passing all screened coins as NEUTRAL candidates."
        )
        return coins
    return approved
