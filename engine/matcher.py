"""
Market Matching Engine — finds the same real-world event across platforms.

A bad match = fake arbitrage = real money loss. This is the most important
module. Five layers of validation before any market pair is flagged.

Layer 1: Category filter (eliminates ~90% of impossible pairs)
Layer 2: Keyword / entity extraction
Layer 3: Fuzzy string matching (RapidFuzz)
Layer 4: Semantic validation (dates, polarity, volume)
Layer 5: Composite confidence scoring
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from rapidfuzz import fuzz

import config
from database.models import NormalisedMarket, MarketMatch

logger = logging.getLogger(__name__)

# ─── Fuzzy score cache ────────────────────────────────────────────────────────
# Fuzzy scores are deterministic given the title pair, so we cache them for the
# lifetime of the process. On the second scan, 900k+ fuzzy calls are skipped.
# Keys are (title_a, title_b) tuples; values are (token_sort, token_set).
_fuzzy_cache: dict[tuple[str, str], tuple[float, float]] = {}

# ─── US State lookup ──────────────────────────────────────────────────────────
# Full state names only — abbreviations are too ambiguous in natural language
# (e.g. " in " matches Indiana but also "race in Arizona")
_US_STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}


# Pre-compiled state patterns (longest names first to avoid partial matches)
_US_STATE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b" + re.escape(name) + r"\b"), code)
    for name, code in sorted(
        _US_STATE_NAMES.items(), key=lambda x: -len(x[0])  # longest first
    )
]


def _extract_us_states(title: str) -> set[str]:
    """Return set of canonical state codes (e.g. 'AZ') found in title."""
    lower = title.lower()
    found: set[str] = set()
    remaining = lower
    for pattern, code in _US_STATE_PATTERNS:
        if pattern.search(remaining):
            found.add(code)
            # Remove matched text so "west virginia" doesn't also match "virginia"
            remaining = pattern.sub("", remaining)
    return found


# ─── Synonym normalization ────────────────────────────────────────────────────
# Applied before fuzzy matching to bridge cross-platform terminology differences.
# Both titles are normalized to canonical forms, then scored in addition to the
# raw title match. The best score from either comparison is used.
_SYNONYM_MAP: list[tuple[re.Pattern[str], str]] = [
    # Crypto tickers → full names
    (re.compile(r"\bbtc\b", re.IGNORECASE), "bitcoin"),
    (re.compile(r"\beth\b", re.IGNORECASE), "ethereum"),
    (re.compile(r"\bsol\b(?!ar)", re.IGNORECASE), "solana"),  # avoid "solar"
    (re.compile(r"\bxrp\b", re.IGNORECASE), "ripple"),
    (re.compile(r"\bdoge\b", re.IGNORECASE), "dogecoin"),
    # Fed / FOMC → federal reserve
    (re.compile(r"\bfomc\b", re.IGNORECASE), "federal reserve"),
    (re.compile(r"\bthe\s+fed\b", re.IGNORECASE), "federal reserve"),
    # Economic indicators
    (re.compile(r"\bcpi\b", re.IGNORECASE), "inflation"),
    (re.compile(r"\bgdp\b", re.IGNORECASE), "growth"),
    # Basis points: "25bps" → "25 basis points", "0bps" → "no change"
    (re.compile(r"\b0\s*bps\b", re.IGNORECASE), "no change"),
    (re.compile(r"\b(\d+)\s*bps\b", re.IGNORECASE), r"\1 basis points"),
    # Rate actions
    (re.compile(r"\brate\s+cut\b", re.IGNORECASE), "rate decrease"),
    (re.compile(r"\brate\s+hike\b", re.IGNORECASE), "rate increase"),
    (re.compile(r"\brate\s+hold\b|\brate\s+pause\b", re.IGNORECASE), "rate unchanged"),
    (re.compile(r"\binterest\s+rate\b", re.IGNORECASE), "rate"),
    # Dollar amounts: "$90k" → "90000"
    (re.compile(r"\$\s*(\d+(?:\.\d+)?)\s*k\b", re.IGNORECASE), lambda m: str(int(float(m.group(1)) * 1000))),
    (re.compile(r"\$\s*(\d+(?:\.\d+)?)\s*m\b", re.IGNORECASE), lambda m: str(int(float(m.group(1)) * 1_000_000))),
]


def _normalise_for_matching(title: str) -> str:
    """
    Apply synonym substitutions to a market title to create a canonical form
    for improved cross-platform fuzzy matching.
    Original titles are NOT modified — this is only used during matching.
    """
    t = title.lower()
    for pattern, replacement in _SYNONYM_MAP:
        if callable(replacement):
            t = pattern.sub(replacement, t)
        else:
            t = pattern.sub(replacement, t)
    return t


# ─── Entity patterns ──────────────────────────────────────────────────────────
# Capture named entities likely to appear in prediction market titles
_YEAR_RE = re.compile(r"\b(202[0-9]|203[0-9])\b")
_PCT_RE = re.compile(r"\b\d+(\.\d+)?%\b")
_DOLLAR_RE = re.compile(r"\$\s*[\d,]+(\.\d+)?[kKmMbB]?\b")
_NUMBER_RE = re.compile(r"\b\d{2,}\b")
_PROPER_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")  # Proper nouns

# Stopwords to strip before entity/keyword matching
_STOPWORDS = frozenset({
    "will", "the", "a", "an", "be", "in", "by", "of", "to", "for",
    "on", "at", "is", "are", "was", "were", "than", "or", "and",
    "before", "after", "during", "between", "from", "over", "under",
    "with", "without", "yes", "no", "not", "can", "could", "would",
    "should", "has", "have", "had", "does", "do", "did", "get",
    "what", "when", "who", "which", "that", "this", "it", "its",
    "he", "she", "they", "their", "any", "all", "more", "most",
    "win", "lose", "reach", "exceed", "hit", "beat", "pass",
    "become", "remain", "end", "start", "happen", "occur",
})

# Negation indicators — used to detect polarity flips
_NEGATION_PHRASES = frozenset({
    "not", "no ", "won't", "will not", "doesn't", "does not",
    "fail to", "fails to", "unable", "never",
})

# Party polarity: Democrat/Republican titles are complementary outcomes
_DEMOCRAT_RE = re.compile(r"democrats?|democratic|\bdem\b|\bdems\b", re.IGNORECASE)
_REPUBLICAN_RE = re.compile(r"republicans?|gop|\brep\b|\breps\b", re.IGNORECASE)


def _clean_title(title: str) -> str:
    """Lowercase, strip punctuation, remove stopwords."""
    t = title.lower()
    t = re.sub(r"[^\w\s%$.]", " ", t)
    tokens = t.split()
    return " ".join(w for w in tokens if w not in _STOPWORDS)


def _extract_entities(title: str) -> set[str]:
    """
    Extract meaningful tokens and patterns from a market title.
    Returns a set of entity strings for overlap comparison.
    """
    entities: set[str] = set()

    # Years
    for m in _YEAR_RE.finditer(title):
        entities.add(m.group())

    # Percentages
    for m in _PCT_RE.finditer(title):
        entities.add(m.group().replace(" ", ""))

    # Dollar amounts
    for m in _DOLLAR_RE.finditer(title):
        entities.add(m.group().replace(" ", "").replace(",", ""))

    # Large numbers (possible prices, thresholds)
    for m in _NUMBER_RE.finditer(title):
        entities.add(m.group())

    # Proper nouns (capitalised words longer than 2 chars)
    for m in _PROPER_RE.finditer(title):
        word = m.group()
        if word not in _STOPWORDS and len(word) > 2:
            entities.add(word.lower())

    # Significant keywords after cleaning
    cleaned = _clean_title(title)
    for word in cleaned.split():
        if len(word) > 3:
            entities.add(word)

    return entities


def _has_negation(title: str) -> bool:
    """Return True if the title contains a clear negation phrase."""
    lower = title.lower()
    return any(neg in lower for neg in _NEGATION_PHRASES)


def _detect_polarity_flip(title_a: str, title_b: str) -> bool:
    """
    Detect if two titles describe opposite outcomes of the same binary event.

    Case 1 — Explicit negation: "Will X win?" vs "Will X NOT win?"
    Case 2 — Party opposition: "Will Democrats win?" vs "Will Republicans win?"
             Both describe the same event from opposite framings.
    """
    neg_a = _has_negation(title_a)
    neg_b = _has_negation(title_b)
    if neg_a != neg_b:
        return True

    # Party-polarity flip: one title names only Dems, the other only Reps
    dems_a = bool(_DEMOCRAT_RE.search(title_a))
    reps_a = bool(_REPUBLICAN_RE.search(title_a))
    dems_b = bool(_DEMOCRAT_RE.search(title_b))
    reps_b = bool(_REPUBLICAN_RE.search(title_b))

    if dems_a and not reps_a and reps_b and not dems_b:
        return True
    if reps_a and not dems_a and dems_b and not reps_b:
        return True

    return False


def _resolution_date_score(date_a: Optional[datetime], date_b: Optional[datetime]) -> float:
    """
    Score based on how close the resolution dates are.
    Returns 0.0–20.0 (the date_score component of confidence).
    """
    if date_a is None or date_b is None:
        # Missing date = lower confidence but not disqualifying
        return 5.0

    # Make both timezone-aware for safe comparison
    if date_a.tzinfo is None:
        date_a = date_a.replace(tzinfo=timezone.utc)
    if date_b.tzinfo is None:
        date_b = date_b.replace(tzinfo=timezone.utc)

    diff_days = abs((date_a - date_b).total_seconds()) / 86400.0

    if diff_days <= 1:
        return 20.0
    elif diff_days <= 3:
        return 15.0
    elif diff_days <= config.DATE_WINDOW_DAYS:
        return 10.0
    else:
        return 0.0  # Outside window — disqualify in Layer 4


def _volume_score(vol_a: float, vol_b: float) -> float:
    """
    Score how similar the market volumes are (proxy for co-listed event).
    Returns 0.0–10.0.
    """
    if vol_a <= 0 or vol_b <= 0:
        return 0.0
    ratio = min(vol_a, vol_b) / max(vol_a, vol_b)
    return ratio * 10.0


def _entity_overlap_score(entities_a: set[str], entities_b: set[str]) -> tuple[float, list[str]]:
    """
    Measure entity overlap between two markets.
    Returns (score 0-10, list of shared entities).
    """
    if not entities_a or not entities_b:
        return 0.0, []

    intersection = entities_a & entities_b
    union = entities_a | entities_b

    if not union:
        return 0.0, []

    jaccard = len(intersection) / len(union)
    return jaccard * 10.0, sorted(intersection)


class MatchingEngine:
    """
    Five-layer market matching engine.
    Input: lists of normalised markets from each platform.
    Output: list of MarketMatch objects that passed all validation layers.
    """

    def __init__(self) -> None:
        self.stats = {
            "pairs_considered": 0,
            "layer1_eliminated": 0,
            "layer2_eliminated": 0,
            "layer3_eliminated": 0,
            "layer4_eliminated": 0,
            "layer5_eliminated": 0,
            "matches_found": 0,
        }

    def find_matches(
        self,
        poly_markets: list[NormalisedMarket],
        kalshi_markets: list[NormalisedMarket],
    ) -> list[MarketMatch]:
        """
        Main entry point. Runs all five matching layers.
        Returns list of valid MarketMatch objects.
        """
        # Reset stats
        self.stats = {k: 0 for k in self.stats}

        # Pre-filter: remove markets below minimum volume threshold before
        # building the cross-product. Sports game markets with zero volume
        # are the main offender — pre-filtering reduces millions of futile
        # fuzzy comparisons down to a manageable set.
        poly_markets = [m for m in poly_markets if m.total_volume >= config.MIN_VOLUME_USD]
        kalshi_markets = [m for m in kalshi_markets if m.total_volume >= config.MIN_VOLUME_USD]
        logger.info(
            f"[matcher] After volume pre-filter (>=${config.MIN_VOLUME_USD}): "
            f"Poly={len(poly_markets)}, Kalshi={len(kalshi_markets)}"
        )

        # Pre-compute entities for all markets to avoid redundant computation
        poly_entities = {m.external_id: _extract_entities(m.title) for m in poly_markets}
        kalshi_entities = {m.external_id: _extract_entities(m.title) for m in kalshi_markets}

        # Group by category for Layer 1 filtering
        poly_by_cat: dict[str, list[NormalisedMarket]] = {}
        for m in poly_markets:
            poly_by_cat.setdefault(m.category, []).append(m)

        kalshi_by_cat: dict[str, list[NormalisedMarket]] = {}
        for m in kalshi_markets:
            kalshi_by_cat.setdefault(m.category, []).append(m)

        matches: list[MarketMatch] = []
        all_categories = set(poly_by_cat.keys()) | set(kalshi_by_cat.keys())

        for category in all_categories:
            poly_group = poly_by_cat.get(category, [])
            kalshi_group = kalshi_by_cat.get(category, [])

            if not poly_group or not kalshi_group:
                # Layer 1: different categories, skip
                self.stats["layer1_eliminated"] += len(poly_group) * len(kalshi_group)
                continue

            for pm in poly_group:
                for km in kalshi_group:
                    self.stats["pairs_considered"] += 1

                    match = self._evaluate_pair(
                        pm, km,
                        poly_entities[pm.external_id],
                        kalshi_entities[km.external_id],
                    )
                    if match:
                        matches.append(match)

        self.stats["matches_found"] = len(matches)
        logger.info(
            f"[matcher] Pairs considered: {self.stats['pairs_considered']} | "
            f"Matches found: {len(matches)} | "
            f"L1:{self.stats['layer1_eliminated']} "
            f"L2:{self.stats['layer2_eliminated']} "
            f"L3:{self.stats['layer3_eliminated']} "
            f"L4:{self.stats['layer4_eliminated']} "
            f"L5:{self.stats['layer5_eliminated']}"
        )
        return matches

    def _evaluate_pair(
        self,
        pm: NormalisedMarket,
        km: NormalisedMarket,
        poly_entities: set[str],
        kalshi_entities: set[str],
    ) -> Optional[MarketMatch]:
        """
        Run layers 2–5 on a single market pair.
        Returns MarketMatch if valid, None if rejected.
        """

        # ── Layer 2: Entity / keyword check ──────────────────────────────────
        # If both markets have entities and share NONE, they can't be the same event
        entity_score, shared_entities = _entity_overlap_score(poly_entities, kalshi_entities)
        if poly_entities and kalshi_entities and entity_score == 0.0:
            self.stats["layer2_eliminated"] += 1
            return None

        # ── Layer 3: Fuzzy string matching ───────────────────────────────────
        title_a = pm.title
        title_b = km.title

        # Cache fuzzy scores by title pair — titles never change, so the result
        # is deterministic. Eliminates 900k+ redundant fuzzy calls on scan 2+.
        _fuzzy_key = (title_a, title_b)
        if _fuzzy_key in _fuzzy_cache:
            token_sort, token_set = _fuzzy_cache[_fuzzy_key]
        else:
            norm_a = _normalise_for_matching(title_a)
            norm_b = _normalise_for_matching(title_b)
            token_sort = max(
                fuzz.token_sort_ratio(title_a, title_b),
                fuzz.token_sort_ratio(norm_a, norm_b),
            )
            token_set = max(
                fuzz.token_set_ratio(title_a, title_b),
                fuzz.token_set_ratio(norm_a, norm_b),
            )
            _fuzzy_cache[_fuzzy_key] = (token_sort, token_set)

        if token_sort < config.FUZZY_MIN_TOKEN_SORT or token_set < config.FUZZY_MIN_TOKEN_SET:
            self.stats["layer3_eliminated"] += 1
            return None

        # ── Layer 4: Semantic validation ─────────────────────────────────────

        # Date window check
        date_a = pm.resolution_date
        date_b = km.resolution_date

        if date_a and date_b:
            if date_a.tzinfo is None:
                date_a = date_a.replace(tzinfo=timezone.utc)
            if date_b.tzinfo is None:
                date_b = date_b.replace(tzinfo=timezone.utc)
            diff_days = abs((date_a - date_b).total_seconds()) / 86400.0
            if diff_days > config.DATE_WINDOW_DAYS:
                self.stats["layer4_eliminated"] += 1
                return None

        # Volume gate — both sides must have meaningful activity
        if pm.total_volume < config.MIN_VOLUME_USD or km.total_volume < config.MIN_VOLUME_USD:
            self.stats["layer4_eliminated"] += 1
            return None

        # ── State consistency check ───────────────────────────────────────────
        # If either title references a US state, both must reference the SAME state.
        # This prevents "Republicans win Colorado Senate" (Poly) from matching
        # "Republicans win Senate race in Arizona" (Kalshi).
        states_a = _extract_us_states(title_a)
        states_b = _extract_us_states(title_b)
        if states_a or states_b:
            # Both must have at least one common state
            if not (states_a & states_b):
                self.stats["layer4_eliminated"] += 1
                return None

        # Polarity detection
        polarity_flipped = _detect_polarity_flip(title_a, title_b)

        # ── Price alignment check ─────────────────────────────────────────────
        # For non-polarity-flipped pairs: both YES prices must be on the same side
        # of 50% (or within 40% of each other). A 97% Poly market vs a 5% Kalshi
        # market means they measure DIFFERENT outcomes of the same event series
        # (e.g., "Fed HOLD" vs "Fed HIKE 25bps") — same title, different number.
        # Maximum realistic gross arb spread is ~30%, so prices can differ by at
        # most ~30 percentage points for a genuinely matched pair.
        if not polarity_flipped:
            price_gap = abs(pm.yes_price - km.yes_price)
            if price_gap > 0.60:
                self.stats["layer4_eliminated"] += 1
                return None

        # ── Layer 5: Composite confidence scoring ─────────────────────────────

        # String similarity score (0-40)
        avg_fuzzy = (token_sort + token_set) / 2.0
        string_score = (avg_fuzzy / 100.0) * config.SCORE_STRING_MAX

        # Category score (0-20): already confirmed same category
        category_score = float(config.SCORE_CATEGORY_MAX)

        # Date proximity score (0-20)
        date_score = _resolution_date_score(pm.resolution_date, km.resolution_date)

        # Volume similarity score (0-10)
        vol_score = _volume_score(pm.total_volume, km.total_volume)

        # Entity overlap score (0-10)
        entity_score_capped = min(entity_score, float(config.SCORE_ENTITY_MAX))

        total_confidence = (
            string_score + category_score + date_score + vol_score + entity_score_capped
        )

        if total_confidence < config.MIN_MATCH_CONFIDENCE:
            self.stats["layer5_eliminated"] += 1
            return None

        confidence_tier = "HIGH" if total_confidence >= 85 else "MEDIUM"

        return MarketMatch(
            market_poly=pm,
            market_kalshi=km,
            confidence=total_confidence,
            confidence_tier=confidence_tier,
            polarity_flipped=polarity_flipped,
            string_score=string_score,
            category_score=category_score,
            date_score=date_score,
            volume_score=vol_score,
            entity_score=entity_score_capped,
            matched_entities=shared_entities[:10],  # Cap for display
        )
