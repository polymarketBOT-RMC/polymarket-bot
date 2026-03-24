import os
import json
import threading
import requests
import anthropic
import logging
from datetime import datetime, timezone
from flask import Flask, request, render_template_string
from apscheduler.schedulers.blocking import BlockingScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Environment variables ───────────────────────────────────────────────────
ANTHROPIC_API_KEY      = os.environ["ANTHROPIC_API_KEY"]
RESEND_API_KEY         = os.environ["RESEND_API_KEY"]
ALERT_EMAIL            = os.environ["ALERT_EMAIL"]
JSONBIN_API_KEY        = os.environ["JSONBIN_API_KEY"]
JSONBIN_BIN_ID         = os.environ["JSONBIN_BIN_ID"]
JSONBIN_HISTORY_BIN_ID = os.environ["JSONBIN_HISTORY_BIN_ID"]
ALPHAVANTAGE_API_KEY   = os.environ["ALPHAVANTAGE_API_KEY"]
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))
STOCK_SCAN_HOUR_UTC    = int(os.environ.get("STOCK_SCAN_HOUR_UTC", "23"))  # 06:00 Bangkok

# Thresholds
MYRIAD_TP_MULTIPLIER = float(os.environ.get("MYRIAD_TP_MULTIPLIER", "1.8"))
MYRIAD_SL_FRACTION   = float(os.environ.get("MYRIAD_SL_FRACTION", "0.5"))
STOCK_TP_PCT         = float(os.environ.get("STOCK_TP_PCT", "15"))
STOCK_SL_PCT         = float(os.environ.get("STOCK_SL_PCT", "7"))
CRYPTO_TP_PCT        = float(os.environ.get("CRYPTO_TP_PCT", "20"))
CRYPTO_SL_PCT        = float(os.environ.get("CRYPTO_SL_PCT", "10"))

STOCK_WATCHLIST  = [t.strip().upper() for t in
                    os.environ.get("WATCHLIST", "AAPL,TSLA,NVDA,MSFT,AMZN,GOOGL").split(",")]
CRYPTO_TOP_N     = int(os.environ.get("CRYPTO_TOP_N", "20"))  # how many pairs to send Claude after filtering

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
app    = Flask(__name__)

MYRIAD_API      = "https://api-v2.myriadprotocol.com"
AV_BASE         = "https://www.alphavantage.co/query"
COINGECKO_BASE  = "https://api.coingecko.com/api/v3"
JSONBIN_URL         = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
JSONBIN_HISTORY_URL = f"https://api.jsonbin.io/v3/b/{JSONBIN_HISTORY_BIN_ID}"
JSONBIN_HEADERS     = {"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"}

# ── HTML form ───────────────────────────────────────────────────────────────
FORM_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Signal Bot — Trade Log</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; background: #f0f2f5;
           display: flex; justify-content: center; padding: 30px 20px; }
    .wrap { max-width: 540px; width: 100%; }
    .card { background: white; border-radius: 12px; padding: 28px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08); margin-bottom: 24px; }
    h2 { color: #1a1a2e; margin-bottom: 16px; }
    .tabs { display: flex; gap: 8px; margin-bottom: 22px; flex-wrap: wrap; }
    .tab { padding: 8px 16px; border-radius: 20px; cursor: pointer; font-size: 13px;
           font-weight: bold; border: 2px solid #4361ee; color: #4361ee; background: white; }
    .tab.active { background: #4361ee; color: white; }
    .tab.myriad { border-color: #7b2ff7; color: #7b2ff7; }
    .tab.myriad.active { background: #7b2ff7; color: white; }
    .tab.crypto { border-color: #f7931a; color: #f7931a; }
    .tab.crypto.active { background: #f7931a; color: white; }
    label { display: block; font-size: 13px; font-weight: bold; color: #444; margin-bottom: 5px; }
    input, select { width: 100%; padding: 10px 14px; border: 1px solid #ddd;
                    border-radius: 8px; font-size: 15px; margin-bottom: 16px; }
    button.primary { width: 100%; color: white; border: none; padding: 13px;
                     border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; }
    .btn-myriad { background: #7b2ff7; }
    .btn-stock  { background: #4361ee; }
    .btn-crypto { background: #f7931a; }
    .success { background: #d4edda; border: 1px solid #c3e6cb; color: #155724;
               padding: 14px; border-radius: 8px; margin-bottom: 18px; font-size: 14px; }
    .pos-card { border-radius: 8px; padding: 14px; margin-bottom: 10px; }
    .pos-card.myriad { background: #f5f0ff; border-left: 4px solid #7b2ff7; }
    .pos-card.stock  { background: #f0f2ff; border-left: 4px solid #4361ee; }
    .pos-card.crypto { background: #fff8f0; border-left: 4px solid #f7931a; }
    .pos-header { font-weight: bold; color: #1a1a2e; font-size: 15px; }
    .pos-tag { display: inline-block; font-size: 11px; padding: 2px 8px;
               border-radius: 10px; margin-left: 8px; font-weight: bold; }
    .tag-myriad { background: #ede7f6; color: #7b2ff7; }
    .tag-stock  { background: #e8eafd; color: #4361ee; }
    .tag-crypto { background: #fff3e0; color: #f7931a; }
    .pos-detail { color: #555; font-size: 13px; margin-top: 5px; }
    .del-btn { background: none; border: none; color: #e63946; cursor: pointer;
               font-size: 12px; font-weight: bold; margin-top: 8px; padding: 0; width: auto; }
    .close-btn { background: none; border: none; color: #2d6a4f; cursor: pointer;
                 font-size: 12px; font-weight: bold; margin-top: 8px; margin-right: 12px;
                 padding: 0; width: auto; }
    .close-form { display: none; margin-top: 10px; background: #fff;
                  border: 1px solid #ddd; border-radius: 8px; padding: 12px; }
    .close-form input { margin-bottom: 8px; }
    .close-form button.confirm { background: #2d6a4f; color: white; border: none;
                                  padding: 8px 16px; border-radius: 6px; font-size: 13px;
                                  font-weight: bold; cursor: pointer; width: auto; }
    .close-success { background: #d4edda; border: 1px solid #c3e6cb; color: #155724;
                     padding: 14px; border-radius: 8px; margin-bottom: 18px; font-size: 14px; }
    .section { display: none; }
    .section.active { display: block; }
    h3 { color: #1a1a2e; margin-bottom: 14px; }
  </style>
  <script>
    function showTab(tab) {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
      document.getElementById('tab-' + tab).classList.add('active');
      document.getElementById('sec-' + tab).classList.add('active');
    }
    function toggleClose(id) {
      var f = document.getElementById('close-form-' + id);
      f.style.display = f.style.display === 'none' ? 'block' : 'none';
    }
  </script>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h2>📋 Log a Trade</h2>
    {% if success %}<div class="success">✅ Position saved! Bot will now monitor this trade.</div>{% endif %}
    <div class="tabs">
      <button class="tab myriad active" id="tab-myriad" onclick="showTab('myriad')">🔮 Myriad</button>
      <button class="tab" id="tab-stock" onclick="showTab('stock')">📈 Stock</button>
      <button class="tab crypto" id="tab-crypto" onclick="showTab('crypto')">₿ Crypto</button>
    </div>

    <div class="section active" id="sec-myriad">
      <form method="POST" action="/log">
        <input type="hidden" name="type" value="myriad">
        <label>Market Question</label>
        <input type="text" name="market" placeholder="e.g. Will X happen by June?" required value="{{ prefill_market }}">
        <label>Side Bought</label>
        <select name="side"><option value="YES">YES</option><option value="NO">NO</option></select>
        <label>Entry Price (e.g. 0.34)</label>
        <input type="number" name="entry_price" step="0.01" min="0.01" max="0.99" placeholder="0.34" required>
        <label>Amount Invested (USDT)</label>
        <input type="number" name="amount" step="1" min="1" placeholder="50" required>
        <label>Market ID (from Myriad URL — needed for sell alerts)</label>
        <input type="text" name="market_id" placeholder="e.g. 123456">
        <button type="submit" class="primary btn-myriad">Save Myriad Position</button>
      </form>
    </div>

    <div class="section" id="sec-stock">
      <form method="POST" action="/log">
        <input type="hidden" name="type" value="stock">
        <label>Stock Ticker</label>
        <input type="text" name="ticker" placeholder="e.g. AAPL" value="{{ prefill_ticker }}">
        <label>Action</label>
        <select name="side"><option value="BUY">BUY</option><option value="SELL_SHORT">SELL SHORT</option></select>
        <label>Entry Price (USD)</label>
        <input type="number" name="entry_price" step="0.01" min="0.01" placeholder="182.50" required>
        <label>Number of Shares</label>
        <input type="number" name="shares" step="0.01" min="0.01" placeholder="5" required>
        <button type="submit" class="primary btn-stock">Save Stock Position</button>
      </form>
    </div>

    <div class="section" id="sec-crypto">
      <form method="POST" action="/log">
        <input type="hidden" name="type" value="crypto">
        <label>Pair (Binance format)</label>
        <input type="text" name="ticker" placeholder="e.g. BTCUSDT, ETHUSDT" value="{{ prefill_ticker }}">
        <label>Action</label>
        <select name="side"><option value="BUY">BUY</option><option value="SELL_SHORT">SELL SHORT</option></select>
        <label>Entry Price (USDT)</label>
        <input type="number" name="entry_price" step="0.01" min="0.01" placeholder="65000" required>
        <label>Amount in USDT</label>
        <input type="number" name="amount" step="1" min="1" placeholder="100" required>
        <button type="submit" class="primary btn-crypto">Save Crypto Position</button>
      </form>
    </div>
  </div>

  {% if close_success %}
  <div class="card">
    <div class="close-success">✅ Trade closed and recorded in your history. P&L calculated.</div>
  </div>
  {% endif %}

  {% if positions %}
  <div class="card">
    <h3>Open Positions ({{ positions|length }})</h3>
    {% for pos in positions %}
    <div class="pos-card {{ pos.type }}">
      <div class="pos-header">
        {% if pos.type == 'myriad' %}
          {{ pos.market[:55] }}{% if pos.market|length > 55 %}...{% endif %}
          <span class="pos-tag tag-myriad">MYRIAD</span>
        {% elif pos.type == 'stock' %}
          {{ pos.ticker }}<span class="pos-tag tag-stock">STOCK</span>
        {% else %}
          {{ pos.ticker }}<span class="pos-tag tag-crypto">CRYPTO</span>
        {% endif %}
      </div>
      <div class="pos-detail">
        {{ pos.side }} @ ${{ pos.entry_price }} &nbsp;|&nbsp; {{ pos.date }}
      </div>
      <button type="button" class="close-btn" onclick="toggleClose('{{ pos.id }}')">✓ Close Trade</button>
      <form method="POST" action="/delete" style="display:inline">
        <input type="hidden" name="pos_id" value="{{ pos.id }}">
        <button type="submit" class="del-btn">✕ Remove</button>
      </form>
      <div class="close-form" id="close-form-{{ pos.id }}">
        <form method="POST" action="/close">
          <input type="hidden" name="pos_id" value="{{ pos.id }}">
          <label>Exit Price (what did you sell at?)</label>
          <input type="number" name="exit_price" step="0.0001" min="0.0001" placeholder="e.g. 0.75" required>
          <button type="submit" class="confirm">Record Sale & Calculate P&L</button>
        </form>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>
</body>
</html>
"""

# ── JSONBin helpers ─────────────────────────────────────────────────────────
def load_positions():
    try:
        r = requests.get(JSONBIN_URL, headers=JSONBIN_HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json().get("record", {}).get("positions", [])
    except Exception as e:
        logger.error(f"Load positions error: {e}")
    return []

def save_positions(positions):
    try:
        r = requests.put(JSONBIN_URL, headers=JSONBIN_HEADERS,
                         json={"positions": positions}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.error(f"Save positions error: {e}")
        return False

def load_history():
    try:
        r = requests.get(JSONBIN_HISTORY_URL, headers=JSONBIN_HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json().get("record", {}).get("trades", [])
    except Exception as e:
        logger.error(f"Load history error: {e}")
    return []

def save_history(trades):
    try:
        r = requests.put(JSONBIN_HISTORY_URL, headers=JSONBIN_HEADERS,
                         json={"trades": trades}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.error(f"Save history error: {e}")
        return False

def close_position(pos, exit_price):
    """Calculate P&L and move a position to trade history."""
    entry  = float(pos.get("entry_price", 0))
    side   = pos.get("side", "BUY")
    ptype  = pos.get("type", "")

    if ptype == "myriad":
        amount    = float(pos.get("amount", 0))
        shares    = amount / entry if entry else 0
        exit_val  = shares * exit_price
        pnl_dollar = exit_val - amount
    elif ptype == "crypto":
        amount    = float(pos.get("amount", 0))
        shares    = amount / entry if entry else 0
        exit_val  = shares * exit_price
        pnl_dollar = (exit_val - amount) if side == "BUY" else (amount - exit_val)
    else:  # stock
        shares    = float(pos.get("shares", 0))
        pnl_dollar = shares * (exit_price - entry) if side == "BUY" else shares * (entry - exit_price)

    pnl_pct = ((exit_price - entry) / entry * 100) if side == "BUY" else ((entry - exit_price) / entry * 100)

    closed = dict(pos)
    closed.update({
        "exit_price":  exit_price,
        "exit_date":   datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "pnl_dollar":  round(pnl_dollar, 2),
        "pnl_pct":     round(pnl_pct, 2),
        "win":         pnl_dollar > 0,
    })
    return closed

# ── Flask routes ────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return render_template_string(FORM_HTML, success=False, positions=load_positions(),
                                  prefill_market="", prefill_ticker="")

@app.route("/log", methods=["GET", "POST"])
def log_trade():
    prefill_market = request.args.get("market", "")
    prefill_ticker = request.args.get("ticker", "")
    success = False
    if request.method == "POST":
        pos_type = request.form.get("type", "myriad")
        positions = load_positions()
        base = {
            "id": str(int(datetime.now(timezone.utc).timestamp())),
            "type": pos_type,
            "side": request.form.get("side", "BUY"),
            "entry_price": float(request.form.get("entry_price", 0)),
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "alerted_tp": False,
            "alerted_sl": False,
        }
        if pos_type == "myriad":
            base.update({"market": request.form.get("market", "").strip(),
                         "amount": float(request.form.get("amount", 0)),
                         "market_id": request.form.get("market_id", "").strip(),
                         "alerted_expiry": False})
        elif pos_type == "crypto":
            base.update({"ticker": request.form.get("ticker", "").strip().upper(),
                         "amount": float(request.form.get("amount", 0))})
        else:
            base.update({"ticker": request.form.get("ticker", "").strip().upper(),
                         "shares": float(request.form.get("shares", 0))})
        positions.append(base)
        save_positions(positions)
        success = True
        prefill_market = ""
        prefill_ticker = ""
    return render_template_string(FORM_HTML, success=success, positions=load_positions(),
                                  prefill_market=prefill_market, prefill_ticker=prefill_ticker)

@app.route("/close", methods=["POST"])
def close_trade():
    """Mark a trade as closed, record exit price, calculate P&L."""
    pos_id     = request.form.get("pos_id", "")
    exit_price = float(request.form.get("exit_price", 0))
    positions  = load_positions()
    remaining  = []
    for pos in positions:
        if pos.get("id") == pos_id and exit_price > 0:
            closed = close_position(pos, exit_price)
            history = load_history()
            history.append(closed)
            save_history(history)
        else:
            remaining.append(pos)
    save_positions(remaining)
    return render_template_string(FORM_HTML, success=False, positions=remaining,
                                  prefill_market="", prefill_ticker="",
                                  close_success=True)

@app.route("/delete", methods=["POST"])
def delete_position():
    pos_id = request.form.get("pos_id", "")
    positions = [p for p in load_positions() if p.get("id") != pos_id]
    save_positions(positions)
    return render_template_string(FORM_HTML, success=False, positions=positions,
                                  prefill_market="", prefill_ticker="")

# ══════════════════════════════════════════════════════════════════════════════
# CRYPTO BOT — Binance free public API, no key needed
# ══════════════════════════════════════════════════════════════════════════════
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def get_all_crypto_coins():
    """
    Fetch top 200 coins by market cap from CoinGecko.
    Free API, no key needed, no geographic restrictions.
    """
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency":  "usd",
                "order":        "market_cap_desc",
                "per_page":     200,
                "page":         1,
                "sparkline":    "false",
                "price_change_percentage": "24h",
            },
            timeout=20
        )
        r.raise_for_status()
        coins = r.json()
        if not isinstance(coins, list):
            logger.error(f"CoinGecko unexpected response: {coins}")
            return []
        logger.info(f"CoinGecko: fetched {len(coins)} coins")
        return coins
    except Exception as e:
        logger.error(f"Failed to fetch CoinGecko coins: {e}")
        return []


def prefilter_pairs(coins, top_n=20):
    """
    Score coins into three buckets and return top_n for Claude.
    Buckets: biggest movers, top gainers, biggest losers (oversold bounce candidates).
    """
    import math

    valid = [c for c in coins if isinstance(c, dict) and c.get("current_price", 0) > 0]

    # Bucket 1: volume-weighted movers (top 8)
    scored = []
    for c in valid:
        try:
            change = abs(float(c.get("price_change_percentage_24h") or 0))
            volume = float(c.get("total_volume") or 0)
            score  = change * math.log10(max(volume, 1))
            scored.append((score, c))
        except Exception:
            continue
    scored.sort(key=lambda x: x[0], reverse=True)
    top_movers = [c for _, c in scored[:8]]

    # Bucket 2: biggest gainers (top 6)
    gainers = sorted(valid, key=lambda c: float(c.get("price_change_percentage_24h") or 0), reverse=True)[:6]

    # Bucket 3: biggest losers (top 6)
    losers  = sorted(valid, key=lambda c: float(c.get("price_change_percentage_24h") or 0))[:6]

    seen, result = set(), []
    for c in top_movers + gainers + losers:
        cid = c.get("id", "")
        if cid not in seen:
            seen.add(cid)
            result.append(c)
        if len(result) >= top_n:
            break

    logger.info(f"Pre-filtered to {len(result)} coins for Claude")
    return result[:top_n]


def get_coin_ohlc(coin_id):
    """Fetch 90-day OHLC data from CoinGecko and calculate RSI + SMAs."""
    result = {}
    try:
        # CoinGecko OHLC — free tier supports 90 days
        r = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
            params={"vs_currency": "usd", "days": "90"},
            timeout=15
        )
        r.raise_for_status()
        ohlc = r.json()
        if not isinstance(ohlc, list) or len(ohlc) < 14:
            return result
        closes = [float(c[4]) for c in ohlc]  # close price is index 4
        result["rsi_14"] = calculate_rsi(closes)
        result["sma_50"] = round(sum(closes[-50:]) / 50, 4) if len(closes) >= 50 else None
    except Exception as e:
        logger.error(f"OHLC error {coin_id}: {e}")
    return result


def build_crypto_summary(coin):
    """Build a clean summary dict from CoinGecko coin data + OHLC."""
    coin_id = coin.get("id", "")
    symbol  = coin.get("symbol", "").upper()
    price   = float(coin.get("current_price") or 0)
    change  = float(coin.get("price_change_percentage_24h") or 0)

    ohlc    = get_coin_ohlc(coin_id)
    rsi     = ohlc.get("rsi_14")
    sma50   = ohlc.get("sma_50")

    return {
        "symbol":         f"{symbol}/USDT",
        "coin_id":        coin_id,
        "price":          price,
        "change_24h_pct": round(change, 2),
        "volume_usd":     round(float(coin.get("total_volume") or 0)),
        "market_cap":     round(float(coin.get("market_cap") or 0)),
        "high_24h":       float(coin.get("high_24h") or 0),
        "low_24h":        float(coin.get("low_24h") or 0),
        "rsi_14":         rsi,
        "rsi_signal":     ("OVERSOLD" if rsi < 32 else "OVERBOUGHT" if rsi > 68 else "NEUTRAL") if rsi else "NO_DATA",
        "sma_50":         sma50,
        "vs_sma50":       ("above" if price > sma50 else "below") if sma50 else "insufficient_history",
    }


def analyze_crypto(crypto_data_list):
    oversold   = [d["symbol"] for d in crypto_data_list if d.get("rsi_14") and d["rsi_14"] < 32]
    overbought = [d["symbol"] for d in crypto_data_list if d.get("rsi_14") and d["rsi_14"] > 68]

    hint = ""
    if oversold:
        hint += f"\nNOTE: These coins have RSI below 32 (oversold): {', '.join(oversold)}"
    if overbought:
        hint += f"\nNOTE: These coins have RSI above 68 (overbought): {', '.join(overbought)}"

    prompt = (
        "You are a crypto trading analyst helping a retail trader.\n\n"
        "Below is market data for the top movers, biggest gainers, and biggest losers "
        "from the top 200 coins by market cap right now:\n\n"
        + json.dumps(crypto_data_list, indent=2)
        + hint
        + "\n\nSIGNAL CRITERIA — alert if ANY of these are met:\n"
        "• RSI below 32: oversold — potential BUY\n"
        "• RSI above 68: overbought — potential SELL/avoid\n"
        "• 24h gain > +7% with volume > $50M: momentum BUY\n"
        "• 24h loss > -8% with RSI below 40: oversold bounce candidate\n"
        "• Price above SMA50 after pullback: trend continuation BUY\n\n"
        "SMA200 data is not available — do NOT require it. Use RSI + 24h change + volume.\n"
        "Max 3 alerts. Skip Low confidence.\n\n"
        "For each opportunity use EXACTLY this format:\n\n"
        "ALERT: [SYMBOL]\n"
        "PRICE: $[current price]\n"
        "SIGNAL: [BUY / SELL / AVOID]\n"
        "RSI: [value] — [OVERSOLD / NEUTRAL / OVERBOUGHT]\n"
        "24H CHANGE: [value]%\n"
        "TREND: [one sentence on momentum or SMA situation]\n"
        "REASONING: [2 sentences on why this is worth acting on]\n"
        "SUGGESTED ENTRY: $[price or range]\n"
        "CONFIDENCE: [Medium / High]\n"
        "---\n\n"
        "If nothing meets criteria, respond ONLY with: NO_ALERT"
    )
    try:
        resp = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1500,
                                      messages=[{"role": "user", "content": prompt}])
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude error (crypto): {e}")
        return None


def check_crypto_positions():
    positions = [p for p in load_positions() if p.get("type") == "crypto"]
    if not positions:
        return
    alerts      = []
    updated_all = load_positions()

    for pos in positions:
        # ticker stored as e.g. "BTCUSDT" or "BTC/USDT" — normalise to CoinGecko id via symbol
        raw_symbol = pos.get("ticker", "").replace("USDT", "").replace("/", "").lower()
        if not raw_symbol:
            continue
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": raw_symbol, "vs_currencies": "usd"},
                timeout=10
            )
            data    = r.json()
            current = float(data.get(raw_symbol, {}).get("usd", 0))
        except Exception:
            continue
        if not current:
            continue

        entry      = float(pos.get("entry_price", 0))
        amount     = float(pos.get("amount", 0))
        side       = pos.get("side", "BUY")
        pnl_pct    = ((current - entry) / entry * 100) if side == "BUY" else ((entry - current) / entry * 100)
        pnl_dollar = (amount / entry) * current - amount if side == "BUY" else amount - (amount / entry) * current

        alert_msg = None
        if pnl_pct >= CRYPTO_TP_PCT and not pos.get("alerted_tp"):
            alert_msg = (
                f"TAKE PROFIT — {pos.get('ticker','')}\n"
                f"Entry: ${entry:,.2f} | Current: ${current:,.2f}\n"
                f"Gain: +{pnl_pct:.1f}% (${pnl_dollar:+.2f})\n"
                f"ACTION: Consider selling to lock in profit."
            )
            pos["alerted_tp"] = True
        elif pnl_pct <= -CRYPTO_SL_PCT and not pos.get("alerted_sl"):
            alert_msg = (
                f"STOP LOSS — {pos.get('ticker','')}\n"
                f"Entry: ${entry:,.2f} | Current: ${current:,.2f}\n"
                f"Loss: {pnl_pct:.1f}% (${pnl_dollar:+.2f})\n"
                f"ACTION: Consider cutting this position."
            )
            pos["alerted_sl"] = True

        if alert_msg:
            alerts.append(alert_msg)
            for p in updated_all:
                if p.get("id") == pos.get("id"):
                    p.update(pos)

    if alerts:
        save_positions(updated_all)
        send_sell_email(alerts)


def run_crypto_cycle():
    logger.info("── Crypto cycle starting — scanning top 200 coins via CoinGecko ──")
    check_crypto_positions()

    coins = get_all_crypto_coins()
    if not coins:
        logger.info("No CoinGecko data. Skipping.")
        return

    top_coins = prefilter_pairs(coins, top_n=CRYPTO_TOP_N)
    logger.info(f"Fetching OHLC data for {len(top_coins)} coins...")
    summaries = [build_crypto_summary(c) for c in top_coins]

    analysis = analyze_crypto(summaries)
    if not analysis or analysis == "NO_ALERT":
        logger.info("No crypto signals this cycle.")
        return
    if "ALERT:" in analysis:
        logger.info("Crypto signal found! Sending email...")
        send_crypto_email(analysis)

# ══════════════════════════════════════════════════════════════════════════════
# MYRIAD MARKETS BOT (every 30 minutes)
# ══════════════════════════════════════════════════════════════════════════════
def get_myriad_markets():
    try:
        r = requests.get(f"{MYRIAD_API}/markets",
                         params={"state": "open", "sort": "volume", "order": "desc", "limit": 100},
                         timeout=15)
        r.raise_for_status()
        data = r.json()
        markets = data.get("data", data) if isinstance(data, dict) else data

        now = datetime.now(timezone.utc)
        filtered = []
        skip_reasons = {"perpetual": 0, "wrong_network": 0, "expiring_soon": 0, "abstract_keyword": 0}

        for m in markets:
            # Skip perpetual sentiment markets — no expiresAt means no payout event
            if not m.get("expiresAt"):
                skip_reasons["perpetual"] += 1
                continue

            # Skip markets expiring within 6 hours — not enough time to act
            try:
                exp_dt = datetime.fromisoformat(m["expiresAt"].replace("Z", "+00:00"))
                hours_left = (exp_dt - now).total_seconds() / 3600
                if hours_left < 6:
                    skip_reasons["expiring_soon"] += 1
                    continue
            except Exception:
                pass

            # Skip non-BNB-Chain markets (56 = BNB Smart Chain)
            # Empty networkId is also excluded — unknown chain is untradeable
            network_id = str(m.get("networkId", ""))
            if network_id != "56":
                skip_reasons["wrong_network"] += 1
                continue

            # Skip markets with "Abstract" in the title — Abstract chain markets
            # sometimes leak through without a clear networkId
            title = m.get("title", "").lower()
            if "abstract" in title and "tge" in title:
                skip_reasons["abstract_keyword"] += 1
                continue

            filtered.append(m)

        logger.info(
            f"Myriad: {len(markets)} total → {len(filtered)} valid "
            f"(skipped: {skip_reasons['perpetual']} perpetual, "
            f"{skip_reasons['wrong_network']} wrong network, "
            f"{skip_reasons['expiring_soon']} expiring soon, "
            f"{skip_reasons['abstract_keyword']} Abstract chain)"
        )
        return filtered
    except Exception as e:
        logger.error(f"Myriad fetch error: {e}")
        return []

def get_myriad_price(market_id):
    try:
        r = requests.get(f"{MYRIAD_API}/markets/{market_id}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            market = data.get("data", data)
            for o in market.get("outcomes", []):
                if o.get("title", "").upper() in ("YES", "TRUE"):
                    return float(o.get("price", 0)), market.get("expiresAt")
    except Exception as e:
        logger.error(f"Myriad price error {market_id}: {e}")
    return None, None

def analyze_myriad(markets):
    if not markets:
        return None
    summary = []
    slug_map = {}  # question title → direct URL
    for m in markets[:20]:
        try:
            outcomes = {o.get("title", "?"): o.get("price", "?") for o in m.get("outcomes", [])}
            title = m.get("title", "?")
            slug  = m.get("slug", "")
            mkt_id = str(m.get("id", ""))
            # Build direct URL — use slug if available, else market ID
            direct_url = f"https://myriad.markets/markets/{slug}" if slug else f"https://myriad.markets/markets/{mkt_id}"
            slug_map[title] = direct_url
            summary.append({
                "question":   title,
                "direct_url": direct_url,
                "volume_usd": round(float(m.get("volume", 0) or 0), 2),
                "outcomes":   outcomes,
                "expires":    m.get("expiresAt", "?"),
            })
        except Exception:
            continue

    prompt = (
        "You are a sharp, conservative prediction market analyst.\n\n"
        "Top active Myriad Markets on BNB Chain right now (perpetual markets excluded):\n\n"
        + json.dumps(summary, indent=2)
        + "\n\nFind markets where odds look clearly wrong, or YES+NO < $0.97 (arbitrage).\n"
        "Only flag genuine edges. Silence is better than noise.\n"
        "IMPORTANT: The 'direct_url' field for each market is the exact URL to trade it — "
        "include it in your response using the MARKET URL field below.\n\n"
        "For each opportunity use EXACTLY this format:\n\n"
        "ALERT: [market question]\n"
        "MARKET URL: [direct_url from the data above]\n"
        "CURRENT ODDS: YES = $[price] / NO = $[price]\n"
        "WHY IT LOOKS MISPRICED: [2 sentences max]\n"
        "SUGGESTED PLAY: BUY [YES or NO] at $[price]\n"
        "CONFIDENCE: [Low / Medium / High]\n"
        "---\n\n"
        "If nothing found respond ONLY with: NO_ALERT"
    )
    try:
        resp = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1000,
                                      messages=[{"role": "user", "content": prompt}])
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude error (Myriad): {e}")
        return None
    try:
        resp = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1000,
                                      messages=[{"role": "user", "content": prompt}])
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude error (Myriad): {e}")
        return None

def check_myriad_positions():
    positions = [p for p in load_positions() if p.get("type") == "myriad"]
    if not positions:
        return
    alerts = []
    updated_all = load_positions()
    for pos in positions:
        market_id = pos.get("market_id", "").strip()
        if not market_id:
            continue
        entry  = float(pos.get("entry_price", 0))
        side   = pos.get("side", "YES")
        amount = float(pos.get("amount", 0))
        market = pos.get("market", "?")
        current_yes, expires_at = get_myriad_price(market_id)
        if current_yes is None:
            continue
        current    = current_yes if side == "YES" else (1.0 - current_yes)
        pnl_pct    = ((current - entry) / entry) * 100
        pnl_dollar = (amount / entry * current) - amount
        alert_msg  = None
        if current >= entry * MYRIAD_TP_MULTIPLIER and not pos.get("alerted_tp"):
            alert_msg = (f"TAKE PROFIT — Myriad\nMarket: {market}\n"
                         f"Side: {side} | Entry: ${entry:.2f} | Current: ${current:.2f}\n"
                         f"Gain: +{pnl_pct:.0f}% (${pnl_dollar:+.2f})\nACTION: Consider selling.")
            pos["alerted_tp"] = True
        elif current <= entry * MYRIAD_SL_FRACTION and not pos.get("alerted_sl"):
            alert_msg = (f"STOP LOSS — Myriad\nMarket: {market}\n"
                         f"Side: {side} | Entry: ${entry:.2f} | Current: ${current:.2f}\n"
                         f"Loss: {pnl_pct:.0f}% (${pnl_dollar:+.2f})\nACTION: Consider cutting.")
            pos["alerted_sl"] = True
        if expires_at and not pos.get("alerted_expiry"):
            try:
                hours_left = (datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                              - datetime.now(timezone.utc)).total_seconds() / 3600
                if 0 < hours_left <= 48:
                    expiry = (f"EXPIRY WARNING — {hours_left:.0f}h left\nMarket: {market}\n"
                              f"P&L: {pnl_pct:+.0f}% (${pnl_dollar:+.2f})\n"
                              f"ACTION: Decide whether to sell before expiry or hold.")
                    alert_msg = (alert_msg + "\n\n---\n\n" + expiry) if alert_msg else expiry
                    pos["alerted_expiry"] = True
            except Exception:
                pass
        if alert_msg:
            alerts.append(alert_msg)
            for p in updated_all:
                if p.get("id") == pos.get("id"):
                    p.update(pos)
    if alerts:
        save_positions(updated_all)
        send_sell_email(alerts)

def run_myriad_cycle():
    logger.info("── Myriad cycle starting ──")
    check_myriad_positions()
    markets = get_myriad_markets()
    if not markets:
        return
    logger.info(f"Fetched {len(markets)} Myriad markets. Analysing...")
    analysis = analyze_myriad(markets)
    if not analysis or analysis == "NO_ALERT":
        logger.info("No Myriad opportunities this cycle.")
        return
    if "ALERT:" in analysis:
        logger.info("Myriad opportunity found!")
        send_myriad_email(analysis)

# ══════════════════════════════════════════════════════════════════════════════
# STOCK BOT (once per day)
# ══════════════════════════════════════════════════════════════════════════════
def get_stock_data(ticker):
    result = {"ticker": ticker}
    try:
        r = requests.get(AV_BASE, params={"function": "GLOBAL_QUOTE", "symbol": ticker,
                                          "apikey": ALPHAVANTAGE_API_KEY}, timeout=15)
        q = r.json().get("Global Quote", {})
        result["price"]      = float(q.get("05. price", 0))
        result["change_pct"] = q.get("10. change percent", "?")
        result["volume"]     = q.get("06. volume", "?")
    except Exception as e:
        logger.error(f"Stock price error {ticker}: {e}")
    try:
        r = requests.get(AV_BASE, params={"function": "RSI", "symbol": ticker,
                                          "interval": "daily", "time_period": 14,
                                          "series_type": "close",
                                          "apikey": ALPHAVANTAGE_API_KEY}, timeout=15)
        rsi_data = r.json().get("Technical Analysis: RSI", {})
        latest = list(rsi_data.keys())[0] if rsi_data else None
        result["rsi"] = float(rsi_data[latest]["RSI"]) if latest else None
    except Exception as e:
        logger.error(f"RSI error {ticker}: {e}")
    try:
        r = requests.get(AV_BASE, params={"function": "SMA", "symbol": ticker,
                                          "interval": "daily", "time_period": 50,
                                          "series_type": "close",
                                          "apikey": ALPHAVANTAGE_API_KEY}, timeout=15)
        sma = r.json().get("Technical Analysis: SMA", {})
        latest = list(sma.keys())[0] if sma else None
        result["sma_50"] = float(sma[latest]["SMA"]) if latest else None
    except Exception as e:
        logger.error(f"SMA50 error {ticker}: {e}")
    try:
        r = requests.get(AV_BASE, params={"function": "NEWS_SENTIMENT", "tickers": ticker,
                                          "limit": 5, "apikey": ALPHAVANTAGE_API_KEY}, timeout=15)
        feed = r.json().get("feed", [])
        result["news"] = [{"title": i.get("title", ""),
                           "sentiment": i.get("overall_sentiment_label", "")} for i in feed[:5]]
    except Exception as e:
        logger.error(f"News error {ticker}: {e}")
    return result

def analyze_stocks(stock_data_list):
    prompt = (
        "You are a sharp stock analyst helping a retail investor.\n\n"
        "Daily stock data and news:\n\n"
        + json.dumps(stock_data_list, indent=2)
        + "\n\nRSI below 30 = oversold (BUY). RSI above 70 = overbought (SELL).\n"
        "Only alert when technical AND news agree. Be conservative.\n\n"
        "For each genuine opportunity use EXACTLY this format:\n\n"
        "ALERT: [TICKER] — [Company name]\n"
        "PRICE: $[price]\n"
        "SIGNAL: [BUY / SELL]\n"
        "TECHNICAL REASON: [1-2 sentences]\n"
        "NEWS REASON: [1-2 sentences]\n"
        "SUGGESTED ENTRY: $[price range]\n"
        "CONFIDENCE: [Low / Medium / High]\n"
        "---\n\n"
        "If nothing, respond ONLY with: NO_ALERT"
    )
    try:
        resp = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1500,
                                      messages=[{"role": "user", "content": prompt}])
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude error (stocks): {e}")
        return None

def check_stock_positions():
    positions = [p for p in load_positions() if p.get("type") == "stock"]
    if not positions:
        return
    alerts = []
    updated_all = load_positions()
    for pos in positions:
        ticker = pos.get("ticker", "")
        if not ticker:
            continue
        data    = get_stock_data(ticker)
        current = data.get("price", 0)
        if not current:
            continue
        entry  = float(pos.get("entry_price", 0))
        shares = float(pos.get("shares", 0))
        side   = pos.get("side", "BUY")
        pnl_pct    = ((current - entry) / entry * 100) if side == "BUY" else ((entry - current) / entry * 100)
        pnl_dollar = shares * abs(current - entry) * (1 if side == "BUY" else -1)
        alert_msg = None
        if pnl_pct >= STOCK_TP_PCT and not pos.get("alerted_tp"):
            alert_msg = (f"TAKE PROFIT — {ticker}\nEntry: ${entry:.2f} | Current: ${current:.2f}\n"
                         f"Gain: +{pnl_pct:.1f}% (${pnl_dollar:+.2f})\nACTION: Consider selling.")
            pos["alerted_tp"] = True
        elif pnl_pct <= -STOCK_SL_PCT and not pos.get("alerted_sl"):
            alert_msg = (f"STOP LOSS — {ticker}\nEntry: ${entry:.2f} | Current: ${current:.2f}\n"
                         f"Loss: {pnl_pct:.1f}% (${pnl_dollar:+.2f})\nACTION: Consider cutting position.")
            pos["alerted_sl"] = True
        if alert_msg:
            alerts.append(alert_msg)
            for p in updated_all:
                if p.get("id") == pos.get("id"):
                    p.update(pos)
    if alerts:
        save_positions(updated_all)
        send_sell_email(alerts)

def run_stock_cycle():
    logger.info("── Daily stock scan starting ──")
    check_stock_positions()
    logger.info(f"Scanning stocks: {STOCK_WATCHLIST}")
    all_data = [get_stock_data(t) for t in STOCK_WATCHLIST]
    analysis = analyze_stocks(all_data)
    if not analysis or analysis == "NO_ALERT":
        logger.info("No stock signals today.")
        return
    if "ALERT:" in analysis:
        logger.info("Stock signal found!")
        send_stock_email(analysis)

# ══════════════════════════════════════════════════════════════════════════════
# EMAIL
# ══════════════════════════════════════════════════════════════════════════════
def get_log_url():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    return ("https://" + domain) if domain and not domain.startswith("http") else domain

def _parse_blocks(analysis_text, field_map, border_color, log_url=None, log_param=""):
    cards = ""
    for block in analysis_text.split("---"):
        block = block.strip()
        if not block:
            continue
        inner = ""
        param_val = ""
        for line in block.splitlines():
            for prefix, template in field_map.items():
                if line.startswith(prefix):
                    val = line.replace(prefix, "").strip()
                    if prefix == list(field_map.keys())[0]:
                        param_val = val
                    inner += template.format(val=val)
                    break
        if inner:
            log_btn = ""
            if log_url:
                log_btn = (f'<a href="{log_url}/log?{log_param}={param_val}" '
                           f'style="display:inline-block;background:#2d6a4f;color:#fff;'
                           f'padding:8px 16px;text-decoration:none;border-radius:6px;'
                           f'font-size:13px;font-weight:bold;margin-top:10px">📋 Log this trade</a>')
            cards += (f'<div style="background:#f8f9ff;border-left:4px solid {border_color};'
                      f'padding:16px;border-radius:6px;margin-bottom:16px">{inner}{log_btn}</div>')
    return cards

def send_email(subject, html, plain):
    try:
        r = requests.post("https://api.resend.com/emails",
                          headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                                   "Content-Type": "application/json"},
                          json={"from": "Signal Bot <onboarding@resend.dev>",
                                "to": [ALERT_EMAIL], "subject": subject,
                                "html": html, "text": plain}, timeout=15)
        if r.status_code in (200, 201):
            logger.info("Email sent.")
        else:
            logger.error(f"Resend error {r.status_code}: {r.text}")
    except Exception as e:
        logger.error(f"Email error: {e}")

def send_myriad_email(analysis):
    log_url = get_log_url()
    field_map = {
        "ALERT:": '<h3 style="margin:0 0 10px;color:#1a1a2e">{val}</h3>',
        "CURRENT ODDS:": '<p style="margin:5px 0"><strong>Odds:</strong> {val}</p>',
        "WHY IT LOOKS MISPRICED:": '<p style="margin:5px 0"><strong>Why:</strong> {val}</p>',
        "SUGGESTED PLAY:": '<p style="margin:8px 0;font-size:16px;font-weight:bold;color:#2d6a4f">▶ {val}</p>',
        "CONFIDENCE:": '<p style="margin:5px 0"><strong>Confidence:</strong> {val}</p>',
    }
    cards = _parse_blocks(analysis, field_map, "#7b2ff7", log_url, "market")
    html = (
        '<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
        '<h2 style="color:#1a1a2e">🔮 Myriad Markets — Opportunity Spotted</h2>'
        + cards +
        '<a href="https://myriad.markets" style="display:inline-block;background:#7b2ff7;'
        'color:#fff;padding:12px 28px;text-decoration:none;border-radius:6px;'
        'font-weight:bold;margin-top:8px">Open Myriad Markets</a>'
        '<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice.</p>'
        '</body></html>'
    )
    send_email("🔮 Myriad Alert — Opportunity Spotted", html,
               f"Myriad Opportunity\n\n{analysis}\n\nNot financial advice.")

def send_crypto_email(analysis):
    log_url = get_log_url()
    field_map = {
        "ALERT:": '<h3 style="margin:0 0 10px;color:#1a1a2e">{val}</h3>',
        "PRICE:": '<p style="margin:5px 0"><strong>Price:</strong> {val}</p>',
        "SIGNAL:": '<p style="margin:8px 0;font-size:18px;font-weight:bold;color:#f7931a">▶ {val}</p>',
        "RSI:": '<p style="margin:5px 0"><strong>RSI:</strong> {val}</p>',
        "24H CHANGE:": '<p style="margin:5px 0"><strong>24h Change:</strong> {val}</p>',
        "TREND:": '<p style="margin:5px 0"><strong>Trend:</strong> {val}</p>',
        "REASONING:": '<p style="margin:5px 0"><strong>Why:</strong> {val}</p>',
        "SUGGESTED ENTRY:": '<p style="margin:5px 0"><strong>Entry:</strong> {val}</p>',
        "CONFIDENCE:": '<p style="margin:5px 0"><strong>Confidence:</strong> {val}</p>',
    }

    # Build cards manually so we can add a direct Binance trade link per symbol
    cards = ""
    for block in analysis.split("---"):
        block = block.strip()
        if not block:
            continue
        inner  = ""
        symbol = ""
        for line in block.splitlines():
            matched = False
            for prefix, template in field_map.items():
                if line.startswith(prefix):
                    val = line.replace(prefix, "").strip()
                    if prefix == "ALERT:":
                        symbol = val.replace("USDT", "").strip()
                    inner += template.format(val=val)
                    matched = True
                    break

        if inner:
            # Direct link to the exact trading pair on Binance
            binance_url = f"https://www.binance.com/en/trade/{symbol}_USDT" if symbol else "https://www.binance.com/en/trade"
            trade_btn = (
                f'<a href="{binance_url}" style="display:inline-block;background:#f7931a;'
                f'color:#fff;padding:10px 20px;text-decoration:none;border-radius:6px;'
                f'font-size:14px;font-weight:bold;margin-top:12px;margin-right:8px">'
                f'▶ Trade on Binance</a>'
            )
            log_btn = ""
            if log_url:
                log_btn = (
                    f'<a href="{log_url}/log?ticker={symbol}USDT" style="display:inline-block;'
                    f'background:#2d6a4f;color:#fff;padding:10px 20px;text-decoration:none;'
                    f'border-radius:6px;font-size:14px;font-weight:bold;margin-top:12px">'
                    f'📋 Log Trade</a>'
                )
            cards += (
                f'<div style="background:#fff8f0;border-left:4px solid #f7931a;'
                f'padding:16px;border-radius:6px;margin-bottom:16px">'
                f'{inner}{trade_btn}{log_btn}</div>'
            )

    html = (
        '<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
        '<h2 style="color:#1a1a2e">₿ Crypto Signal Alert</h2>'
        '<p style="color:#666;font-size:13px;margin-bottom:20px">Scanning all Binance USDT pairs — RSI + momentum analysis</p>'
        + cards +
        '<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice. '
        'Crypto is highly volatile — only trade what you can afford to lose.</p>'
        '</body></html>'
    )
    send_email("₿ Crypto Signal — " + analysis.split("\n")[0].replace("ALERT:", "").strip()[:40],
               html, f"Crypto Signal\n\n{analysis}\n\nNot financial advice.")

def send_stock_email(analysis):
    log_url = get_log_url()
    field_map = {
        "ALERT:": '<h3 style="margin:0 0 10px;color:#1a1a2e">{val}</h3>',
        "PRICE:": '<p style="margin:5px 0"><strong>Price:</strong> {val}</p>',
        "SIGNAL:": '<p style="margin:8px 0;font-size:18px;font-weight:bold;color:#4361ee">▶ {val}</p>',
        "TECHNICAL REASON:": '<p style="margin:5px 0"><strong>Technical:</strong> {val}</p>',
        "NEWS REASON:": '<p style="margin:5px 0"><strong>News:</strong> {val}</p>',
        "SUGGESTED ENTRY:": '<p style="margin:5px 0"><strong>Entry:</strong> {val}</p>',
        "CONFIDENCE:": '<p style="margin:5px 0"><strong>Confidence:</strong> {val}</p>',
    }
    cards = _parse_blocks(analysis, field_map, "#4361ee", log_url, "ticker")
    html = (
        '<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
        '<h2 style="color:#1a1a2e">📈 Daily Stock Signal</h2>'
        '<p style="color:#666;font-size:13px;margin-bottom:20px">Technical + news combined analysis</p>'
        + cards +
        '<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice. '
        'Sort your broker account before trading stocks.</p>'
        '</body></html>'
    )
    send_email("📈 Daily Stock Signal", html,
               f"Stock Signal\n\n{analysis}\n\nNot financial advice.")

def send_sell_email(alerts):
    cards = ""
    for alert in alerts:
        lines  = alert.strip().splitlines()
        title  = lines[0] if lines else "Alert"
        body   = "".join(f'<p style="margin:4px 0;font-size:14px;color:#333">{l}</p>'
                         for l in lines[1:] if l.strip())
        border = "#e63946" if "STOP LOSS" in title else "#2d6a4f"
        emoji  = "🛑" if "STOP LOSS" in title else "💰"
        cards += (f'<div style="background:#fff;border-left:5px solid {border};'
                  f'padding:16px;border-radius:6px;margin-bottom:16px;'
                  f'box-shadow:0 2px 8px rgba(0,0,0,0.06)">'
                  f'<h3 style="margin:0 0 10px;color:#1a1a2e">{emoji} {title}</h3>{body}</div>')
    html = (
        '<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
        '<h2 style="color:#1a1a2e">📊 Position Alert — Action Required</h2>'
        + cards +
        '<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice.</p>'
        '</body></html>'
    )
    send_email("📊 Position Alert — Action Required", html,
               "Position Alert\n\n" + "\n\n---\n\n".join(alerts))

# ══════════════════════════════════════════════════════════════════════════════
# REPORTING — daily, weekly, monthly P&L summaries
# ══════════════════════════════════════════════════════════════════════════════
def build_report(trades, period_label):
    """Build P&L stats dict from a list of closed trades."""
    if not trades:
        return None

    total_pnl    = sum(t.get("pnl_dollar", 0) for t in trades)
    wins         = [t for t in trades if t.get("win")]
    losses       = [t for t in trades if not t.get("win")]
    win_rate     = (len(wins) / len(trades) * 100) if trades else 0
    best_trade   = max(trades, key=lambda t: t.get("pnl_dollar", 0))
    worst_trade  = min(trades, key=lambda t: t.get("pnl_dollar", 0))

    by_type = {}
    for t in trades:
        pt = t.get("type", "unknown")
        by_type.setdefault(pt, {"count": 0, "pnl": 0})
        by_type[pt]["count"] += 1
        by_type[pt]["pnl"]   += t.get("pnl_dollar", 0)

    return {
        "period":      period_label,
        "total_trades": len(trades),
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     round(win_rate, 1),
        "total_pnl":    round(total_pnl, 2),
        "best_trade":   best_trade,
        "worst_trade":  worst_trade,
        "by_type":      by_type,
    }

def build_report_html(stats, all_time_pnl):
    """Build a clean HTML email for a P&L report."""
    pnl_colour = "#2d6a4f" if stats["total_pnl"] >= 0 else "#e63946"
    pnl_emoji  = "📈" if stats["total_pnl"] >= 0 else "📉"

    # By type breakdown
    type_rows = ""
    type_labels = {"myriad": "🔮 Myriad", "crypto": "₿ Crypto", "stock": "📈 Stocks"}
    for pt, data in stats["by_type"].items():
        col = "#2d6a4f" if data["pnl"] >= 0 else "#e63946"
        type_rows += (
            f'<tr><td style="padding:8px;border-bottom:1px solid #eee">{type_labels.get(pt, pt)}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;text-align:center">{data["count"]}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;text-align:right;'
            f'color:{col};font-weight:bold">${data["pnl"]:+.2f}</td></tr>'
        )

    # Best/worst trade name
    def trade_name(t):
        if t.get("type") == "myriad":
            return t.get("market", "?")[:50]
        return t.get("ticker", "?")

    best  = stats["best_trade"]
    worst = stats["worst_trade"]

    return (
        '<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
        f'<h2 style="color:#1a1a2e">{pnl_emoji} {stats["period"]} Trading Report</h2>'

        # Summary box
        f'<div style="background:#f8f9ff;border-radius:12px;padding:20px;margin:20px 0">'
        f'<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:12px">'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;color:{pnl_colour}">'
        f'${stats["total_pnl"]:+.2f}</div><div style="color:#666;font-size:13px">Total P&L</div></div>'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;color:#1a1a2e">'
        f'{stats["win_rate"]}%</div><div style="color:#666;font-size:13px">Win Rate</div></div>'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;color:#1a1a2e">'
        f'{stats["total_trades"]}</div><div style="color:#666;font-size:13px">Trades</div></div>'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;color:#2d6a4f">'
        f'{stats["wins"]}</div><div style="color:#666;font-size:13px">Wins</div></div>'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;color:#e63946">'
        f'{stats["losses"]}</div><div style="color:#666;font-size:13px">Losses</div></div>'
        f'</div></div>'

        # By market type
        f'<h3 style="color:#1a1a2e;margin:20px 0 10px">By Market Type</h3>'
        f'<table style="width:100%;border-collapse:collapse">'
        f'<tr style="background:#f0f2ff"><th style="padding:8px;text-align:left">Market</th>'
        f'<th style="padding:8px;text-align:center">Trades</th>'
        f'<th style="padding:8px;text-align:right">P&L</th></tr>'
        f'{type_rows}</table>'

        # Best and worst
        f'<div style="display:flex;gap:12px;margin:20px 0;flex-wrap:wrap">'
        f'<div style="flex:1;min-width:220px;background:#f0fff4;border-left:4px solid #2d6a4f;'
        f'padding:14px;border-radius:6px">'
        f'<div style="font-size:11px;font-weight:bold;color:#2d6a4f;margin-bottom:4px">BEST TRADE</div>'
        f'<div style="font-size:13px;color:#1a1a2e;margin-bottom:6px">{trade_name(best)}</div>'
        f'<div style="font-size:20px;font-weight:bold;color:#2d6a4f">${best.get("pnl_dollar",0):+.2f}</div>'
        f'<div style="font-size:12px;color:#555">{best.get("pnl_pct",0):+.1f}%</div></div>'

        f'<div style="flex:1;min-width:220px;background:#fff5f5;border-left:4px solid #e63946;'
        f'padding:14px;border-radius:6px">'
        f'<div style="font-size:11px;font-weight:bold;color:#e63946;margin-bottom:4px">WORST TRADE</div>'
        f'<div style="font-size:13px;color:#1a1a2e;margin-bottom:6px">{trade_name(worst)}</div>'
        f'<div style="font-size:20px;font-weight:bold;color:#e63946">${worst.get("pnl_dollar",0):+.2f}</div>'
        f'<div style="font-size:12px;color:#555">{worst.get("pnl_pct",0):+.1f}%</div></div>'
        f'</div>'

        # All-time running total
        f'<div style="background:#1a1a2e;color:white;border-radius:8px;padding:16px;'
        f'text-align:center;margin-top:8px">'
        f'<div style="font-size:13px;opacity:0.7;margin-bottom:4px">ALL-TIME TOTAL P&L</div>'
        f'<div style="font-size:32px;font-weight:bold;color:{"#4ade80" if all_time_pnl >= 0 else "#f87171"}">'
        f'${all_time_pnl:+.2f}</div></div>'

        '<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice.</p>'
        '</body></html>'
    )

def filter_trades_by_period(trades, period):
    """Filter trade history to a specific period."""
    now = datetime.now(timezone.utc)
    result = []
    for t in trades:
        try:
            exit_date = datetime.strptime(t.get("exit_date", ""), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if period == "daily"   and (now - exit_date).days < 1:
                result.append(t)
            elif period == "weekly"  and (now - exit_date).days < 7:
                result.append(t)
            elif period == "monthly" and (now - exit_date).days < 31:
                result.append(t)
        except Exception:
            continue
    return result

def send_report(period):
    logger.info(f"Generating {period} report...")
    history = load_history()
    if not history:
        logger.info(f"No closed trades yet — skipping {period} report.")
        return

    period_trades = filter_trades_by_period(history, period)
    if not period_trades:
        logger.info(f"No trades closed in this {period} period — skipping report.")
        return

    all_time_pnl = sum(t.get("pnl_dollar", 0) for t in history)
    labels = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
    stats  = build_report(period_trades, f"{labels[period]} ({datetime.now(timezone.utc).strftime('%d %b %Y')})")
    if not stats:
        return

    html    = build_report_html(stats, all_time_pnl)
    subject = f"{'📈' if stats['total_pnl'] >= 0 else '📉'} {labels[period]} Report — ${stats['total_pnl']:+.2f} P&L"
    plain   = (
        f"{labels[period]} Trading Report\n\n"
        f"Total P&L: ${stats['total_pnl']:+.2f}\n"
        f"Win Rate:  {stats['win_rate']}%\n"
        f"Trades:    {stats['total_trades']} ({stats['wins']} wins, {stats['losses']} losses)\n\n"
        f"All-time P&L: ${all_time_pnl:+.2f}"
    )
    send_email(subject, html, plain)
    logger.info(f"{period.capitalize()} report sent.")

# ── Entry point ─────────────────────────────────────────────────────────────
def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

if __name__ == "__main__":
    logger.info("Combined Signal Bot starting up...")
    logger.info(f"Myriad + Crypto: every {CHECK_INTERVAL_MINUTES} minutes")
    logger.info(f"Stocks: daily at {STOCK_SCAN_HOUR_UTC}:00 UTC")
    logger.info(f"Stock watchlist:  {STOCK_WATCHLIST}")
    logger.info(f"Crypto: scanning top 200 coins via CoinGecko, top {CRYPTO_TOP_N} sent to Claude")

    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Trade log web form live.")

    # Run all three on startup
    run_myriad_cycle()
    run_crypto_cycle()
    run_stock_cycle()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_myriad_cycle, "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.add_job(run_crypto_cycle, "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.add_job(run_stock_cycle,  "cron", hour=STOCK_SCAN_HOUR_UTC, minute=0)

    # Reports — daily 11pm UTC (6am Bangkok next day), weekly Mon 11pm, monthly 1st 11pm
    scheduler.add_job(lambda: send_report("daily"),   "cron", hour=23, minute=0)
    scheduler.add_job(lambda: send_report("weekly"),  "cron", day_of_week="mon", hour=23, minute=30)
    scheduler.add_job(lambda: send_report("monthly"), "cron", day=1,  hour=23, minute=45)

    logger.info("All three bots + reporting running.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
