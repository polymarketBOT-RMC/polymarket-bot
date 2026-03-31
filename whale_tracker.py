"""
Polymarket Whale Tracker
========================
Tracks the top-performing Polymarket wallets and surfaces trades they are
making right now as potential copy signals.

How it works:
1. Maintains a watchlist of known high-ROI whale wallets (seeded manually,
   can be extended via the WHALE_WATCHLIST env var).
2. Every cycle, fetches recent trades for each wallet via the Polymarket
   data API (gamma-api.polymarket.com).
3. Filters to trades made in the last WHALE_LOOKBACK_HOURS hours.
4. Deduplicates — only alerts once per whale/market/direction combo.
5. Returns trade signals in the same format as the main Myriad pipeline
   so they flow into log_signal() and get paper traded automatically.

Why it works:
  Polymarket is on-chain — every trade is public. Top wallets have
  demonstrated consistent alpha over hundreds of markets. Following them
  is not guaranteed to work, but it's a real signal source that doesn't
  depend on AI research or cross-market pricing gaps.

Confidence weighting:
  - Whale historical ROI > 80%  → HIGH confidence
  - Whale historical ROI 60-80% → MEDIUM confidence
  - Whale historical ROI < 60%  → LOW confidence (still logged, not alerted)

Data source: gamma-api.polymarket.com (public, no auth required)
"""
import os
import time
import logging
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

# How far back to look for whale trades (hours)
WHALE_LOOKBACK_HOURS = int(os.environ.get("WHALE_LOOKBACK_HOURS", "4"))

# Minimum trade size in USD to count as a whale signal
WHALE_MIN_TRADE_USD = float(os.environ.get("WHALE_MIN_TRADE_USD", "500"))

# Minimum whale historical win rate to include in tracking
WHALE_MIN_WIN_RATE = float(os.environ.get("WHALE_MIN_WIN_RATE", "0.60"))

# Seed wallet list — known high-ROI Polymarket addresses.
# Add more via WHALE_WALLETS env var (comma-separated addresses).
_DEFAULT_WHALES = [
    "0x8fe9f1f76a2804ee9c5f1c0c9b0e8d8f1e5b0d34",  # placeholder — replace with real addresses
    "0x3a4e2f9c8b7d6e1f0a5c2b9d8e7f6c5a4b3d2e1f",  # placeholder
]

# In-memory dedup: set of (wallet, market_id, direction) already seen this session
_whale_seen: set = set()
# Cache: wallet address -> {trades, fetched_at}
_whale_cache: dict = {}
_WHALE_CACHE_TTL = 3600  # 1 hour


def _get_whale_wallets() -> list:
    """Return combined seed list + any wallets from env var."""
    env_wallets = [
        w.strip().lower() for w in
        os.environ.get("WHALE_WALLETS", "").split(",")
        if w.strip()
    ]
    return list(set(_DEFAULT_WHALES + env_wallets))


def _fetch_wallet_trades(address: str) -> list:
    """
    Fetch recent trades for a wallet from Polymarket's data API.
    Returns list of trade dicts or [] on failure.
    Cached for 1 hour.
    """
    now = time.time()
    cached = _whale_cache.get(address)
    if cached and now - cached["fetched_at"] < _WHALE_CACHE_TTL:
        return cached["trades"]

    try:
        # Polymarket trades endpoint
        url = f"{GAMMA_API}/trades"
        params = {
            "maker": address,
            "limit": 50,
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        trades = r.json() if isinstance(r.json(), list) else r.json().get("data", [])
        _whale_cache[address] = {"trades": trades, "fetched_at": now}
        return trades
    except Exception as e:
        logger.debug(f"Whale trade fetch failed for {address[:12]}…: {e}")
        return []


def _fetch_wallet_stats(address: str) -> dict:
    """
    Fetch profit/ROI stats for a wallet.
    Returns dict with win_rate, total_pnl, n_trades or empty dict.
    """
    try:
        url = f"{GAMMA_API}/profiles/{address}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        return {
            "win_rate":   float(data.get("winRate", 0)),
            "total_pnl":  float(data.get("pnl", 0)),
            "n_trades":   int(data.get("tradesCount", 0)),
        }
    except Exception:
        return {}


def _fetch_market_details(condition_id: str) -> dict:
    """Fetch market title and current price for a condition ID."""
    try:
        url = f"{GAMMA_API}/markets"
        r = requests.get(url, params={"conditionId": condition_id}, timeout=10)
        r.raise_for_status()
        markets = r.json()
        if isinstance(markets, list) and markets:
            m = markets[0]
            return {
                "question":     m.get("question", "Unknown market"),
                "market_id":    m.get("id", ""),
                "condition_id": condition_id,
                "yes_price":    float(m.get("outcomePrices", ["0.5"])[0]),
                "volume":       float(m.get("volumeNum", 0)),
                "end_date":     m.get("endDate", ""),
            }
    except Exception as e:
        logger.debug(f"Market detail fetch failed for {condition_id}: {e}")
    return {}


def get_whale_signals() -> list:
    """
    Main entry point. Call this each Myriad cycle.
    Returns list of signal dicts ready for log_signal():
    {
        identifier, direction, price, true_prob,
        whale_address, whale_win_rate, whale_pnl,
        trade_size_usd, confidence, reason
    }
    """
    signals = []
    wallets = _get_whale_wallets()
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=WHALE_LOOKBACK_HOURS)

    for address in wallets:
        try:
            trades = _fetch_wallet_trades(address)
            if not trades:
                continue

            # Get wallet stats for confidence weighting
            stats    = _fetch_wallet_stats(address)
            win_rate = stats.get("win_rate", 0)

            if win_rate > 0 and win_rate < WHALE_MIN_WIN_RATE:
                logger.debug(
                    f"Whale {address[:12]}… win rate {win_rate:.0%} below threshold — skipping"
                )
                continue

            for trade in trades:
                try:
                    # Parse trade timestamp
                    ts_str = trade.get("timestamp") or trade.get("createdAt", "")
                    if not ts_str:
                        continue
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts < cutoff:
                        continue  # too old

                    condition_id = trade.get("conditionId", "")
                    outcome      = trade.get("outcome", "Yes")   # Yes / No
                    side         = trade.get("side", "buy")      # buy / sell
                    size_usd     = float(trade.get("amount", 0) or 0)
                    avg_price    = float(trade.get("price", 0) or 0)

                    if size_usd < WHALE_MIN_TRADE_USD:
                        continue  # too small to be meaningful

                    if side.lower() != "buy":
                        continue  # only follow buys, not exits

                    # Dedup
                    dedup_key = (address, condition_id, outcome)
                    if dedup_key in _whale_seen:
                        continue
                    _whale_seen.add(dedup_key)

                    # Get market details
                    market = _fetch_market_details(condition_id)
                    if not market:
                        continue

                    question = market.get("question", "Unknown")
                    price    = avg_price if avg_price > 0 else market.get("yes_price", 0.5)

                    # Direction string
                    direction = f"BUY {outcome.upper()}"

                    # Confidence tier
                    if win_rate >= 0.80:
                        confidence = "HIGH"
                    elif win_rate >= 0.60:
                        confidence = "MEDIUM"
                    else:
                        confidence = "LOW"

                    # Use whale's implied probability as our true_prob estimate
                    # If whale is buying YES at 0.35, they think it's worth more
                    # We use price + edge premium as conservative estimate
                    true_prob = min(95, round(price * 100 + 10, 1))

                    reason = (
                        f"Whale copy: {address[:8]}… bought {outcome} "
                        f"${size_usd:.0f} @ {price:.2f} | "
                        f"WR={win_rate:.0%} | "
                        f"Confidence={confidence}"
                    )

                    signals.append({
                        "identifier":      question,
                        "direction":       direction,
                        "price":           price,
                        "true_prob":       true_prob,
                        "market_id":       market.get("market_id", ""),
                        "condition_id":    condition_id,
                        "whale_address":   address,
                        "whale_win_rate":  win_rate,
                        "whale_pnl":       stats.get("total_pnl", 0),
                        "trade_size_usd":  size_usd,
                        "confidence":      confidence,
                        "reason":          reason,
                        "source":          "whale_copy",
                        "volume":          market.get("volume", 0),
                    })

                    logger.info(
                        f"🐋 Whale signal: {question[:50]} | "
                        f"{direction} @ {price:.2f} | "
                        f"${size_usd:.0f} | {confidence} confidence"
                    )

                except Exception as e:
                    logger.debug(f"Whale trade parse error: {e}")
                    continue

        except Exception as e:
            logger.debug(f"Whale wallet error {address[:12]}…: {e}")
            continue

    if signals:
        logger.info(f"Whale tracker: {len(signals)} new signal(s) this cycle")
    else:
        logger.debug("Whale tracker: no new signals this cycle")

    return signals


def reset_whale_dedup():
    """Call daily to allow re-alerting on persistent whale positions."""
    global _whale_seen
    _whale_seen = set()
    logger.info("Whale dedup cache reset")


def add_whale_wallet(address: str):
    """Dynamically add a wallet to track (persists for session only)."""
    address = address.lower().strip()
    if address not in _DEFAULT_WHALES:
        _DEFAULT_WHALES.append(address)
        logger.info(f"Whale wallet added: {address[:12]}…")
