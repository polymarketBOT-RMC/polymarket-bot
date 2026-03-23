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
CRYPTO_WATCHLIST = [t.strip().upper() for t in
                    os.environ.get("CRYPTO_WATCHLIST", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT").split(",")]

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
app    = Flask(__name__)

MYRIAD_API      = "https://api-v2.myriadprotocol.com"
AV_BASE         = "https://www.alphavantage.co/query"
BINANCE_BASE    = "https://api.binance.com/api/v3"
JSONBIN_URL     = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
JSONBIN_HEADERS = {"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"}

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
      <form method="POST" action="/delete" style="display:inline">
        <input type="hidden" name="pos_id" value="{{ pos.id }}">
        <button type="submit" class="del-btn">✕ Remove</button>
      </form>
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

def get_crypto_data(symbol):
    result = {"symbol": symbol}
    try:
        # Current price
        r = requests.get(f"{BINANCE_BASE}/ticker/24hr",
                         params={"symbol": symbol}, timeout=10)
        d = r.json()
        result["price"]      = float(d.get("lastPrice", 0))
        result["change_pct"] = float(d.get("priceChangePercent", 0))
        result["volume_usdt"]= round(float(d.get("quoteVolume", 0)), 0)
        result["high_24h"]   = float(d.get("highPrice", 0))
        result["low_24h"]    = float(d.get("lowPrice", 0))
    except Exception as e:
        logger.error(f"Binance price error {symbol}: {e}")

    try:
        # OHLCV candles for RSI + SMA
        r = requests.get(f"{BINANCE_BASE}/klines",
                         params={"symbol": symbol, "interval": "1d", "limit": 220},
                         timeout=10)
        candles = r.json()
        closes = [float(c[4]) for c in candles]

        result["rsi_14"]  = calculate_rsi(closes[-30:])
        result["sma_50"]  = round(sum(closes[-50:]) / 50, 4) if len(closes) >= 50 else None
        result["sma_200"] = round(sum(closes[-200:]) / 200, 4) if len(closes) >= 200 else None
        result["price_vs_sma50"]  = "above" if result.get("price", 0) > (result["sma_50"] or 0) else "below"
        result["price_vs_sma200"] = "above" if result.get("price", 0) > (result["sma_200"] or 0) else "below"
    except Exception as e:
        logger.error(f"Binance candles error {symbol}: {e}")

    return result

def analyze_crypto(crypto_data_list):
    prompt = (
        "You are a sharp crypto analyst helping a retail trader.\n\n"
        "Current Binance market data:\n\n"
        + json.dumps(crypto_data_list, indent=2)
        + "\n\nRSI GUIDE: Below 30 = oversold (BUY signal). Above 70 = overbought (SELL signal).\n"
        "SMA GUIDE: Price above both SMA50 and SMA200 = bullish. Below both = bearish.\n"
        "24h change > +5% with high volume = momentum signal.\n"
        "24h change < -8% = potential oversold bounce.\n\n"
        "Only alert when MULTIPLE signals agree. Crypto is volatile — be conservative.\n"
        "Never alert on Low confidence signals.\n\n"
        "For each genuine opportunity use EXACTLY this format:\n\n"
        "ALERT: [SYMBOL] (e.g. BTCUSDT)\n"
        "PRICE: $[current price]\n"
        "SIGNAL: [BUY / SELL]\n"
        "RSI: [value] — [interpretation]\n"
        "TREND: [price vs SMA50/200 interpretation]\n"
        "REASONING: [2-3 sentences combining all signals]\n"
        "SUGGESTED ENTRY: $[price range]\n"
        "CONFIDENCE: [Medium / High]\n"
        "---\n\n"
        "If nothing convincing, respond ONLY with: NO_ALERT"
    )
    try:
        resp = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1200,
                                      messages=[{"role": "user", "content": prompt}])
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude error (crypto): {e}")
        return None

def check_crypto_positions():
    positions = [p for p in load_positions() if p.get("type") == "crypto"]
    if not positions:
        return
    alerts = []
    updated_all = load_positions()

    for pos in positions:
        symbol = pos.get("ticker", "")
        if not symbol:
            continue
        data = get_crypto_data(symbol)
        current = data.get("price", 0)
        if not current:
            continue

        entry  = float(pos.get("entry_price", 0))
        amount = float(pos.get("amount", 0))
        side   = pos.get("side", "BUY")
        pnl_pct    = ((current - entry) / entry * 100) if side == "BUY" else ((entry - current) / entry * 100)
        pnl_dollar = (amount / entry) * current - amount if side == "BUY" else amount - (amount / entry) * current

        alert_msg = None
        if pnl_pct >= CRYPTO_TP_PCT and not pos.get("alerted_tp"):
            alert_msg = (
                f"TAKE PROFIT — {symbol}\n"
                f"Entry: ${entry:,.2f} | Current: ${current:,.2f}\n"
                f"Gain: +{pnl_pct:.1f}% (${pnl_dollar:+.2f})\n"
                f"ACTION: Consider selling to lock in profit on Binance."
            )
            pos["alerted_tp"] = True
        elif pnl_pct <= -CRYPTO_SL_PCT and not pos.get("alerted_sl"):
            alert_msg = (
                f"STOP LOSS — {symbol}\n"
                f"Entry: ${entry:,.2f} | Current: ${current:,.2f}\n"
                f"Loss: {pnl_pct:.1f}% (${pnl_dollar:+.2f})\n"
                f"ACTION: Consider cutting this position on Binance."
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
    logger.info("── Crypto cycle starting ──")
    check_crypto_positions()
    logger.info(f"Scanning crypto watchlist: {CRYPTO_WATCHLIST}")
    all_data = [get_crypto_data(symbol) for symbol in CRYPTO_WATCHLIST]
    analysis = analyze_crypto(all_data)
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
                         params={"state": "open", "sort": "volume", "order": "desc", "limit": 50},
                         timeout=15)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data
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
    for m in markets[:20]:
        try:
            outcomes = {o.get("title", "?"): o.get("price", "?") for o in m.get("outcomes", [])}
            summary.append({"question": m.get("title", "?"),
                            "volume_usd": round(float(m.get("volume", 0) or 0), 2),
                            "outcomes": outcomes, "expires": m.get("expiresAt", "?")})
        except Exception:
            continue
    prompt = (
        "You are a sharp, conservative prediction market analyst.\n\n"
        "Top active Myriad Markets right now:\n\n"
        + json.dumps(summary, indent=2)
        + "\n\nFind markets where odds look clearly wrong, or YES+NO < $0.97 (arbitrage).\n"
        "Only flag genuine edges. Silence is better than noise.\n\n"
        "For each opportunity use EXACTLY this format:\n\n"
        "ALERT: [market question]\n"
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
        "TREND:": '<p style="margin:5px 0"><strong>Trend:</strong> {val}</p>',
        "REASONING:": '<p style="margin:5px 0"><strong>Why:</strong> {val}</p>',
        "SUGGESTED ENTRY:": '<p style="margin:5px 0"><strong>Entry:</strong> {val}</p>',
        "CONFIDENCE:": '<p style="margin:5px 0"><strong>Confidence:</strong> {val}</p>',
    }
    cards = _parse_blocks(analysis, field_map, "#f7931a", log_url, "ticker")
    html = (
        '<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
        '<h2 style="color:#1a1a2e">₿ Crypto Signal Alert</h2>'
        '<p style="color:#666;font-size:13px;margin-bottom:20px">Technical analysis via Binance data</p>'
        + cards +
        '<a href="https://www.binance.com/en/trade" style="display:inline-block;background:#f7931a;'
        'color:#fff;padding:12px 28px;text-decoration:none;border-radius:6px;'
        'font-weight:bold;margin-top:8px">Open Binance</a>'
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

# ── Entry point ─────────────────────────────────────────────────────────────
def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

if __name__ == "__main__":
    logger.info("Combined Signal Bot starting up...")
    logger.info(f"Myriad + Crypto: every {CHECK_INTERVAL_MINUTES} minutes")
    logger.info(f"Stocks: daily at {STOCK_SCAN_HOUR_UTC}:00 UTC")
    logger.info(f"Stock watchlist:  {STOCK_WATCHLIST}")
    logger.info(f"Crypto watchlist: {CRYPTO_WATCHLIST}")

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
    logger.info("All three bots running.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")

