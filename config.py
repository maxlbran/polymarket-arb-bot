"""
PolyKalshi Arb Engine — Central Configuration
All tuneable parameters live here. Do not scatter magic numbers through the codebase.
"""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ─── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DB_PATH = str(BASE_DIR / "database" / "arb.db")
LOG_PATH = str(BASE_DIR / "logs")
ARB_LOG = str(BASE_DIR / "logs" / "arb.log")
ERROR_LOG = str(BASE_DIR / "logs" / "error.log")

# ─── Version ─────────────────────────────────────────────────────────────────
VERSION = "1.0.0"
SYSTEM_NAME = "PolyKalshi Arb Engine"

# ─── Scan Timing ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", 10))
TIER1_BOOST_INTERVAL_MINUTES = 2        # Scan every 2 min after TIER1 found
TIER1_BOOST_DURATION_MINUTES = 30       # Boost period lasts 30 min
VOLATILE_INTERVAL_MINUTES = 5          # During fast-moving markets
QUIET_HOURS_START = 2                  # 2am GMT
QUIET_HOURS_END = 6                    # 6am GMT
QUIET_INTERVAL_MINUTES = 30

# ─── Filtering Thresholds ────────────────────────────────────────────────────
MIN_NET_SPREAD = float(os.getenv("MIN_NET_SPREAD", 0.00))   # 0% — show all positives
MIN_MATCH_CONFIDENCE = 70              # Minimum confidence score (0-100)
MIN_VOLUME_USD = 5000                  # Minimum volume on each side (USD)
MIN_VOLUME_TIER2 = 5000               # Volume required for TIER2
MIN_VOLUME_TIER1 = 10000              # Volume required for TIER1
MIN_RISK_SCORE = 4                    # Minimum risk score (1-10)
MIN_POSITION_USD = 50                 # Smallest position to consider
MAX_DAYS_TO_RESOLUTION = int(os.getenv("MAX_DAYS_TO_RESOLUTION", 30))  # Exclude far-future markets

# ─── Tier Definitions ────────────────────────────────────────────────────────
TIER1_NET_SPREAD = 0.04               # 4%
TIER1_CONFIDENCE = 85
TIER1_VOLUME = 10000

TIER2_NET_SPREAD = 0.02               # 2%
TIER2_CONFIDENCE = 70
TIER2_VOLUME = 5000

# ─── Fees ────────────────────────────────────────────────────────────────────
POLYMARKET_FEE = 0.01                 # 1% taker fee
KALSHI_FEE = 0.01                     # 1% fee (check their current schedule)
GAS_FEE_ESTIMATE = 0.50              # Polygon gas cost estimate per trade (USD)

# ─── Position Sizes ──────────────────────────────────────────────────────────
POSITION_SIZES = [50, 100, 200, 500, 1000]

# ─── API Settings ────────────────────────────────────────────────────────────
API_TIMEOUT_SECONDS = 10
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0              # 1s, 2s, 4s exponential
CACHE_TTL_SECONDS = 480               # 8 minutes

# ─── Exchange URLs ───────────────────────────────────────────────────────────
POLYMARKET_BASE_URL = "https://gamma-api.polymarket.com"
POLYMARKET_MARKETS_ENDPOINT = "/markets"
POLYMARKET_PAGE_SIZE = 500            # Markets per page

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_MARKETS_ENDPOINT = "/markets"
KALSHI_PAGE_SIZE = 100

# ─── Credentials ─────────────────────────────────────────────────────────────
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
KALSHI_API_SECRET = os.getenv("KALSHI_API_SECRET", "")
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")

# ─── Matching Engine ─────────────────────────────────────────────────────────
FUZZY_MIN_TOKEN_SORT = 72             # Minimum token sort ratio
FUZZY_MIN_TOKEN_SET = 72              # Minimum token set ratio
DATE_WINDOW_DAYS = 7                  # Max days between resolution dates

# Confidence scoring weights
SCORE_STRING_MAX = 40
SCORE_CATEGORY_MAX = 20
SCORE_DATE_MAX = 20
SCORE_VOLUME_MAX = 10
SCORE_ENTITY_MAX = 10

# ─── Dashboard ───────────────────────────────────────────────────────────────
DASHBOARD_REFRESH_SECONDS = 30
DASHBOARD_MAX_ROWS = 20               # Max opportunities to display

# ─── Alerts ──────────────────────────────────────────────────────────────────
ALERT_SOUND = os.getenv("ALERT_SOUND", "true").lower() == "true"
ALERT_BELL_REPEATS = 3

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ─── Category Mapping ────────────────────────────────────────────────────────
STANDARD_CATEGORIES = [
    "Politics",
    "Crypto",
    "Sports",
    "Economics",
    "Science",
    "Entertainment",
    "Other",
]

POLYMARKET_CATEGORY_MAP: dict[str, str] = {
    # Politics
    "politics": "Politics",
    "political": "Politics",
    "election": "Politics",
    "elect": "Politics",
    "government": "Politics",
    "president": "Politics",
    "presidential": "Politics",
    "congress": "Politics",
    "senate": "Politics",
    "democrat": "Politics",
    "republican": "Politics",
    "trump": "Politics",
    "biden": "Politics",
    "harris": "Politics",
    "governor": "Politics",
    "minister": "Politics",
    "parliament": "Politics",
    "vote": "Politics",
    "referendum": "Politics",
    "impeach": "Politics",
    "nato": "Politics",
    "un ": "Politics",
    "sanctions": "Politics",
    "war ": "Politics",
    "ceasefire": "Politics",
    "ukraine": "Politics",
    "russia": "Politics",
    "china": "Politics",
    "taiwan": "Politics",
    "iran": "Politics",
    "israel": "Politics",
    "gaza": "Politics",
    # Crypto
    "crypto": "Crypto",
    "cryptocurrency": "Crypto",
    "bitcoin": "Crypto",
    "btc": "Crypto",
    "ethereum": "Crypto",
    "eth ": "Crypto",
    "solana": "Crypto",
    "sol ": "Crypto",
    "defi": "Crypto",
    "nft": "Crypto",
    "blockchain": "Crypto",
    "token": "Crypto",
    "altcoin": "Crypto",
    "binance": "Crypto",
    "coinbase": "Crypto",
    "sec crypto": "Crypto",
    "xrp": "Crypto",
    "doge": "Crypto",
    "shib": "Crypto",
    # Sports
    "sports": "Sports",
    "football": "Sports",
    "soccer": "Sports",
    "basketball": "Sports",
    "tennis": "Sports",
    "nfl": "Sports",
    "nba": "Sports",
    "mlb": "Sports",
    "nhl": "Sports",
    "mls": "Sports",
    "premier league": "Sports",
    "champions league": "Sports",
    "world cup": "Sports",
    "super bowl": "Sports",
    "championship": "Sports",
    "playoffs": "Sports",
    "tournament": "Sports",
    "match": "Sports",
    "season": "Sports",
    "roster": "Sports",
    "trade deadline": "Sports",
    "mvp": "Sports",
    "draft": "Sports",
    "ufc": "Sports",
    "boxing": "Sports",
    "golf": "Sports",
    "formula 1": "Sports",
    "f1 ": "Sports",
    "olympics": "Sports",
    # Economics
    "economics": "Economics",
    "economy": "Economics",
    "finance": "Economics",
    " fed ": "Economics",
    "federal reserve": "Economics",
    "interest rate": "Economics",
    "inflation": "Economics",
    "gdp": "Economics",
    "recession": "Economics",
    "unemployment": "Economics",
    "cpi": "Economics",
    "s&p": "Economics",
    "nasdaq": "Economics",
    "dow ": "Economics",
    "stock": "Economics",
    "market cap": "Economics",
    "ipo": "Economics",
    "earnings": "Economics",
    "trade war": "Economics",
    "tariff": "Economics",
    "debt ceiling": "Economics",
    "treasury": "Economics",
    # Science / Tech
    "science": "Science",
    "climate": "Science",
    "ai ": "Science",
    "artificial intelligence": "Science",
    "spacex": "Science",
    "nasa": "Science",
    "space ": "Science",
    "launch ": "Science",
    "drug": "Science",
    "fda": "Science",
    "vaccine": "Science",
    "pandemic": "Science",
    "earthquake": "Science",
    "hurricane": "Science",
    # Entertainment
    "entertainment": "Entertainment",
    "award": "Entertainment",
    "oscars": "Entertainment",
    "grammys": "Entertainment",
    "academy award": "Entertainment",
    "golden globe": "Entertainment",
    "box office": "Entertainment",
    "album": "Entertainment",
    "movie": "Entertainment",
    "film": "Entertainment",
    "netflix": "Entertainment",
    "emmy": "Entertainment",
    "grammy": "Entertainment",
    "celebrity": "Entertainment",
    "kardashian": "Entertainment",
}

KALSHI_CATEGORY_MAP: dict[str, str] = {
    "POLITICS": "Politics",
    "ELECTIONS": "Politics",
    "CRYPTO": "Crypto",
    "FINANCIALS": "Economics",
    "ECONOMICS": "Economics",
    "SPORTS": "Sports",
    "SCIENCE": "Science",
    "TECHNOLOGY": "Science",
    "ENTERTAINMENT": "Entertainment",
    "CLIMATE": "Science",
    "HEALTHCARE": "Science",
}
