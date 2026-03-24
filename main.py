import os
import json
import time
import hashlib
import threading
import requests
import anthropic
import logging
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, render_template_string
from apscheduler.schedulers.blocking import BlockingScheduler
from waitress import serve

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
STOCK_SCAN_HOUR_UTC    = int(os.environ.get("STOCK_SCAN_HOUR_UTC", "23"))

MYRIAD_TP_MULTIPLIER = float(os.environ.get("MYRIAD_TP_MULTIPLIER", "1.8"))
MYRIAD_SL_FRACTION   = float(os.environ.get("MYRIAD_SL_FRACTION", "0.5"))
STOCK_TP_PCT         = float(os.environ.get("STOCK_TP_PCT", "15"))
STOCK_SL_PCT         = float(os.environ.get("STOCK_SL_PCT", "7"))
CRYPTO_TP_PCT        = float(os.environ.get("CRYPTO_TP_PCT", "20"))
CRYPTO_SL_PCT        = float(os.environ.get("CRYPTO_SL_PCT", "10"))
CRYPTO_TOP_N         = int(os.environ.get("CRYPTO_TOP_N", "20"))

STOCK_WATCHLIST = [t.strip().upper() for t in
                   os.environ.get("WATCHLIST", "AAPL,TSLA,NVDA,MSFT,AMZN,GOOGL").split(",")]

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
app    = Flask(__name__)

MYRIAD_API          = "https://api-v2.myriadprotocol.com"
AV_BASE             = "https://www.alphavantage.co/query"
COINGECKO_BASE      = "https://api.coingecko.com/api/v3"
JSONBIN_URL         = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
JSONBIN_HISTORY_URL = f"https://api.jsonbin.io/v3/b/{JSONBIN_HISTORY_BIN_ID}"
JSONBIN_HEADERS     = {"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"}

# ── UPGRADE 1: In-memory deduplication cache ────────────────────────────────
# Stores {signal_hash: datetime_sent} — resets on restart (acceptable)
_dedup_cache: dict = {}
_dedup_lock         = threading.Lock()
DEDUP_HOURS         = 24  # don't repeat same signal within 24 hours

def signal_hash(identifier: str, direction: str) -> str:
    return hashlib.md5(f"{identifier}:{direction}".encode()).hexdigest()

def is_duplicate(identifier: str, direction: str) -> bool:
    h   = signal_hash(identifier, direction)
    now = datetime.now(timezone.utc)
    with _dedup_lock:
        if h in _dedup_cache:
            if (now - _dedup_cache[h]).total_seconds() < DEDUP_HOURS * 3600:
                return True
        _dedup_cache[h] = now
    return False

def prune_dedup_cache():
    now = datetime.now(timezone.utc)
    with _dedup_lock:
        expired = [k for k, v in _dedup_cache.items()
                   if (now - v).total_seconds() > DEDUP_HOURS * 3600]
        for k in expired:
            del _dedup_cache[k]

# ── UPGRADE 2: Fear & Greed Index ───────────────────────────────────────────
def get_fear_and_greed() -> dict:
    """Fetch current crypto Fear & Greed index. Free API, no key needed."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        data = r.json().get("data", [{}])[0]
        value = int(data.get("value", 50))
        label = data.get("value_classification", "Neutral")
        logger.info(f"Fear & Greed Index: {value} ({label})")
        return {"value": value, "label": label}
    except Exception as e:
        logger.error(f"Fear & Greed fetch error: {e}")
        return {"value": 50, "label": "Neutral"}

# ── UPGRADE 3: Signal outcome tracking ─────────────────────────────────────
# Signals are stored in history JSONBin with type="signal" for outcome tracking
def log_signal(identifier: str, signal_type: str, direction: str,
               price: float, market_type: str):
    """Record a signal so we can check its outcome 24h and 72h later."""
    try:
        history = load_history()
        history.append({
            "type":        "signal",
            "id":          signal_hash(identifier, direction),
            "identifier":  identifier,
            "signal_type": signal_type,
            "direction":   direction,
            "price_at_signal": price,
            "market_type": market_type,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "checked_24h": False,
            "checked_72h": False,
        })
        save_history(history)
    except Exception as e:
        logger.error(f"Log signal error: {e}")

def check_signal_outcomes():
    """Check 24h and 72h outcomes for logged signals and email a report card."""
    try:
        history  = load_history()
        now      = datetime.now(timezone.utc)
        updates  = []
        reports  = []

        for item in history:
            if item.get("type") != "signal":
                continue
            try:
                ts = datetime.fromisoformat(item["timestamp"])
            except Exception:
                continue

            hours_elapsed = (now - ts).total_seconds() / 3600
            mtype     = item.get("market_type", "")
            ident     = item.get("identifier", "")
            direction = item.get("direction", "")
            entry     = float(item.get("price_at_signal", 0))

            current_price = None

            # Fetch current price based on market type
            if mtype == "crypto" and not item.get("checked_24h") and hours_elapsed >= 24:
                coin_id = ident.replace("USDT", "").replace("/", "").lower()
                try:
                    r = requests.get(f"{COINGECKO_BASE}/simple/price",
                                     params={"ids": coin_id, "vs_currencies": "usd"}, timeout=10)
                    current_price = float(r.json().get(coin_id, {}).get("usd", 0))
                except Exception:
                    pass
            elif mtype == "stock" and not item.get("checked_24h") and hours_elapsed >= 24:
                try:
                    r = requests.get(AV_BASE, params={"function": "GLOBAL_QUOTE",
                                     "symbol": ident, "apikey": ALPHAVANTAGE_API_KEY}, timeout=15)
                    current_price = float(r.json().get("Global Quote", {}).get("05. price", 0))
                except Exception:
                    pass

            if current_price and entry > 0:
                pnl_pct = ((current_price - entry) / entry * 100)
                if direction in ("SELL", "AVOID"):
                    pnl_pct = -pnl_pct
                period = "24h" if not item.get("checked_24h") else "72h"
                correct = pnl_pct > 0
                reports.append({
                    "identifier": ident,
                    "direction":  direction,
                    "entry":      entry,
                    "current":    current_price,
                    "pnl_pct":    round(pnl_pct, 2),
                    "correct":    correct,
                    "period":     period,
                    "market_type": mtype,
                })
                if period == "24h":
                    item["checked_24h"]    = True
                    item["price_24h"]      = current_price
                    item["pnl_24h_pct"]    = round(pnl_pct, 2)
                else:
                    item["checked_72h"]    = True
                    item["price_72h"]      = current_price
                    item["pnl_72h_pct"]    = round(pnl_pct, 2)
                updates.append(item)

        if updates:
            save_history(history)
        if reports:
            send_outcome_email(reports)

    except Exception as e:
        logger.error(f"Outcome check error: {e}")

def send_outcome_email(reports):
    correct = [r for r in reports if r["correct"]]
    win_rate = round(len(correct) / len(reports) * 100) if reports else 0
    rows = ""
    for r in reports:
        col   = "#2d6a4f" if r["correct"] else "#e63946"
        tick  = "✓" if r["correct"] else "✗"
        rows += (
            f'<tr style="border-bottom:1px solid #eee">'
            f'<td style="padding:8px">{r["identifier"]}</td>'
            f'<td style="padding:8px">{r["direction"]}</td>'
            f'<td style="padding:8px">${r["entry"]:.4f}</td>'
            f'<td style="padding:8px">${r["current"]:.4f}</td>'
            f'<td style="padding:8px;color:{col};font-weight:bold">{r["pnl_pct"]:+.1f}%</td>'
            f'<td style="padding:8px;color:{col};font-weight:bold">{tick} {r["period"]}</td>'
            f'</tr>'
        )
    html = (
        '<html><body style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;padding:24px">'
        f'<h2 style="color:#1a1a2e">📊 Signal Report Card</h2>'
        f'<p style="color:#666">Checking how previous signals performed</p>'
        f'<div style="background:#f8f9ff;border-radius:8px;padding:16px;margin:16px 0;'
        f'display:flex;gap:24px">'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;'
        f'color:{"#2d6a4f" if win_rate >= 50 else "#e63946"}">{win_rate}%</div>'
        f'<div style="font-size:13px;color:#666">Signal Accuracy</div></div>'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;'
        f'color:#1a1a2e">{len(reports)}</div>'
        f'<div style="font-size:13px;color:#666">Signals Checked</div></div>'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;'
        f'color:#2d6a4f">{len(correct)}</div>'
        f'<div style="font-size:13px;color:#666">Correct</div></div>'
        f'</div>'
        f'<table style="width:100%;border-collapse:collapse;font-size:13px">'
        f'<tr style="background:#f0f2ff;font-weight:bold">'
        f'<th style="padding:8px;text-align:left">Asset</th>'
        f'<th style="padding:8px;text-align:left">Signal</th>'
        f'<th style="padding:8px;text-align:left">Entry</th>'
        f'<th style="padding:8px;text-align:left">Now</th>'
        f'<th style="padding:8px;text-align:left">P&L</th>'
        f'<th style="padding:8px;text-align:left">Result</th>'
        f'</tr>{rows}</table>'
        f'<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice.</p>'
        f'</body></html>'
    )
    send_email("📊 Signal Report Card", html,
               f"Signal accuracy: {win_rate}% ({len(correct)}/{len(reports)} correct)")

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
    .close-success { background: #d4edda; border: 1px solid #c3e6cb; color: #155724;
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
    .del-btn   { background: none; border: none; color: #e63946; cursor: pointer;
                 font-size: 12px; font-weight: bold; margin-top: 8px; padding: 0; width: auto; }
    .close-btn { background: none; border: none; color: #2d6a4f; cursor: pointer;
                 font-size: 12px; font-weight: bold; margin-top: 8px;
                 margin-right: 12px; padding: 0; width: auto; }
    .close-form { display: none; margin-top: 10px; background: #fff;
                  border: 1px solid #ddd; border-radius: 8px; padding: 12px; }
    .close-form input { margin-bottom: 8px; }
    .close-form button.confirm { background: #2d6a4f; color: white; border: none;
                                  padding: 8px 16px; border-radius: 6px; font-size: 13px;
                                  font-weight: bold; cursor: pointer; width: auto; }
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
    {% if close_success %}<div class="close-success">✅ Trade closed and P&L recorded.</div>{% endif %}
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
        <label>Market ID (from Myriad URL)</label>
        <input type="text" name="market_id" placeholder="optional but needed for sell alerts">
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
        <label>Coin (e.g. BTC, ETH, SOL)</label>
        <input type="text" name="ticker" placeholder="e.g. BTC" value="{{ prefill_ticker }}">
        <label>Action</label>
        <select name="side"><option value="BUY">BUY</option><option value="SELL_SHORT">SELL SHORT</option></select>
        <label>Entry Price (USD)</label>
        <input type="number" name="entry_price" step="0.01" min="0.01" placeholder="65000" required>
        <label>Amount in USD</label>
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
      <button type="button" class="close-btn" onclick="toggleClose('{{ pos.id }}')">✓ Close Trade</button>
      <form method="POST" action="/delete" style="display:inline">
        <input type="hidden" name="pos_id" value="{{ pos.id }}">
        <button type="submit" class="del-btn">✕ Remove</button>
      </form>
      <div class="close-form" id="close-form-{{ pos.id }}">
        <form method="POST" action="/close">
          <input type="hidden" name="pos_id" value="{{ pos.id }}">
          <label>Exit Price</label>
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
    entry  = float(pos.get("entry_price", 0))
    side   = pos.get("side", "BUY")
    ptype  = pos.get("type", "")
    if ptype == "myriad":
        amount     = float(pos.get("amount", 0))
        shares     = amount / entry if entry else 0
        pnl_dollar = (shares * exit_price) - amount
    elif ptype == "crypto":
        amount     = float(pos.get("amount", 0))
        shares     = amount / entry if entry else 0
        pnl_dollar = (shares * exit_price - amount) if side == "BUY" else (amount - shares * exit_price)
    else:
        shares     = float(pos.get("shares", 0))
        pnl_dollar = shares * (exit_price - entry) if side == "BUY" else shares * (entry - exit_price)
    pnl_pct = ((exit_price - entry) / entry * 100) if side == "BUY" else ((entry - exit_price) / entry * 100)
    closed = dict(pos)
    closed.update({
        "exit_price": exit_price,
        "exit_date":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "pnl_dollar": round(pnl_dollar, 2),
        "pnl_pct":    round(pnl_pct, 2),
        "win":        pnl_dollar > 0,
        "type_record": "trade",
    })
    return closed

# ── Flask routes ────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return render_template_string(FORM_HTML, success=False, close_success=False,
                                  positions=load_positions(), prefill_market="", prefill_ticker="")

@app.route("/log", methods=["GET", "POST"])
def log_trade():
    prefill_market = request.args.get("market", "")
    prefill_ticker = request.args.get("ticker", "")
    success = False
    if request.method == "POST":
        pos_type  = request.form.get("type", "myriad")
        positions = load_positions()
        base = {
            "id":         str(int(datetime.now(timezone.utc).timestamp())),
            "type":       pos_type,
            "side":       request.form.get("side", "BUY"),
            "entry_price":float(request.form.get("entry_price", 0)),
            "date":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "alerted_tp": False,
            "alerted_sl": False,
        }
        if pos_type == "myriad":
            base.update({"market":    request.form.get("market", "").strip(),
                         "amount":    float(request.form.get("amount", 0)),
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
    return render_template_string(FORM_HTML, success=success, close_success=False,
                                  positions=load_positions(),
                                  prefill_market=prefill_market, prefill_ticker=prefill_ticker)

@app.route("/close", methods=["POST"])
def close_trade():
    pos_id     = request.form.get("pos_id", "")
    exit_price = float(request.form.get("exit_price", 0))
    positions  = load_positions()
    remaining  = []
    for pos in positions:
        if pos.get("id") == pos_id and exit_price > 0:
            closed  = close_position(pos, exit_price)
            history = load_history()
            history.append(closed)
            save_history(history)
        else:
            remaining.append(pos)
    save_positions(remaining)
    return render_template_string(FORM_HTML, success=False, close_success=True,
                                  positions=remaining, prefill_market="", prefill_ticker="")

@app.route("/delete", methods=["POST"])
def delete_position():
    pos_id    = request.form.get("pos_id", "")
    positions = [p for p in load_positions() if p.get("id") != pos_id]
    save_positions(positions)
    return render_template_string(FORM_HTML, success=False, close_success=False,
                                  positions=positions, prefill_market="", prefill_ticker="")

# ══════════════════════════════════════════════════════════════════════════════
# CRYPTO BOT — CoinGecko, no API key, no geo-restriction
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
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)

def get_btc_1h_change() -> float:
    """UPGRADE 6: Get BTC 1-hour price change to filter altcoin signals."""
    try:
        r = requests.get(f"{COINGECKO_BASE}/coins/bitcoin/market_chart",
                         params={"vs_currency": "usd", "days": "1",
                                 "interval": "hourly"}, timeout=10)
        prices = r.json().get("prices", [])
        if len(prices) >= 2:
            prev  = float(prices[-2][1])
            curr  = float(prices[-1][1])
            change = ((curr - prev) / prev) * 100
            logger.info(f"BTC 1h change: {change:+.2f}%")
            return round(change, 2)
    except Exception as e:
        logger.error(f"BTC 1h change error: {e}")
    return 0.0

def get_all_crypto_coins():
    try:
        r = requests.get(f"{COINGECKO_BASE}/coins/markets",
                         params={"vs_currency": "usd", "order": "market_cap_desc",
                                 "per_page": 200, "page": 1, "sparkline": "false",
                                 "price_change_percentage": "1h,24h,7d"},
                         timeout=20)
        r.raise_for_status()
        coins = r.json()
        if not isinstance(coins, list):
            logger.error("CoinGecko unexpected response type")
            return []
        logger.info(f"CoinGecko: fetched {len(coins)} coins")
        return coins
    except Exception as e:
        logger.error(f"CoinGecko fetch error: {e}")
        return []

def prefilter_coins(coins, top_n=20):
    import math
    valid = [c for c in coins if isinstance(c, dict) and c.get("current_price", 0) > 0]
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
    gainers    = sorted(valid, key=lambda c: float(c.get("price_change_percentage_24h") or 0), reverse=True)[:6]
    losers     = sorted(valid, key=lambda c: float(c.get("price_change_percentage_24h") or 0))[:6]
    seen, result = set(), []
    for c in top_movers + gainers + losers:
        cid = c.get("id", "")
        if cid not in seen:
            seen.add(cid)
            result.append(c)
        if len(result) >= top_n:
            break
    return result[:top_n]

def get_coin_ohlc_sequential(coin_id: str) -> dict:
    """Fetch OHLC for a single coin — called sequentially with delay."""
    result = {}
    try:
        r = requests.get(f"{COINGECKO_BASE}/coins/{coin_id}/ohlc",
                         params={"vs_currency": "usd", "days": "90"}, timeout=15)
        if r.status_code == 429:
            logger.warning(f"Rate limit on {coin_id} — skipping OHLC")
            return result
        r.raise_for_status()
        ohlc = r.json()
        if not isinstance(ohlc, list) or len(ohlc) < 14:
            return result
        closes = [float(c[4]) for c in ohlc]
        result["rsi_14"] = calculate_rsi(closes)
        result["sma_50"] = round(sum(closes[-50:]) / 50, 4) if len(closes) >= 50 else None
    except Exception as e:
        logger.error(f"OHLC error {coin_id}: {e}")
    return result

def build_crypto_summaries_concurrent(top_coins: list) -> list:
    """
    Build coin summaries using data already in the markets response.
    Only fetch OHLC (sequentially, with delay) for coins that pass initial scoring.
    This avoids hammering CoinGecko's OHLC rate limit.
    """
    summaries = []
    for c in top_coins:
        cid     = c.get("id", "")
        symbol  = c.get("symbol", "").upper()
        price   = float(c.get("current_price") or 0)
        change_24h = float(c.get("price_change_percentage_24h") or 0)
        change_7d  = float(c.get("price_change_percentage_7d_in_currency") or
                           c.get("price_change_percentage_7d") or 0)
        # Derive weekly trend from 7d price change — no extra API call needed
        weekly_trend = "up" if change_7d > 2 else "down" if change_7d < -2 else "flat"
        summaries.append({
            "symbol":         f"{symbol}/USDT",
            "coin_id":        cid,
            "price":          price,
            "change_24h_pct": round(change_24h, 2),
            "change_7d_pct":  round(change_7d, 2),
            "volume_usd":     round(float(c.get("total_volume") or 0)),
            "market_cap":     round(float(c.get("market_cap") or 0)),
            "high_24h":       float(c.get("high_24h") or 0),
            "low_24h":        float(c.get("low_24h") or 0),
            "rsi_14":         None,  # filled below for qualifying coins only
            "rsi_signal":     "NO_DATA",
            "sma_50":         None,
            "vs_sma50":       "unknown",
            "weekly_trend":   weekly_trend,
        })

    # Pre-score with what we have — only fetch OHLC for coins scoring >= 1
    # This massively reduces OHLC calls (usually 2-4 instead of 12-20)
    needs_ohlc = []
    for s in summaries:
        rough_score = compute_conviction_score(s, 0.0, 50, "BUY" if s["change_24h_pct"] < 0 else "SELL")
        if rough_score >= 1:
            needs_ohlc.append(s)

    logger.info(f"Fetching OHLC sequentially for {len(needs_ohlc)} qualifying coins (3s gap each)...")
    for s in needs_ohlc:
        ohlc = get_coin_ohlc_sequential(s["coin_id"])
        rsi  = ohlc.get("rsi_14")
        sma50= ohlc.get("sma_50")
        if rsi:
            s["rsi_14"]    = rsi
            s["rsi_signal"]= "OVERSOLD" if rsi < 32 else "OVERBOUGHT" if rsi > 68 else "NEUTRAL"
        if sma50:
            s["sma_50"]  = sma50
            s["vs_sma50"]= "above" if s["price"] > sma50 else "below"
        time.sleep(3)  # 3-second gap — well within CoinGecko free tier limits

    return summaries

def compute_conviction_score(coin_data: dict, btc_1h_change: float,
                              fg_value: int, direction: str) -> int:
    """
    UPGRADE 5: Score each signal 0-10 based on independent confirming factors.
    Only alert if score >= 3.
    """
    score = 0
    rsi   = coin_data.get("rsi_14")
    change= float(coin_data.get("change_24h_pct") or 0)
    vol   = float(coin_data.get("volume_usd") or 0)
    trend = coin_data.get("weekly_trend", "unknown")
    sma50 = coin_data.get("sma_50")
    price = float(coin_data.get("price") or 0)

    if direction == "BUY":
        if rsi and rsi < 32:       score += 3  # Strong oversold
        elif rsi and rsi < 40:     score += 1  # Mild oversold
        if change < -8:            score += 2  # Big drop = potential bounce
        elif change < -4:          score += 1
        if vol > 100_000_000:      score += 1  # High volume = conviction
        if trend == "down" and rsi and rsi < 35: score += 1  # Oversold in downtrend
        if sma50 and price > sma50: score += 1  # Above SMA50 = bullish
        if fg_value < 30:          score -= 2  # Extreme fear = dangerous to buy
        if btc_1h_change < -4:     score -= 3  # BTC crashing = don't buy alts
    else:  # SELL/AVOID
        if rsi and rsi > 68:       score += 3
        elif rsi and rsi > 60:     score += 1
        if change > 8:             score += 2  # Parabolic move = sell signal
        elif change > 4:           score += 1
        if vol > 100_000_000:      score += 1
        if fg_value > 75:          score += 2  # Extreme greed = dangerous to hold
        if trend == "up" and rsi and rsi > 65: score += 1

    return max(0, score)

def analyze_crypto(crypto_data_list, fg: dict, btc_1h_change: float):
    oversold   = [d["symbol"] for d in crypto_data_list if d.get("rsi_14") and d["rsi_14"] < 32]
    overbought = [d["symbol"] for d in crypto_data_list if d.get("rsi_14") and d["rsi_14"] > 68]

    fg_warning = ""
    if fg["value"] < 25:
        fg_warning = f"\n⚠️ EXTREME FEAR ({fg['value']}/100) — be very cautious with BUY signals."
    elif fg["value"] > 80:
        fg_warning = f"\n⚠️ EXTREME GREED ({fg['value']}/100) — be very cautious, market may be overextended."

    btc_warning = ""
    if btc_1h_change < -4:
        btc_warning = f"\n⚠️ BTC dropped {btc_1h_change:.1f}% in the last hour — suppress altcoin BUY signals."

    hint = f"\nFear & Greed Index: {fg['value']}/100 ({fg['label']})"
    hint += f"\nBTC 1h change: {btc_1h_change:+.2f}%"
    hint += fg_warning + btc_warning
    if oversold:
        hint += f"\nOVERSOLD coins (RSI<32): {', '.join(oversold)}"
    if overbought:
        hint += f"\nOVERBOUGHT coins (RSI>68): {', '.join(overbought)}"

    prompt = (
        "You are a professional crypto trader analysing the market.\n\n"
        "Market context:\n" + hint + "\n\n"
        "Coin data (top movers, gainers, losers from top 200 by market cap):\n\n"
        + json.dumps(crypto_data_list, indent=2)
        + "\n\nSIGNAL CRITERIA (alert if ANY met):\n"
        "• RSI below 32 = oversold, potential BUY\n"
        "• RSI above 68 = overbought, potential SELL/AVOID\n"
        "• 24h gain >+7% with volume >$50M = momentum BUY\n"
        "• 24h loss >-8% with RSI<40 = oversold bounce candidate\n"
        "• Weekly trend UP with price above SMA50 = trend continuation BUY\n\n"
        "HARD RULES:\n"
        "• If BTC dropped more than 4% in the last hour, do NOT suggest BUY on any altcoin\n"
        "• If Fear & Greed is below 25, only flag the very strongest oversold setups\n"
        "• If Fear & Greed is above 80, flag overbought coins aggressively\n"
        "• Max 3 alerts. Never Low confidence. Weekly_trend 'down' reduces BUY conviction.\n\n"
        "For each alert use EXACTLY this format:\n\n"
        "ALERT: [SYMBOL]\n"
        "PRICE: $[price]\n"
        "SIGNAL: [BUY / SELL / AVOID]\n"
        "RSI: [value] — [OVERSOLD/NEUTRAL/OVERBOUGHT]\n"
        "24H CHANGE: [value]%\n"
        "WEEKLY TREND: [up/down/unknown]\n"
        "FEAR & GREED CONTEXT: [one sentence]\n"
        "REASONING: [2 sentences]\n"
        "SUGGESTED ENTRY: $[price]\n"
        "CONFIDENCE: [Medium / High]\n"
        "---\n\n"
        "If nothing meets criteria respond ONLY with: NO_ALERT"
    )
    try:
        resp = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1500,
                                      messages=[{"role": "user", "content": prompt}])
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude crypto error: {e}")
        return None

def check_crypto_positions():
    positions = [p for p in load_positions() if p.get("type") == "crypto"]
    if not positions:
        return
    alerts, updated_all = [], load_positions()
    for pos in positions:
        raw_symbol = pos.get("ticker", "").replace("USDT", "").replace("/", "").lower()
        if not raw_symbol:
            continue
        try:
            r = requests.get(f"{COINGECKO_BASE}/simple/price",
                             params={"ids": raw_symbol, "vs_currencies": "usd"}, timeout=10)
            current = float(r.json().get(raw_symbol, {}).get("usd", 0))
        except Exception:
            continue
        if not current:
            continue
        entry      = float(pos.get("entry_price", 0))
        amount     = float(pos.get("amount", 0))
        side       = pos.get("side", "BUY")
        pnl_pct    = ((current - entry) / entry * 100) if side == "BUY" else ((entry - current) / entry * 100)
        pnl_dollar = (amount / entry) * current - amount if side == "BUY" else amount - (amount / entry) * current
        alert_msg  = None
        if pnl_pct >= CRYPTO_TP_PCT and not pos.get("alerted_tp"):
            alert_msg = (f"TAKE PROFIT — {pos.get('ticker','')}\n"
                         f"Entry: ${entry:,.2f} | Now: ${current:,.2f}\n"
                         f"Gain: +{pnl_pct:.1f}% (${pnl_dollar:+.2f})\nACTION: Consider selling.")
            pos["alerted_tp"] = True
        elif pnl_pct <= -CRYPTO_SL_PCT and not pos.get("alerted_sl"):
            alert_msg = (f"STOP LOSS — {pos.get('ticker','')}\n"
                         f"Entry: ${entry:,.2f} | Now: ${current:,.2f}\n"
                         f"Loss: {pnl_pct:.1f}% (${pnl_dollar:+.2f})\nACTION: Consider cutting.")
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
    prune_dedup_cache()

    # UPGRADE 2 & 6: Get market context before analysis
    fg            = get_fear_and_greed()
    btc_1h_change = get_btc_1h_change()

    coins = get_all_crypto_coins()
    if not coins:
        logger.info("No CoinGecko data. Skipping.")
        return

    top_coins = prefilter_coins(coins, top_n=CRYPTO_TOP_N)

    # UPGRADE 3: Concurrent OHLC fetching
    logger.info(f"Fetching OHLC concurrently for {len(top_coins)} coins...")
    summaries = build_crypto_summaries_concurrent(top_coins)

    # UPGRADE 5: Pre-score coins and filter low-conviction ones before sending to Claude
    high_conviction = []
    for s in summaries:
        # Estimate likely direction from RSI
        rsi = s.get("rsi_14")
        if rsi:
            direction = "BUY" if rsi < 50 else "SELL"
        else:
            direction = "BUY"
        score = compute_conviction_score(s, btc_1h_change, fg["value"], direction)
        s["conviction_score"] = score
        if score >= 2:
            high_conviction.append(s)

    if not high_conviction:
        logger.info(f"No high-conviction crypto setups this cycle (F&G: {fg['value']}, BTC 1h: {btc_1h_change:+.1f}%)")
        return

    logger.info(f"{len(high_conviction)} high-conviction coins. Sending to Claude...")
    analysis = analyze_crypto(high_conviction, fg, btc_1h_change)

    if not analysis or analysis == "NO_ALERT":
        logger.info("Claude: no crypto signals this cycle.")
        return

    if "ALERT:" in analysis:
        # UPGRADE 1: Deduplication check
        new_alerts = []
        for block in analysis.split("---"):
            if "ALERT:" not in block:
                continue
            sym_line  = [l for l in block.splitlines() if l.startswith("ALERT:")]
            sig_line  = [l for l in block.splitlines() if l.startswith("SIGNAL:")]
            if not sym_line:
                continue
            symbol    = sym_line[0].replace("ALERT:", "").strip()
            direction = sig_line[0].replace("SIGNAL:", "").strip() if sig_line else "BUY"
            if is_duplicate(symbol, direction):
                logger.info(f"Duplicate suppressed: {symbol} {direction}")
                continue
            new_alerts.append(block)
            # UPGRADE 3: Log signal for outcome tracking
            price_line = [l for l in block.splitlines() if l.startswith("PRICE:")]
            price      = float(price_line[0].replace("PRICE:", "").replace("$", "").strip()) if price_line else 0
            log_signal(symbol, "crypto", direction, price, "crypto")

        if new_alerts:
            clean_analysis = "\n---\n".join(new_alerts)
            logger.info(f"{len(new_alerts)} new crypto signal(s). Sending email...")
            send_crypto_email(clean_analysis, fg)
        else:
            logger.info("All crypto signals were duplicates — suppressed.")

# ══════════════════════════════════════════════════════════════════════════════
# MYRIAD MARKETS BOT (every 30 minutes)
# ══════════════════════════════════════════════════════════════════════════════
def get_myriad_markets():
    try:
        r = requests.get(f"{MYRIAD_API}/markets",
                         params={"state": "open", "sort": "volume", "order": "desc", "limit": 100},
                         timeout=15)
        r.raise_for_status()
        data    = r.json()
        markets = data.get("data", data) if isinstance(data, dict) else data
        now     = datetime.now(timezone.utc)
        filtered, skip = [], {"perpetual": 0, "network": 0, "expiring": 0}
        for m in markets:
            if not m.get("expiresAt"):
                skip["perpetual"] += 1
                continue
            try:
                exp_dt = datetime.fromisoformat(m["expiresAt"].replace("Z", "+00:00"))
                if (exp_dt - now).total_seconds() < 6 * 3600:
                    skip["expiring"] += 1
                    continue
            except Exception:
                pass
            if str(m.get("networkId", "")) != "56":
                skip["network"] += 1
                continue
            filtered.append(m)
        logger.info(f"Myriad: {len(markets)} total → {len(filtered)} valid "
                    f"(skipped: {skip['perpetual']} perpetual, {skip['network']} wrong network, "
                    f"{skip['expiring']} expiring soon)")
        return filtered
    except Exception as e:
        logger.error(f"Myriad fetch error: {e}")
        return []

def get_myriad_price(market_id):
    try:
        r = requests.get(f"{MYRIAD_API}/markets/{market_id}", timeout=10)
        if r.status_code == 200:
            data   = r.json()
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
    summary    = []
    for m in markets[:20]:
        try:
            outcomes  = {o.get("title", "?"): o.get("price", "?") for o in m.get("outcomes", [])}
            slug      = m.get("slug", "")
            mkt_id    = str(m.get("id", ""))
            direct_url= f"https://myriad.markets/markets/{slug}" if slug else f"https://myriad.markets/markets/{mkt_id}"
            summary.append({"question": m.get("title", "?"),
                             "direct_url": direct_url,
                             "volume_usd": round(float(m.get("volume", 0) or 0), 2),
                             "outcomes":   outcomes,
                             "expires":    m.get("expiresAt", "?")})
        except Exception:
            continue

    prompt = (
        "You are a sharp, conservative prediction market analyst.\n\n"
        "Top active Myriad Markets on BNB Chain (perpetuals and near-expired excluded):\n\n"
        + json.dumps(summary, indent=2)
        + "\n\nFind markets where odds look clearly wrong, or YES+NO < $0.97 (pure arbitrage).\n"
        "Only flag genuine edges. Silence is better than noise.\n"
        "Include the direct_url from the data in your response.\n\n"
        "For each opportunity use EXACTLY this format:\n\n"
        "ALERT: [market question]\n"
        "MARKET URL: [direct_url]\n"
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
        logger.error(f"Claude Myriad error: {e}")
        return None

def check_myriad_positions():
    positions   = [p for p in load_positions() if p.get("type") == "myriad"]
    if not positions:
        return
    alerts, updated_all = [], load_positions()
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
                         f"Side: {side} | Entry: ${entry:.2f} | Now: ${current:.2f}\n"
                         f"Gain: +{pnl_pct:.0f}% (${pnl_dollar:+.2f})\nACTION: Consider selling.")
            pos["alerted_tp"] = True
        elif current <= entry * MYRIAD_SL_FRACTION and not pos.get("alerted_sl"):
            alert_msg = (f"STOP LOSS — Myriad\nMarket: {market}\n"
                         f"Side: {side} | Entry: ${entry:.2f} | Now: ${current:.2f}\n"
                         f"Loss: {pnl_pct:.0f}% (${pnl_dollar:+.2f})\nACTION: Consider cutting.")
            pos["alerted_sl"] = True
        if expires_at and not pos.get("alerted_expiry"):
            try:
                hours_left = (datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                              - datetime.now(timezone.utc)).total_seconds() / 3600
                if 0 < hours_left <= 48:
                    exp_msg = (f"EXPIRY WARNING — {hours_left:.0f}h left\nMarket: {market}\n"
                               f"P&L: {pnl_pct:+.0f}% (${pnl_dollar:+.2f})\n"
                               f"ACTION: Decide whether to sell or hold to resolution.")
                    alert_msg = (alert_msg + "\n\n---\n\n" + exp_msg) if alert_msg else exp_msg
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
        # UPGRADE 1: Deduplication
        new_alerts = []
        for block in analysis.split("---"):
            if "ALERT:" not in block:
                continue
            alert_line = [l for l in block.splitlines() if l.startswith("ALERT:")]
            play_line  = [l for l in block.splitlines() if l.startswith("SUGGESTED PLAY:")]
            if not alert_line:
                continue
            identifier = alert_line[0].replace("ALERT:", "").strip()[:80]
            direction  = play_line[0].replace("SUGGESTED PLAY:", "").strip()[:10] if play_line else "BUY YES"
            if is_duplicate(identifier, direction):
                logger.info(f"Myriad duplicate suppressed: {identifier[:40]}")
                continue
            new_alerts.append(block)

        if new_alerts:
            logger.info(f"{len(new_alerts)} new Myriad signal(s). Sending email...")
            send_myriad_email("\n---\n".join(new_alerts))
        else:
            logger.info("All Myriad signals were duplicates — suppressed.")

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
        latest   = list(rsi_data.keys())[0] if rsi_data else None
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
        "You are a sharp stock analyst.\n\n"
        "Daily stock data and news:\n\n"
        + json.dumps(stock_data_list, indent=2)
        + "\n\nRSI below 30 = oversold (BUY). RSI above 70 = overbought (SELL).\n"
        "Only alert when technical AND news agree. Be conservative.\n\n"
        "For each opportunity use EXACTLY this format:\n\n"
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
        logger.error(f"Claude stocks error: {e}")
        return None

def check_stock_positions():
    positions   = [p for p in load_positions() if p.get("type") == "stock"]
    if not positions:
        return
    alerts, updated_all = [], load_positions()
    for pos in positions:
        ticker  = pos.get("ticker", "")
        if not ticker:
            continue
        data    = get_stock_data(ticker)
        current = data.get("price", 0)
        if not current:
            continue
        entry      = float(pos.get("entry_price", 0))
        shares     = float(pos.get("shares", 0))
        side       = pos.get("side", "BUY")
        pnl_pct    = ((current - entry) / entry * 100) if side == "BUY" else ((entry - current) / entry * 100)
        pnl_dollar = shares * abs(current - entry) * (1 if side == "BUY" else -1)
        alert_msg  = None
        if pnl_pct >= STOCK_TP_PCT and not pos.get("alerted_tp"):
            alert_msg = (f"TAKE PROFIT — {ticker}\nEntry: ${entry:.2f} | Now: ${current:.2f}\n"
                         f"Gain: +{pnl_pct:.1f}% (${pnl_dollar:+.2f})\nACTION: Consider selling.")
            pos["alerted_tp"] = True
        elif pnl_pct <= -STOCK_SL_PCT and not pos.get("alerted_sl"):
            alert_msg = (f"STOP LOSS — {ticker}\nEntry: ${entry:.2f} | Now: ${current:.2f}\n"
                         f"Loss: {pnl_pct:.1f}% (${pnl_dollar:+.2f})\nACTION: Consider cutting.")
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
        # Deduplication for stocks
        new_alerts = []
        for block in analysis.split("---"):
            if "ALERT:" not in block:
                continue
            alert_line = [l for l in block.splitlines() if l.startswith("ALERT:")]
            sig_line   = [l for l in block.splitlines() if l.startswith("SIGNAL:")]
            if not alert_line:
                continue
            identifier = alert_line[0].replace("ALERT:", "").strip()[:20]
            direction  = sig_line[0].replace("SIGNAL:", "").strip()[:10] if sig_line else "BUY"
            if is_duplicate(identifier, direction):
                logger.info(f"Stock duplicate suppressed: {identifier}")
                continue
            new_alerts.append(block)
            price_line = [l for l in block.splitlines() if l.startswith("PRICE:")]
            price = float(price_line[0].replace("PRICE:", "").replace("$", "").strip()) if price_line else 0
            log_signal(identifier.split("—")[0].strip(), "stock", direction, price, "stock")

        if new_alerts:
            send_stock_email("\n---\n".join(new_alerts))

# ══════════════════════════════════════════════════════════════════════════════
# EMAIL
# ══════════════════════════════════════════════════════════════════════════════
def get_log_url():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    return ("https://" + domain) if domain and not domain.startswith("http") else domain

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
    cards   = ""
    for block in analysis.split("---"):
        block = block.strip()
        if not block or "ALERT:" not in block:
            continue
        inner, market_url = "", ""
        for line in block.splitlines():
            if line.startswith("ALERT:"):
                inner += f'<h3 style="margin:0 0 10px;color:#1a1a2e">{line.replace("ALERT:","").strip()}</h3>'
            elif line.startswith("MARKET URL:"):
                market_url = line.replace("MARKET URL:", "").strip()
            elif line.startswith("CURRENT ODDS:"):
                inner += f'<p style="margin:5px 0"><strong>Odds:</strong> {line.replace("CURRENT ODDS:","").strip()}</p>'
            elif line.startswith("WHY IT LOOKS MISPRICED:"):
                inner += f'<p style="margin:5px 0"><strong>Why:</strong> {line.replace("WHY IT LOOKS MISPRICED:","").strip()}</p>'
            elif line.startswith("SUGGESTED PLAY:"):
                val = line.replace("SUGGESTED PLAY:", "").strip()
                inner += f'<p style="margin:8px 0;font-size:16px;font-weight:bold;color:#2d6a4f">▶ {val}</p>'
            elif line.startswith("CONFIDENCE:"):
                level = line.replace("CONFIDENCE:", "").strip()
                col   = {"High":"#e63946","Medium":"#f4a261","Low":"#adb5bd"}.get(level,"#adb5bd")
                inner += f'<p style="margin:5px 0"><strong>Confidence:</strong> <span style="color:{col};font-weight:bold">{level}</span></p>'
        if inner:
            trade_btn = (f'<a href="{market_url}" style="display:inline-block;background:#7b2ff7;'
                         f'color:#fff;padding:10px 20px;text-decoration:none;border-radius:6px;'
                         f'font-size:14px;font-weight:bold;margin-top:12px;margin-right:8px">'
                         f'▶ Trade This Market</a>') if market_url else ""
            log_btn = (f'<a href="{log_url}/log" style="display:inline-block;background:#2d6a4f;'
                       f'color:#fff;padding:10px 20px;text-decoration:none;border-radius:6px;'
                       f'font-size:14px;font-weight:bold;margin-top:12px">📋 Log Trade</a>') if log_url else ""
            cards += (f'<div style="background:#f8f9ff;border-left:4px solid #7b2ff7;'
                      f'padding:16px;border-radius:6px;margin-bottom:16px">{inner}{trade_btn}{log_btn}</div>')
    html = ('<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
            '<h2 style="color:#1a1a2e">🔮 Myriad Markets — Opportunity Spotted</h2>'
            + cards +
            '<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice.</p>'
            '</body></html>')
    send_email("🔮 Myriad Alert — Opportunity Spotted", html,
               f"Myriad Opportunity\n\n{analysis}\n\nNot financial advice.")

def send_crypto_email(analysis, fg: dict = None):
    log_url  = get_log_url()
    fg_bar   = ""
    if fg:
        col = "#2d6a4f" if fg["value"] > 50 else "#e63946"
        fg_bar = (f'<div style="background:#f8f9ff;border-radius:8px;padding:12px;'
                  f'margin-bottom:16px;font-size:13px">'
                  f'😱 <strong>Fear & Greed:</strong> '
                  f'<span style="color:{col};font-weight:bold">{fg["value"]}/100 — {fg["label"]}</span></div>')
    cards = ""
    for block in analysis.split("---"):
        block = block.strip()
        if not block or "ALERT:" not in block:
            continue
        inner, symbol = "", ""
        field_map = {
            "ALERT:":               lambda v: f'<h3 style="margin:0 0 10px;color:#1a1a2e">{v}</h3>',
            "PRICE:":               lambda v: f'<p style="margin:5px 0"><strong>Price:</strong> {v}</p>',
            "SIGNAL:":              lambda v: f'<p style="margin:8px 0;font-size:18px;font-weight:bold;color:#f7931a">▶ {v}</p>',
            "RSI:":                 lambda v: f'<p style="margin:5px 0"><strong>RSI:</strong> {v}</p>',
            "24H CHANGE:":          lambda v: f'<p style="margin:5px 0"><strong>24h:</strong> {v}</p>',
            "WEEKLY TREND:":        lambda v: f'<p style="margin:5px 0"><strong>Weekly:</strong> {v}</p>',
            "FEAR & GREED CONTEXT:":lambda v: f'<p style="margin:5px 0;font-style:italic;color:#666">{v}</p>',
            "REASONING:":           lambda v: f'<p style="margin:5px 0"><strong>Why:</strong> {v}</p>',
            "SUGGESTED ENTRY:":     lambda v: f'<p style="margin:5px 0"><strong>Entry:</strong> {v}</p>',
            "CONFIDENCE:":          lambda v: f'<p style="margin:5px 0"><strong>Confidence:</strong> {v}</p>',
        }
        for line in block.splitlines():
            for prefix, tmpl in field_map.items():
                if line.startswith(prefix):
                    val = line.replace(prefix, "").strip()
                    if prefix == "ALERT:":
                        symbol = val.replace("/USDT", "").replace("USDT", "").strip()
                    inner += tmpl(val)
                    break
        if inner:
            binance_url = f"https://www.binance.com/en/trade/{symbol}_USDT" if symbol else "https://www.binance.com"
            trade_btn = (f'<a href="{binance_url}" style="display:inline-block;background:#f7931a;'
                         f'color:#fff;padding:10px 20px;text-decoration:none;border-radius:6px;'
                         f'font-size:14px;font-weight:bold;margin-top:12px;margin-right:8px">▶ Trade on Binance</a>')
            log_btn = (f'<a href="{log_url}/log?ticker={symbol}" style="display:inline-block;'
                       f'background:#2d6a4f;color:#fff;padding:10px 20px;text-decoration:none;'
                       f'border-radius:6px;font-size:14px;font-weight:bold;margin-top:12px">📋 Log Trade</a>') if log_url else ""
            cards += (f'<div style="background:#fff8f0;border-left:4px solid #f7931a;'
                      f'padding:16px;border-radius:6px;margin-bottom:16px">{inner}{trade_btn}{log_btn}</div>')
    html = ('<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
            '<h2 style="color:#1a1a2e">₿ Crypto Signal Alert</h2>'
            + fg_bar + cards +
            '<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice. '
            'Crypto is highly volatile.</p></body></html>')
    send_email("₿ Crypto Signal — " + analysis.split("\n")[0].replace("ALERT:", "").strip()[:40],
               html, f"Crypto Signal\n\n{analysis}\n\nNot financial advice.")

def send_stock_email(analysis):
    log_url = get_log_url()
    cards   = ""
    for block in analysis.split("---"):
        block = block.strip()
        if not block or "ALERT:" not in block:
            continue
        inner, ticker = "", ""
        field_map = {
            "ALERT:":            lambda v: f'<h3 style="margin:0 0 10px;color:#1a1a2e">{v}</h3>',
            "PRICE:":            lambda v: f'<p style="margin:5px 0"><strong>Price:</strong> {v}</p>',
            "SIGNAL:":           lambda v: f'<p style="margin:8px 0;font-size:18px;font-weight:bold;color:#4361ee">▶ {v}</p>',
            "TECHNICAL REASON:": lambda v: f'<p style="margin:5px 0"><strong>Technical:</strong> {v}</p>',
            "NEWS REASON:":      lambda v: f'<p style="margin:5px 0"><strong>News:</strong> {v}</p>',
            "SUGGESTED ENTRY:":  lambda v: f'<p style="margin:5px 0"><strong>Entry:</strong> {v}</p>',
            "CONFIDENCE:":       lambda v: f'<p style="margin:5px 0"><strong>Confidence:</strong> {v}</p>',
        }
        for line in block.splitlines():
            for prefix, tmpl in field_map.items():
                if line.startswith(prefix):
                    val = line.replace(prefix, "").strip()
                    if prefix == "ALERT:":
                        ticker = val.split("—")[0].strip()
                    inner += tmpl(val)
                    break
        if inner:
            log_btn = (f'<a href="{log_url}/log?ticker={ticker}" style="display:inline-block;'
                       f'background:#2d6a4f;color:#fff;padding:8px 16px;text-decoration:none;'
                       f'border-radius:6px;font-size:13px;font-weight:bold;margin-top:10px">'
                       f'📋 Log this trade</a>') if log_url else ""
            cards += (f'<div style="background:#f8f9ff;border-left:4px solid #4361ee;'
                      f'padding:16px;border-radius:6px;margin-bottom:16px">{inner}{log_btn}</div>')
    html = ('<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
            '<h2 style="color:#1a1a2e">📈 Daily Stock Signal</h2>'
            + cards +
            '<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice.</p>'
            '</body></html>')
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
    html = ('<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
            '<h2 style="color:#1a1a2e">📊 Position Alert — Action Required</h2>'
            + cards +
            '<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice.</p>'
            '</body></html>')
    send_email("📊 Position Alert — Action Required", html,
               "Position Alert\n\n" + "\n\n---\n\n".join(alerts))

# ══════════════════════════════════════════════════════════════════════════════
# REPORTING
# ══════════════════════════════════════════════════════════════════════════════
def build_report_html(stats, all_time_pnl):
    pnl_col   = "#2d6a4f" if stats["total_pnl"] >= 0 else "#e63946"
    type_labels = {"myriad": "🔮 Myriad", "crypto": "₿ Crypto", "stock": "📈 Stocks"}
    type_rows = ""
    for pt, data in stats["by_type"].items():
        col = "#2d6a4f" if data["pnl"] >= 0 else "#e63946"
        type_rows += (f'<tr><td style="padding:8px;border-bottom:1px solid #eee">{type_labels.get(pt,pt)}</td>'
                      f'<td style="padding:8px;border-bottom:1px solid #eee;text-align:center">{data["count"]}</td>'
                      f'<td style="padding:8px;border-bottom:1px solid #eee;text-align:right;'
                      f'color:{col};font-weight:bold">${data["pnl"]:+.2f}</td></tr>')
    def trade_name(t):
        return t.get("market", t.get("ticker", "?"))[:50]
    best  = stats["best_trade"]
    worst = stats["worst_trade"]
    return (
        '<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
        f'<h2 style="color:#1a1a2e">{"📈" if stats["total_pnl"] >= 0 else "📉"} {stats["period"]} Report</h2>'
        f'<div style="background:#f8f9ff;border-radius:12px;padding:20px;margin:20px 0;'
        f'display:flex;justify-content:space-between;flex-wrap:wrap;gap:12px">'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;color:{pnl_col}">'
        f'${stats["total_pnl"]:+.2f}</div><div style="color:#666;font-size:13px">P&L</div></div>'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;color:#1a1a2e">'
        f'{stats["win_rate"]}%</div><div style="color:#666;font-size:13px">Win Rate</div></div>'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;color:#1a1a2e">'
        f'{stats["total_trades"]}</div><div style="color:#666;font-size:13px">Trades</div></div>'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;color:#2d6a4f">'
        f'{stats["wins"]}</div><div style="color:#666;font-size:13px">Wins</div></div>'
        f'<div style="text-align:center"><div style="font-size:28px;font-weight:bold;color:#e63946">'
        f'{stats["losses"]}</div><div style="color:#666;font-size:13px">Losses</div></div></div>'
        f'<h3 style="color:#1a1a2e;margin:20px 0 10px">By Market</h3>'
        f'<table style="width:100%;border-collapse:collapse">'
        f'<tr style="background:#f0f2ff"><th style="padding:8px;text-align:left">Market</th>'
        f'<th style="padding:8px;text-align:center">Trades</th>'
        f'<th style="padding:8px;text-align:right">P&L</th></tr>{type_rows}</table>'
        f'<div style="display:flex;gap:12px;margin:20px 0;flex-wrap:wrap">'
        f'<div style="flex:1;min-width:200px;background:#f0fff4;border-left:4px solid #2d6a4f;'
        f'padding:14px;border-radius:6px"><div style="font-size:11px;font-weight:bold;color:#2d6a4f">BEST TRADE</div>'
        f'<div style="font-size:13px;margin:4px 0">{trade_name(best)}</div>'
        f'<div style="font-size:20px;font-weight:bold;color:#2d6a4f">${best.get("pnl_dollar",0):+.2f}</div></div>'
        f'<div style="flex:1;min-width:200px;background:#fff5f5;border-left:4px solid #e63946;'
        f'padding:14px;border-radius:6px"><div style="font-size:11px;font-weight:bold;color:#e63946">WORST TRADE</div>'
        f'<div style="font-size:13px;margin:4px 0">{trade_name(worst)}</div>'
        f'<div style="font-size:20px;font-weight:bold;color:#e63946">${worst.get("pnl_dollar",0):+.2f}</div></div></div>'
        f'<div style="background:#1a1a2e;color:white;border-radius:8px;padding:16px;text-align:center">'
        f'<div style="font-size:13px;opacity:0.7;margin-bottom:4px">ALL-TIME P&L</div>'
        f'<div style="font-size:32px;font-weight:bold;color:{"#4ade80" if all_time_pnl >= 0 else "#f87171"}">'
        f'${all_time_pnl:+.2f}</div></div>'
        '<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice.</p>'
        '</body></html>'
    )

def filter_trades_by_period(trades, period):
    now    = datetime.now(timezone.utc)
    result = []
    for t in trades:
        if t.get("type_record") != "trade":
            continue
        try:
            exit_date = datetime.strptime(t.get("exit_date", ""), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_map  = {"daily": 1, "weekly": 7, "monthly": 31}
            if (now - exit_date).days < days_map.get(period, 1):
                result.append(t)
        except Exception:
            continue
    return result

def send_report(period):
    logger.info(f"Generating {period} report...")
    history = load_history()
    if not history:
        logger.info("No history yet.")
        return
    trades = filter_trades_by_period(history, period)
    if not trades:
        logger.info(f"No closed trades in {period} period.")
        return
    all_time_pnl = sum(t.get("pnl_dollar", 0) for t in history if t.get("type_record") == "trade")
    wins  = [t for t in trades if t.get("win")]
    by_type = {}
    for t in trades:
        pt = t.get("type", "unknown")
        by_type.setdefault(pt, {"count": 0, "pnl": 0})
        by_type[pt]["count"] += 1
        by_type[pt]["pnl"]   += t.get("pnl_dollar", 0)
    labels = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
    stats  = {
        "period":       f"{labels[period]} ({datetime.now(timezone.utc).strftime('%d %b %Y')})",
        "total_trades": len(trades),
        "wins":         len(wins),
        "losses":       len(trades) - len(wins),
        "win_rate":     round(len(wins) / len(trades) * 100, 1),
        "total_pnl":    round(sum(t.get("pnl_dollar", 0) for t in trades), 2),
        "best_trade":   max(trades, key=lambda t: t.get("pnl_dollar", 0)),
        "worst_trade":  min(trades, key=lambda t: t.get("pnl_dollar", 0)),
        "by_type":      by_type,
    }
    html  = build_report_html(stats, all_time_pnl)
    label = labels[period]
    pnl   = stats["total_pnl"]
    send_email(f'{"📈" if pnl >= 0 else "📉"} {label} Report — ${pnl:+.2f} P&L', html,
               f"{label} Report\nP&L: ${pnl:+.2f} | Win rate: {stats['win_rate']}% | "
               f"Trades: {stats['total_trades']}\nAll-time P&L: ${all_time_pnl:+.2f}")
    logger.info(f"{label} report sent.")

# ── Entry point ─────────────────────────────────────────────────────────────
def run_flask():
    """UPGRADE 4: Waitress production WSGI server — replaces Flask dev server."""
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Starting Waitress production server on port {port}")
    serve(app, host="0.0.0.0", port=port, threads=4)

if __name__ == "__main__":
    logger.info("═══════════════════════════════════════")
    logger.info("  Combined Signal Bot v2 — UPGRADED")
    logger.info("═══════════════════════════════════════")
    logger.info(f"Myriad + Crypto: every {CHECK_INTERVAL_MINUTES} min")
    logger.info(f"Stocks: daily at {STOCK_SCAN_HOUR_UTC}:00 UTC (6am Bangkok)")
    logger.info(f"Stock watchlist: {STOCK_WATCHLIST}")
    logger.info("Upgrades active: dedup | fear&greed | concurrent | waitress | conviction | btc-filter | outcomes | weekly-trend")

    threading.Thread(target=run_flask, daemon=True).start()

    # Run all cycles immediately on startup
    run_myriad_cycle()
    run_crypto_cycle()
    run_stock_cycle()
    check_signal_outcomes()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_myriad_cycle,       "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.add_job(run_crypto_cycle,       "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.add_job(run_stock_cycle,        "cron",     hour=STOCK_SCAN_HOUR_UTC, minute=0)
    scheduler.add_job(check_signal_outcomes,  "interval", hours=6)
    scheduler.add_job(lambda: send_report("daily"),   "cron", hour=23, minute=0)
    scheduler.add_job(lambda: send_report("weekly"),  "cron", day_of_week="mon", hour=23, minute=30)
    scheduler.add_job(lambda: send_report("monthly"), "cron", day=1, hour=23, minute=45)

    logger.info("All jobs scheduled. Bot is live.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")


