"""
engines/screener.py — Coin Discovery Engine.

Pulls all active USDT pairs from Bybit, then filters them by:
  1. Coin age (>= MIN_COIN_AGE_DAYS)
  2. Market cap (MIN_MARKET_CAP_USD to MAX_MARKET_CAP_USD)
  3. Partnership keyword hits in recent news

Returns a ranked watchlist of up to MAX_WATCHLIST_SIZE coins.
"""

import time
from datetime import datetime, timezone
from typing import List, Dict

import requests
from loguru import logger

import config
from database.db_setup import get_session
from database.models import ScreenedCoin


# ─── Bybit ────────────────────────────────────────────────────────────────────

def fetch_bybit_usdt_pairs() -> List[str]:
    """Return all active USDT spot symbols from Bybit (e.g. ['BTCUSDT', ...])."""
    url = "https://api.bybit.com/v5/market/instruments-info"
    params = {"category": "spot"}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        symbols = [
            item["symbol"]
            for item in data.get("result", {}).get("list", [])
            if item.get("quoteCoin") == "USDT" and item.get("status") == "Trading"
        ]
        logger.info(f"Bybit returned {len(symbols)} active USDT pairs")
        return symbols
    except Exception as e:
        logger.error(f"Failed to fetch Bybit pairs: {e}")
        return []


# ─── CoinGecko ────────────────────────────────────────────────────────────────

def _coingecko_headers() -> Dict:
    headers = {"accept": "application/json"}
    if config.COINGECKO_API_KEY:
        # Demo keys start with "CG-" and use x-cg-demo-api-key.
        # Pro keys use x-cg-pro-api-key.
        key = config.COINGECKO_API_KEY
        if key.startswith("CG-"):
            headers["x-cg-demo-api-key"] = key
        else:
            headers["x-cg-pro-api-key"] = key
    return headers


def fetch_all_coin_markets(pages: int = 4) -> Dict[str, Dict]:
    """
    Batch-fetch market cap for all coins from CoinGecko using /coins/markets.
    Returns a dict keyed by UPPERCASE symbol: { "market_cap_usd": ..., "coin_id": ... }

    /coins/markets returns 250 coins per page sorted by market cap.
    4 pages = top 1000 coins — more than enough to cover all Bybit-listed coins.
    """
    results: Dict[str, Dict] = {}
    for page in range(1, pages + 1):
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                    "sparkline": "false",
                },
                headers=_coingecko_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            for coin in resp.json():
                sym = (coin.get("symbol") or "").upper()
                mcap = coin.get("market_cap")
                coin_id = coin.get("id")
                if sym and mcap is not None and coin_id:
                    # Keep the entry with the highest market cap if symbol appears twice
                    if sym not in results or mcap > results[sym]["market_cap_usd"]:
                        results[sym] = {"market_cap_usd": mcap, "coin_id": coin_id}
            logger.debug(f"CoinGecko markets page {page}: {len(results)} total coins loaded")
            time.sleep(2.0)  # 30 req/min demo rate limit
        except Exception as e:
            logger.warning(f"CoinGecko /coins/markets page {page} failed: {e}")
            time.sleep(5.0)
    logger.info(f"CoinGecko batch fetch complete: {len(results)} coins indexed")
    return results


def fetch_genesis_date(coin_id: str) -> datetime | None:
    """Fetch just the genesis_date for a single coin by its CoinGecko ID."""
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}",
            params={"localization": "false", "tickers": "false",
                    "market_data": "false", "community_data": "false"},
            headers=_coingecko_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        genesis_str = resp.json().get("genesis_date")
        if genesis_str:
            return datetime.strptime(genesis_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception as e:
        logger.debug(f"genesis_date fetch failed for {coin_id}: {e}")
    return None


def fetch_coin_metadata(base_currency: str) -> Dict:
    """
    Single-coin fallback: fetch launch date and market cap from CoinGecko.
    Used only when the batch fetch missed this coin (e.g. outside top 1000).
    """
    search_url = "https://api.coingecko.com/api/v3/search"
    try:
        resp = requests.get(
            search_url,
            params={"query": base_currency},
            headers=_coingecko_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("coins", [])
        if not results:
            return {}

        coin_id = None
        for coin in results[:5]:
            if coin.get("symbol", "").upper() == base_currency.upper():
                coin_id = coin["id"]
                break
        if not coin_id:
            coin_id = results[0]["id"]

        detail_resp = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}",
            params={"localization": "false", "tickers": "false", "community_data": "false"},
            headers=_coingecko_headers(),
            timeout=10,
        )
        detail_resp.raise_for_status()
        detail = detail_resp.json()

        genesis_str = detail.get("genesis_date")
        launch_date = None
        if genesis_str:
            try:
                launch_date = datetime.strptime(genesis_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        market_cap = (
            detail.get("market_data", {}).get("market_cap", {}).get("usd")
        )
        return {"launch_date": launch_date, "market_cap_usd": market_cap, "coin_id": coin_id}

    except Exception as e:
        logger.warning(f"CoinGecko lookup failed for {base_currency}: {e}")
        return {}


# ─── Partnership keyword filter ───────────────────────────────────────────────

def fetch_partnership_score(base_currency: str) -> int:
    """
    Search for recent partnership-related news about the coin using:
      1. Free RSS feeds (CoinDesk, CoinTelegraph, Decrypt — no API key)
      2. NewsAPI (fallback — uses NEWSAPI_KEY if set)
    Returns the count of partnership keyword hits across all articles.
    """
    import xml.etree.ElementTree as ET

    RSS_FEEDS = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://news.bitcoin.com/feed/",
    ]

    keyword_in_article = base_currency.upper()
    score = 0

    # --- RSS feeds (free, no key needed) ---
    for feed_url in RSS_FEEDS:
        try:
            resp = requests.get(
                feed_url,
                timeout=10,
                headers={"User-Agent": "SweptCoinAI/1.0"},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.iter("item"):
                title_el = item.find("title")
                desc_el = item.find("description")
                title = (title_el.text or "") if title_el is not None else ""
                desc = (desc_el.text or "") if desc_el is not None else ""
                combined = f"{title} {desc}".lower()
                if keyword_in_article.lower() not in combined:
                    continue
                for kw in config.PARTNERSHIP_KEYWORDS:
                    if kw.lower() in combined:
                        score += 1
        except Exception as e:
            logger.debug(f"RSS partnership scan failed [{feed_url}]: {e}")

    # --- NewsAPI fallback ---
    if config.NEWSAPI_KEY:
        try:
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": f"{base_currency} crypto partnership",
                "apiKey": config.NEWSAPI_KEY,
                "pageSize": 20,
                "sortBy": "publishedAt",
                "language": "en",
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
            for article in articles:
                title = (article.get("title") or "").lower()
                desc = (article.get("description") or "").lower()
                combined = title + " " + desc
                for kw in config.PARTNERSHIP_KEYWORDS:
                    if kw.lower() in combined:
                        score += 1
            return score
        except Exception as e:
            logger.warning(f"NewsAPI failed for {base_currency}: {e}")

    return score


# ─── Main screener function ───────────────────────────────────────────────────

def run_screener() -> List[Dict]:
    """
    Full screener pass. Returns a list of qualifying coin dicts, saved to DB.
    Each dict: { symbol, base_currency, market_cap_usd, launch_date, age_days,
                 partnership_score }
    """
    logger.info("=== Screener Engine: Starting scan ===")
    symbols = fetch_bybit_usdt_pairs()
    if not symbols:
        logger.error("No symbols fetched. Aborting screener.")
        return []

    # ── Step 1: Batch-fetch market caps (2 API calls instead of 459) ──
    markets = fetch_all_coin_markets(pages=4)

    qualified = []
    now = datetime.now(timezone.utc)

    # Build a set of base tickers from Bybit for quick lookup
    bybit_bases = {sym.replace("USDT", ""): sym for sym in symbols if sym.endswith("USDT")}

    for base, symbol in bybit_bases.items():
        coin_info = markets.get(base.upper())

        # Fallback: coin not in top-1000 batch — do individual lookup
        if coin_info is None:
            time.sleep(2.0)
            meta = fetch_coin_metadata(base)
            if not meta:
                continue
            coin_info = {"market_cap_usd": meta.get("market_cap_usd"), "coin_id": meta.get("coin_id")}
            launch_date = meta.get("launch_date")
        else:
            launch_date = None  # will fetch below if market cap qualifies

        market_cap = coin_info.get("market_cap_usd")
        coin_id = coin_info.get("coin_id")

        # ── Filter 1: Market cap ──
        if market_cap is None:
            continue
        if not (config.MIN_MARKET_CAP_USD <= market_cap <= config.MAX_MARKET_CAP_USD):
            continue

        # ── Filter 2: Age — only fetch genesis date for coins that passed market cap ──
        if launch_date is None and coin_id:
            time.sleep(2.0)
            launch_date = fetch_genesis_date(coin_id)
        if launch_date is None:
            continue
        age_days = (now - launch_date).days
        if age_days < config.MIN_COIN_AGE_DAYS:
            continue

        # ── Filter 3: Partnership keywords ──
        time.sleep(0.5)
        p_score = fetch_partnership_score(base)

        coin_data = {
            "symbol": symbol,
            "base_currency": base,
            "market_cap_usd": market_cap,
            "launch_date": launch_date,
            "age_days": age_days,
            "partnership_score": p_score,
        }
        qualified.append(coin_data)
        logger.info(
            f"  QUALIFIED: {symbol} | mcap=${market_cap:,.0f} | "
            f"age={age_days}d | partnerships={p_score}"
        )

        if len(qualified) >= config.MAX_WATCHLIST_SIZE:
            break

    # Sort by partnership score descending
    qualified.sort(key=lambda x: x["partnership_score"], reverse=True)

    _save_to_db(qualified)
    logger.info(f"=== Screener complete: {len(qualified)} coins qualify ===")
    return qualified


def _save_to_db(coins: List[Dict]):
    """Persist screened coins to the database, updating existing records."""
    session = get_session()
    try:
        # Mark all existing coins inactive before refreshing
        session.query(ScreenedCoin).update({"is_active": False})
        for c in coins:
            existing = (
                session.query(ScreenedCoin)
                .filter_by(symbol=c["symbol"])
                .first()
            )
            if existing:
                existing.market_cap_usd = c["market_cap_usd"]
                existing.launch_date = c["launch_date"]
                existing.age_days = c["age_days"]
                existing.partnership_score = c["partnership_score"]
                existing.is_active = True
                existing.last_screened = datetime.utcnow()
            else:
                session.add(ScreenedCoin(
                    symbol=c["symbol"],
                    base_currency=c["base_currency"],
                    market_cap_usd=c["market_cap_usd"],
                    launch_date=c["launch_date"],
                    age_days=c["age_days"],
                    partnership_score=c["partnership_score"],
                    is_active=True,
                ))
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"DB save failed in screener: {e}")
    finally:
        session.close()
