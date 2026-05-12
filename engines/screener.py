"""
engines/screener.py — Coin Discovery Engine.

Pulls all active USDT pairs from Bybit, then filters by:
  1. Market cap  (batch, fast — no per-coin API calls)
  2. Coin age    (one API call per candidate that passed cap filter)
  3. Partnership news — with named company/project partners extracted

Returns a ranked watchlist of up to MAX_WATCHLIST_SIZE coins, each with
a 'partners' list of actual org names found in recent news.
"""

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
from loguru import logger

import config
from database.db_setup import get_session
from database.models import ScreenedCoin


# ─── Bybit ────────────────────────────────────────────────────────────────────

def fetch_bybit_usdt_pairs() -> List[str]:
    """Return all active USDT spot symbols from Bybit."""
    try:
        resp = requests.get(
            "https://api.bybit.com/v5/market/instruments-info",
            params={"category": "spot"},
            timeout=10,
        )
        resp.raise_for_status()
        symbols = [
            item["symbol"]
            for item in resp.json().get("result", {}).get("list", [])
            if item.get("quoteCoin") == "USDT" and item.get("status") == "Trading"
        ]
        logger.info(f"Bybit returned {len(symbols)} active USDT pairs")
        return symbols
    except Exception as e:
        logger.error(f"Failed to fetch Bybit pairs: {e}")
        return []


# ─── CoinGecko ────────────────────────────────────────────────────────────────

def _cg_headers() -> Dict:
    headers = {"accept": "application/json"}
    key = config.COINGECKO_API_KEY
    if key:
        headers["x-cg-demo-api-key" if key.startswith("CG-") else "x-cg-pro-api-key"] = key
    return headers


def fetch_all_coin_markets(pages: int = 2) -> Dict[str, Dict]:
    """
    Batch-fetch market caps — 2 pages × 250 = top 500 coins.
    All $300M+ coins are within the top 200, so 2 pages is sufficient.
    Returns dict keyed by UPPERCASE symbol: { market_cap_usd, coin_id, coin_name }
    Only 2 API calls total.
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
                headers=_cg_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            for coin in resp.json():
                sym = (coin.get("symbol") or "").upper()
                mcap = coin.get("market_cap")
                cid = coin.get("id")
                if sym and mcap is not None and cid:
                    if sym not in results or mcap > results[sym]["market_cap_usd"]:
                        results[sym] = {
                            "market_cap_usd": mcap,
                            "coin_id": cid,
                            "coin_name": coin.get("name", sym),
                        }
            logger.debug(f"CoinGecko page {page}: {len(results)} coins indexed")
            time.sleep(2.0)
        except Exception as e:
            logger.warning(f"CoinGecko markets page {page} failed: {e}")
            time.sleep(5.0)
    logger.info(f"CoinGecko batch fetch complete: {len(results)} coins indexed")
    return results


def fetch_genesis_date(coin_id: str) -> Optional[datetime]:
    """Fetch just the genesis_date for one coin. Returns None if unknown."""
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}",
            params={
                "localization": "false", "tickers": "false",
                "market_data": "false", "community_data": "false",
            },
            headers=_cg_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        g = resp.json().get("genesis_date")
        if g:
            return datetime.strptime(g, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception as e:
        logger.debug(f"genesis_date fetch failed [{coin_id}]: {e}")
    return None


# ─── Partnership detection with named partners ────────────────────────────────

_RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://news.bitcoin.com/feed/",
]

# Words to exclude when extracting org names from headlines
_NAME_NOISE = {
    "Bitcoin", "Ethereum", "Crypto", "Token", "Blockchain", "DeFi", "NFT",
    "Web3", "The", "This", "That", "With", "For", "And", "But", "Has",
    "Have", "Will", "Are", "Its", "New", "Market", "Price", "USD", "USDT",
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "Network", "Protocol", "Foundation", "Report", "Update", "Exchange",
    "Trading", "According", "Announces", "Launches", "Platform",
}


def _extract_org_names(headline: str) -> List[str]:
    """
    Pull capitalized multi-word phrases from a headline as likely org/company names.
    e.g. 'Waves partners with Google Cloud' → ['Google Cloud']
    """
    candidates = re.findall(
        r'\b(?:[A-Z][a-zA-Z0-9]+)(?:\s+[A-Z][a-zA-Z0-9]+){0,3}\b',
        headline,
    )
    seen: set = set()
    names: List[str] = []
    for c in candidates:
        words = c.split()
        if any(w in _NAME_NOISE for w in words):
            continue
        if len(c) <= 2:
            continue
        if c.isupper() and len(c) <= 6:   # skip pure tickers like "ETH", "WAVESUSDT"
            continue
        if c not in seen:
            seen.add(c)
            names.append(c)
    return names


def fetch_partnership_details(base_currency: str) -> Dict:
    """
    Scan RSS feeds + NewsAPI for partnership news.
    Returns { "score": int, "partners": [str, ...] }
    'partners' contains extracted company/project names (deduplicated, max 8).
    """
    keyword = base_currency.upper()
    score = 0
    all_partners: List[str] = []

    # ── RSS feeds ──
    for feed_url in _RSS_FEEDS:
        try:
            resp = requests.get(feed_url, timeout=10, headers={"User-Agent": "SweptCoinAI/1.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.iter("item"):
                title_el = item.find("title")
                desc_el = item.find("description")
                title = (title_el.text or "") if title_el is not None else ""
                desc = (desc_el.text or "") if desc_el is not None else ""
                combined = f"{title} {desc}"
                if keyword not in combined.upper():
                    continue
                for kw in config.PARTNERSHIP_KEYWORDS:
                    if kw.lower() in combined.lower():
                        score += 1
                        all_partners.extend(_extract_org_names(title))
        except Exception as e:
            logger.debug(f"RSS scan failed [{feed_url}]: {e}")

    # ── NewsAPI fallback ──
    if config.NEWSAPI_KEY:
        try:
            resp = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": f"{base_currency} crypto partnership",
                    "apiKey": config.NEWSAPI_KEY,
                    "pageSize": 20,
                    "sortBy": "publishedAt",
                    "language": "en",
                },
                timeout=10,
            )
            resp.raise_for_status()
            for article in resp.json().get("articles", []):
                title = article.get("title") or ""
                desc = article.get("description") or ""
                combined = f"{title} {desc}".lower()
                for kw in config.PARTNERSHIP_KEYWORDS:
                    if kw.lower() in combined:
                        score += 1
                        all_partners.extend(_extract_org_names(title))
        except Exception as e:
            logger.warning(f"NewsAPI failed for {base_currency}: {e}")

    # Deduplicate partners
    seen: set = set()
    unique_partners: List[str] = []
    for p in all_partners:
        if p not in seen:
            seen.add(p)
            unique_partners.append(p)

    return {"score": score, "partners": unique_partners[:8]}


# ─── Main screener ────────────────────────────────────────────────────────────

def run_screener() -> List[Dict]:
    """
    Fast screener — finishes in ~2 min instead of 30+.

    Strategy:
      1. Batch market cap fetch (4 API calls for 1000 coins)
      2. Filter market cap IN MEMORY — zero extra API calls
      3. Sort by market cap, keep MAX_WATCHLIST_SIZE × 4 candidates
      4. For each candidate: fetch genesis_date, stop when watchlist full
      5. For qualifying coins: fetch partnership details + named partners
    """
    logger.info("=== Screener Engine: Starting scan ===")
    symbols = fetch_bybit_usdt_pairs()
    if not symbols:
        logger.error("No symbols fetched. Aborting screener.")
        return []

    # Step 1: Batch market data
    markets = fetch_all_coin_markets(pages=4)
    now = datetime.now(timezone.utc)
    bybit_bases = {sym.replace("USDT", ""): sym for sym in symbols if sym.endswith("USDT")}

    # Step 2: In-memory market cap filter
    candidates = []
    for base, symbol in bybit_bases.items():
        info = markets.get(base.upper())
        if not info:
            continue
        mcap = info["market_cap_usd"]
        if config.MIN_MARKET_CAP_USD <= mcap <= config.MAX_MARKET_CAP_USD:
            candidates.append({
                "base": base, "symbol": symbol,
                "market_cap_usd": mcap,
                "coin_id": info["coin_id"],
                "coin_name": info.get("coin_name", base),
            })

    # Step 3: Sort highest market cap first (most established coins first)
    candidates.sort(key=lambda x: x["market_cap_usd"], reverse=True)
    logger.info(
        f"Market cap filter: {len(candidates)} candidates pass "
        f"(${config.MIN_MARKET_CAP_USD/1e6:.0f}M–${config.MAX_MARKET_CAP_USD/1e6:.0f}M), "
        f"checking age for all {len(candidates)} candidates..."
    )

    # Step 4 & 5: Age check + partnership details
    qualified: List[Dict] = []
    for c in candidates:
        if len(qualified) >= config.MAX_WATCHLIST_SIZE:
            break

        time.sleep(2.0)
        launch_date = fetch_genesis_date(c["coin_id"])
        if launch_date is None:
            continue
        age_days = (now - launch_date).days
        if age_days < config.MIN_COIN_AGE_DAYS:
            continue

        p = fetch_partnership_details(c["base"])
        partner_str = ", ".join(p["partners"]) if p["partners"] else "none found in recent news"

        coin_data = {
            "symbol": c["symbol"],
            "base_currency": c["base"],
            "coin_name": c.get("coin_name", c["base"]),
            "market_cap_usd": c["market_cap_usd"],
            "launch_date": launch_date,
            "age_days": age_days,
            "partnership_score": p["score"],
            "partners": p["partners"],
        }
        qualified.append(coin_data)
        logger.info(
            f"  QUALIFIED: {c['symbol']} | "
            f"mcap=${c['market_cap_usd']:,.0f} | "
            f"age={age_days}d | "
            f"partners: {partner_str}"
        )

    qualified.sort(key=lambda x: x["partnership_score"], reverse=True)
    _save_to_db(qualified)
    logger.info(f"=== Screener complete: {len(qualified)} coins qualify ===")
    return qualified


def _save_to_db(coins: List[Dict]):
    """Persist screened coins to DB, updating existing records."""
    session = get_session()
    try:
        session.query(ScreenedCoin).update({"is_active": False})
        for c in coins:
            existing = session.query(ScreenedCoin).filter_by(symbol=c["symbol"]).first()
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
