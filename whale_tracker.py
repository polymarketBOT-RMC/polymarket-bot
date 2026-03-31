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

# Seed wallet list — verified high-ROI Polymarket addresses (Polygon network).
# Sources: polymarket.com/leaderboard, predicts.guru, polytrackhq.app
# Add more via WHALE_WALLETS env var (comma-separated addresses).
_DEFAULT_WHALES = [
    # ── All-time profit leaderboard ──────────────────────────────────────
    "0x56687bf447db6ffa42ffe2204a05edaa20f55839",  # Theo4       — $22M PnL, election specialist
    "0x1f2dd6d473f3e824cd2f8a89d9c69fb96f6ad0cf",  # Fredi9999   — $16.6M PnL, 45 markets
    "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee",  # kch123      — 61% WR, 74.9% ROI, 2932 trades
    "0x78b9ac44a6d7d7a076c14e0ad518b301b63c6b76",  # Len9311238  — $8.7M PnL
    "0xd235973291b2b75ff4070e9c0b01728c520b0f29",  # zxgngl      — $7.8M PnL
    "0x863134d00841b2e200492805a01e1e2f5defaa53",  # RepTrump    — $7.5M PnL
    "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",  # RN1         — $6.5M PnL, high volume
    "0x8119010a6e589062aa03583bb3f39ca632d9f887",  # PrincessCaro — $6.1M PnL
    "0xe9ad918c7678cd38b12603a762e638a5d1ee7091",  # walletmobile — $5.9M PnL
    "0x94f199fb7789f1aef7fff6b758d6b375100f4c7a",  # KeyTransporter — $5.7M PnL

    # ── Monthly leaderboard top performers ──────────────────────────────
    "0x492442eab586f242b53bda933fd5de859c8a3782",  # #1 monthly  — $4.2M monthly PnL
    "0x02227b8f5a9636e895607edd3185ed6ee5598ff7",  # HorizonSplendidView — $4M monthly
    "0xc2e7800b5af46e6093872b177b7a5e7f0563be51",  # beachboy4   — $3.76M monthly
    "0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2",  # reachingthesky — $3.74M monthly
    "0x019782cab5d844f02bafb71f512758be78579f3c",  # majorexploiter — $2.4M monthly

    # ── High win-rate specialist ─────────────────────────────────────────
    "0x4e25605fd905e9972efc0f4d8814530c655cd7a7",  # polyburg #7 — 79.8% WR
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
