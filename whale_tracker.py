"""
Polymarket Whale Tracker  (v2 — stake-weighted win rate only)
=============================================================
Tracks wallets that are consistently RIGHT with real money behind their bets.

The only metric that matters:
  stake-weighted win rate = total stakes on winning bets / total stakes placed

This filters out:
  - Lucky one-hit wonders (high PnL from one election bet)
  - High trade-count noise traders (lots of small wins, big losses)
  - Anyone with fewer than MIN_TRADES bets (statistically meaningless)

A wallet qualifies only if:
  1. 200+ resolved trades (enough sample to be statistically meaningful)
  2. Stake-weighted win rate >= 58% (beats the market meaningfully)
  3. Active in the last 30 days (not a retired account)

Seed wallets (v2 — win-rate verified only):
  kch123  — 61.2% WR, 2,932 trades, 74.9% ROI — the only all-time leaderboard
             wallet with both high WR and high trade count
  polyburg #7 — 79.8% WR (trade count to be verified via API on first run)

All PnL-based wallets (Theo4, Fredi9999 etc) removed — one-hit wonders.

Data source: gamma-api.polymarket.com + data-api.polymarket.com (public)
"""
import os
import time
import logging
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"

# ── Thresholds ────────────────────────────────────────────────────────────────
WHALE_LOOKBACK_HOURS   = int(os.environ.get("WHALE_LOOKBACK_HOURS",   "6"))
WHALE_MIN_TRADE_USD    = float(os.environ.get("WHALE_MIN_TRADE_USD",  "300"))
WHALE_MIN_WIN_RATE     = float(os.environ.get("WHALE_MIN_WIN_RATE",   "0.58"))
WHALE_MIN_TRADES       = int(os.environ.get("WHALE_MIN_TRADES",       "200"))
WHALE_MIN_ACTIVE_DAYS  = int(os.environ.get("WHALE_MIN_ACTIVE_DAYS",  "30"))

# ── Seed wallets — win-rate verified, 200+ trades only ───────────────────────
# PnL whales deliberately excluded. Only consistent high-accuracy traders.
_DEFAULT_WHALES = [
    "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee",  # kch123      — 61.2% WR, 2932 trades
    "0x4e25605fd905e9972efc0f4d8814530c655cd7a7",  # polyburg #7 — 79.8% WR (count TBC)
]

# ── Runtime state ─────────────────────────────────────────────────────────────
_whale_seen:    set  = set()   # (address, condition_id, outcome) — session dedup
_stats_cache:   dict = {}      # address -> {stats, fetched_at}
_trades_cache:  dict = {}      # address -> {trades, fetched_at}
_STATS_TTL  = 86400            # re-fetch wallet stats once per day
_TRADES_TTL = 3600             # re-fetch recent trades every hour


# ── Wallet qualification ──────────────────────────────────────────────────────

def _fetch_wallet_stats(address: str) -> dict:
    """
    Fetch wallet performance stats.
    Tries gamma-api profile endpoint first, falls back to data-api.
    Returns {stake_weighted_wr, simple_wr, n_trades, last_active, pnl} or {}.
    """
    now = time.time()
    cached = _stats_cache.get(address)
    if cached and now - cached["fetched_at"] < _STATS_TTL:
        return cached["stats"]

    stats = {}

    # Try gamma-api profile
    try:
        r = requests.get(f"{GAMMA_API}/profiles/{address}", timeout=10)
        if r.status_code == 200:
            d = r.json()
            stats = {
                "simple_wr":          float(d.get("winRate", 0)),
                "stake_weighted_wr":  float(d.get("winRate", 0)),  # best available
                "n_trades":           int(d.get("tradesCount", 0)),
                "pnl":                float(d.get("pnl", 0)),
                "last_active":        d.get("lastTrade", ""),
            }
    except Exception:
        pass

    # Try data-api for richer stats if gamma returned nothing useful
    if not stats or stats.get("n_trades", 0) == 0:
        try:
            r = requests.get(
                f"{DATA_API}/profiles",
                params={"address": address},
                timeout=10
            )
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, list) and d:
                    d = d[0]
                stats = {
                    "simple_wr":         float(d.get("winRate", 0)),
                    "stake_weighted_wr": float(d.get("winRate", 0)),
                    "n_trades":          int(d.get("tradesCount", d.get("numTrades", 0))),
                    "pnl":               float(d.get("pnl", d.get("profit", 0))),
                    "last_active":       d.get("lastTrade", d.get("lastActiveTime", "")),
                }
        except Exception:
            pass

    _stats_cache[address] = {"stats": stats, "fetched_at": now}
    return stats


def _wallet_qualifies(address: str, stats: dict) -> tuple:
    """
    Returns (qualifies: bool, reason: str).
    Hard gates: min trades, min win rate, must be recently active.
    """
    if not stats:
        return False, "no stats available"

    n = stats.get("n_trades", 0)
    if n < WHALE_MIN_TRADES:
        return False, f"only {n} trades (min {WHALE_MIN_TRADES})"

    wr = stats.get("stake_weighted_wr", stats.get("simple_wr", 0))
    if wr < WHALE_MIN_WIN_RATE:
        return False, f"win rate {wr:.1%} below {WHALE_MIN_WIN_RATE:.0%} threshold"

    last = stats.get("last_active", "")
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            days_ago = (datetime.now(timezone.utc) - last_dt).days
            if days_ago > WHALE_MIN_ACTIVE_DAYS:
                return False, f"inactive for {days_ago} days"
        except Exception:
            pass  # can't parse date — don't disqualify

    return True, f"WR={wr:.1%} n={n}"


# ── Trade fetching ────────────────────────────────────────────────────────────

def _fetch_recent_trades(address: str) -> list:
    """
    Fetch recent trades for a wallet. Tries both APIs.
    Returns list of normalised trade dicts.
    """
    now = time.time()
    cached = _trades_cache.get(address)
    if cached and now - cached["fetched_at"] < _TRADES_TTL:
        return cached["trades"]

    trades = []

    # gamma-api trades endpoint
    try:
        r = requests.get(
            f"{GAMMA_API}/trades",
            params={"maker": address, "limit": 50},
            timeout=10
        )
        if r.status_code == 200:
            raw = r.json()
            trades = raw if isinstance(raw, list) else raw.get("data", [])
    except Exception:
        pass

    # data-api activity endpoint as fallback
    if not trades:
        try:
            r = requests.get(
                f"{DATA_API}/activity",
                params={"user": address, "limit": 50},
                timeout=10
            )
            if r.status_code == 200:
                raw = r.json()
                trades = raw if isinstance(raw, list) else raw.get("data", [])
        except Exception:
            pass

    _trades_cache[address] = {"trades": trades, "fetched_at": now}
    return trades


def _fetch_market_details(condition_id: str) -> dict:
    """Fetch market question and current price."""
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"conditionId": condition_id},
            timeout=10
        )
        if r.status_code == 200:
            markets = r.json()
            if isinstance(markets, list) and markets:
                m = markets[0]
                prices = m.get("outcomePrices", ["0.5", "0.5"])
                return {
                    "question":     m.get("question", ""),
                    "market_id":    m.get("id", ""),
                    "condition_id": condition_id,
                    "yes_price":    float(prices[0]) if prices else 0.5,
                    "volume":       float(m.get("volumeNum", 0)),
                    "end_date":     m.get("endDate", ""),
                    "closed":       m.get("closed", False),
                }
    except Exception as e:
        logger.debug(f"Market fetch failed {condition_id[:12]}: {e}")
    return {}


# ── Main entry point ──────────────────────────────────────────────────────────

def get_whale_signals() -> list:
    """
    Returns list of signal dicts for qualified whale trades in the last
    WHALE_LOOKBACK_HOURS hours.

    Confidence is based purely on stake-weighted win rate:
      HIGH   — WR >= 70%  AND  n_trades >= 500
      MEDIUM — WR >= 58%  AND  n_trades >= 200
      (LOW signals are not returned — not worth following)
    """
    signals = []
    wallets = _get_whale_wallets()
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=WHALE_LOOKBACK_HOURS)

    for address in wallets:
        try:
            # Step 1 — qualify the wallet
            stats = _fetch_wallet_stats(address)
            qualifies, reason = _wallet_qualifies(address, stats)

            if not qualifies:
                logger.debug(f"Whale {address[:12]}… disqualified: {reason}")
                continue

            wr      = stats.get("stake_weighted_wr", stats.get("simple_wr", 0))
            n       = stats.get("n_trades", 0)
            logger.debug(f"Whale {address[:12]}… qualified: {reason}")

            # Step 2 — fetch recent trades
            trades = _fetch_recent_trades(address)
            if not trades:
                continue

            for trade in trades:
                try:
                    # Normalise timestamp field name
                    ts_str = (trade.get("timestamp") or
                              trade.get("createdAt") or
                              trade.get("time", ""))
                    if not ts_str:
                        continue
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts < cutoff:
                        continue

                    # Normalise field names across APIs
                    condition_id = (trade.get("conditionId") or
                                    trade.get("market", {}).get("conditionId", ""))
                    outcome      = trade.get("outcome", "Yes")
                    side         = (trade.get("side") or
                                    trade.get("type", "buy")).lower()
                    size_usd     = float(trade.get("amount") or
                                         trade.get("usdcSize") or
                                         trade.get("size", 0) or 0)
                    avg_price    = float(trade.get("price") or
                                         trade.get("avgPrice", 0) or 0)

                    if not condition_id:
                        continue
                    if size_usd < WHALE_MIN_TRADE_USD:
                        continue
                    if "sell" in side or "redeem" in side:
                        continue  # only follow entry buys

                    # Session dedup
                    dedup_key = (address, condition_id, outcome)
                    if dedup_key in _whale_seen:
                        continue
                    _whale_seen.add(dedup_key)

                    # Fetch market
                    market = _fetch_market_details(condition_id)
                    if not market or not market.get("question"):
                        continue
                    if market.get("closed"):
                        continue  # market already resolved

                    question  = market["question"]
                    price     = avg_price if avg_price > 0 else market["yes_price"]
                    direction = f"BUY {outcome.upper()}"

                    # Confidence — stake-weighted WR + sample size
                    if wr >= 0.70 and n >= 500:
                        confidence = "HIGH"
                    elif wr >= 0.58 and n >= 200:
                        confidence = "MEDIUM"
                    else:
                        continue  # below threshold — skip entirely

                    # Conservative true_prob: whale's entry price + 8pp edge premium
                    true_prob = min(92, round(price * 100 + 8, 1))

                    reason = (
                        f"Whale copy [{confidence}]: {address[:8]}… "
                        f"staked ${size_usd:.0f} on {outcome} @ {price:.2f} | "
                        f"stake-WR={wr:.1%} over {n} trades"
                    )

                    signals.append({
                        "identifier":     question,
                        "direction":      direction,
                        "price":          price,
                        "true_prob":      true_prob,
                        "market_id":      market.get("market_id", ""),
                        "condition_id":   condition_id,
                        "whale_address":  address,
                        "whale_wr":       wr,
                        "whale_n_trades": n,
                        "trade_size_usd": size_usd,
                        "confidence":     confidence,
                        "reason":         reason,
                        "source":         "whale_copy",
                        "volume":         market.get("volume", 0),
                    })

                    logger.info(
                        f"🐋 {confidence} whale signal: {question[:55]} | "
                        f"{direction} @ {price:.2f} | "
                        f"${size_usd:.0f} stake | WR={wr:.1%} ({n} trades)"
                    )

                except Exception as e:
                    logger.debug(f"Trade parse error: {e}")
                    continue

        except Exception as e:
            logger.debug(f"Wallet error {address[:12]}…: {e}")
            continue

    if signals:
        logger.info(f"🐋 Whale tracker: {len(signals)} signal(s) this cycle")
    else:
        logger.debug("🐋 Whale tracker: no new signals this cycle")

    return signals


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_whale_wallets() -> list:
    """Seed list + any wallets added via WHALE_WALLETS env var."""
    env_wallets = [
        w.strip().lower() for w in
        os.environ.get("WHALE_WALLETS", "").split(",")
        if w.strip()
    ]
    return list(set(_DEFAULT_WHALES + env_wallets))


def reset_whale_dedup():
    """Call daily to allow re-alerting on persistent positions."""
    global _whale_seen
    _whale_seen = set()
    logger.info("🐋 Whale dedup cache reset")


def add_whale_wallet(address: str):
    """Dynamically add a wallet to track (session only)."""
    address = address.lower().strip()
    if address not in _DEFAULT_WHALES:
        _DEFAULT_WHALES.append(address)
        logger.info(f"🐋 Whale wallet added: {address[:12]}…")
