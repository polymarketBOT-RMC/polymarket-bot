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
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))

# Take profit when odds reach this multiple of entry price (default: 1.8x = 80% gain)
TAKE_PROFIT_MULTIPLIER = float(os.environ.get("TAKE_PROFIT_MULTIPLIER", "1.8"))
# Stop loss when odds drop to this fraction of entry price (default: 0.5 = 50% loss)
STOP_LOSS_FRACTION     = float(os.environ.get("STOP_LOSS_FRACTION", "0.5"))
# Warn when market expires within this many hours
EXPIRY_WARNING_HOURS   = int(os.environ.get("EXPIRY_WARNING_HOURS", "48"))

client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
app      = Flask(__name__)
MYRIAD_API  = "https://api-v2.myriadprotocol.com"
JSONBIN_URL = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
JSONBIN_HEADERS = {
    "X-Master-Key": JSONBIN_API_KEY,
    "Content-Type": "application/json"
}

# ── HTMLform template ───────────────────────────────────────────────────────
FORM_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Log Trade</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; background: #f0f2f5; display: flex;
           justify-content: center; align-items: center; min-height: 100vh; padding: 20px; }
    .card { background: white; border-radius: 12px; padding: 32px; max-width: 480px;
            width: 100%; box-shadow: 0 4px 20px rgba(0,0,0,0.1); }
    h2 { color: #1a1a2e; margin-bottom: 8px; }
    p.sub { color: #666; font-size: 14px; margin-bottom: 24px; }
    label { display: block; font-size: 13px; font-weight: bold; color: #444; margin-bottom: 6px; }
    input, select { width: 100%; padding: 10px 14px; border: 1px solid #ddd;
                    border-radius: 8px; font-size: 15px; margin-bottom: 18px; }
    button { width: 100%; background: #4361ee; color: white; border: none;
             padding: 14px; border-radius: 8px; font-size: 16px; font-weight: bold;
             cursor: pointer; }
    button:hover { background: #3451d1; }
    .success { background: #d4edda; border: 1px solid #c3e6cb; color: #155724;
               padding: 16px; border-radius: 8px; margin-bottom: 20px; }
    .positions { margin-top: 32px; }
    .positions h3 { color: #1a1a2e; margin-bottom: 16px; font-size: 16px; }
    .pos-card { background: #f8f9ff; border-left: 4px solid #4361ee; padding: 14px;
                border-radius: 6px; margin-bottom: 12px; font-size: 14px; }
    .pos-card .market { font-weight: bold; color: #1a1a2e; margin-bottom: 6px; }
    .pos-card .details { color: #555; }
    .pos-card .details span { margin-right: 16px; }
    .delete-btn { background: none; border: none; color: #e63946; cursor: pointer;
                  font-size: 13px; font-weight: bold; padding: 0; width: auto;
                  margin-top: 8px; }
  </style>
</head>
<body>
<div class="card">
  <h2>📊 Log a Trade</h2>
  <p class="sub">Record a position so the bot can alert you when to sell.</p>

  {% if success %}
  <div class="success">✅ Position saved! The bot will now monitor this trade.</div>
  {% endif %}

  <form method="POST" action="/log">
    <label>Market Question</label>
    <input type="text" name="market" placeholder="e.g. Will Bitcoin exceed $100k by June?" required
           value="{{ prefill_market }}">

    <label>Side Bought</label>
    <select name="side">
      <option value="YES">YES</option>
      <option value="NO">NO</option>
    </select>

    <label>Entry Price (in $, e.g. 0.34)</label>
    <input type="number" name="entry_price" step="0.01" min="0.01" max="0.99"
           placeholder="0.34" required>

    <label>Amount Invested (in USDT/USDC)</label>
    <input type="number" name="amount" step="1" min="1" placeholder="50" required>

    <label>Market ID (optional — paste from Myriad URL)</label>
    <input type="text" name="market_id" placeholder="e.g. 123456 (from myriad.markets/markets/123456)">

    <button type="submit">Save Position</button>
  </form>

  {% if positions %}
  <div class="positions">
    <h3>📋 Open Positions ({{ positions|length }})</h3>
    {% for pos in positions %}
    <div class="pos-card">
      <div class="market">{{ pos.market }}</div>
      <div class="details">
        <span><strong>{{ pos.side }}</strong> @ ${{ pos.entry_price }}</span>
        <span>${{ pos.amount }} invested</span>
        <span>{{ pos.date }}</span>
      </div>
      <form method="POST" action="/delete" style="display:inline">
        <input type="hidden" name="pos_id" value="{{ pos.id }}">
        <button type="submit" class="delete-btn">✕ Remove</button>
      </form>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>
</body>
</html>
"""

# ── JSONBin storage helpers ─────────────────────────────────────────────────
def load_positions():
    try:
        resp = requests.get(JSONBIN_URL, headers=JSONBIN_HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("record", {}).get("positions", [])
        return []
    except Exception as e:
        logger.error(f"Failed to load positions: {e}")
        return []


def save_positions(positions):
    try:
        resp = requests.put(
            JSONBIN_URL,
            headers=JSONBIN_HEADERS,
            json={"positions": positions},
            timeout=10
        )
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Failed to save positions: {e}")
        return False


# ── Flask routes ────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    positions = load_positions()
    return render_template_string(FORM_HTML, success=False,
                                  positions=positions, prefill_market="")


@app.route("/log", methods=["GET", "POST"])
def log_trade():
    prefill_market = request.args.get("market", "")
    success = False

    if request.method == "POST":
        positions = load_positions()
        new_pos = {
            "id": str(int(datetime.now(timezone.utc).timestamp())),
            "market": request.form.get("market", "").strip(),
            "side": request.form.get("side", "YES"),
            "entry_price": float(request.form.get("entry_price", 0)),
            "amount": float(request.form.get("amount", 0)),
            "market_id": request.form.get("market_id", "").strip(),
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "alerted_tp": False,
            "alerted_sl": False,
            "alerted_expiry": False,
        }
        positions.append(new_pos)
        save_positions(positions)
        success = True
        prefill_market = ""

    positions = load_positions()
    return render_template_string(FORM_HTML, success=success,
                                  positions=positions, prefill_market=prefill_market)


@app.route("/delete", methods=["POST"])
def delete_position():
    pos_id = request.form.get("pos_id", "")
    positions = load_positions()
    positions = [p for p in positions if p.get("id") != pos_id]
    save_positions(positions)
    return render_template_string(FORM_HTML, success=False,
                                  positions=positions, prefill_market="")


# ── Myriad market data ──────────────────────────────────────────────────────
def get_markets(limit=50):
    try:
        resp = requests.get(
            f"{MYRIAD_API}/markets",
            params={"state": "open", "sort": "volume", "order": "desc", "limit": limit},
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data
    except Exception as e:
        logger.error(f"Failed to fetch markets: {e}")
        return []


def get_market_price(market_id):
    """Fetch current YES price for a specific market by ID."""
    try:
        resp = requests.get(f"{MYRIAD_API}/markets/{market_id}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            market = data.get("data", data)
            outcomes = market.get("outcomes", [])
            for o in outcomes:
                if o.get("title", "").upper() in ("YES", "TRUE"):
                    return float(o.get("price", 0)), market.get("expiresAt")
    except Exception as e:
        logger.error(f"Failed to fetch market {market_id}: {e}")
    return None, None


# ── Buy opportunity analysis ────────────────────────────────────────────────
def analyze_markets(markets):
    if not markets:
        return None

    summary = []
    for m in markets[:20]:
        try:
            outcomes = m.get("outcomes", [])
            outcome_prices = {o.get("title", "?"): o.get("price", "?") for o in outcomes}
            summary.append({
                "question": m.get("title", "?"),
                "volume_usd": round(float(m.get("volume", 0) or 0), 2),
                "outcomes": outcome_prices,
                "expires": m.get("expiresAt", "?"),
            })
        except Exception:
            continue

    prompt = (
        "You are a sharp, conservative prediction market analyst helping a retail investor in Thailand.\n\n"
        "Below are the top active markets on Myriad Markets right now, ordered by trading volume:\n\n"
        + json.dumps(summary, indent=2)
        + "\n\nYour job:\n"
        "1. Identify any markets where the current odds look clearly WRONG based on your knowledge.\n"
        "2. Check whether YES + NO outcome prices sum to less than $0.97 — flag as arbitrage immediately.\n"
        "3. Flag only markets where you are genuinely confident there is an edge. Silence is better than noise.\n\n"
        "For every opportunity found, respond in EXACTLY this format:\n\n"
        "ALERT: [full market question]\n"
        "CURRENT ODDS: YES = $[price] / NO = $[price]\n"
        "WHY IT LOOKS MISPRICED: [2 sentences max]\n"
        "SUGGESTED PLAY: BUY [YES or NO] at $[price]\n"
        "CONFIDENCE: [Low / Medium / High]\n"
        "---\n\n"
        "If NO clear opportunities, respond with ONLY: NO_ALERT"
    )

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return None


# ── Sell / position monitoring ──────────────────────────────────────────────
def check_positions():
    positions = load_positions()
    if not positions:
        return

    alerts = []
    updated = False

    for pos in positions:
        market_id = pos.get("market_id", "").strip()
        if not market_id:
            continue  # Can't monitor without an ID

        entry   = float(pos.get("entry_price", 0))
        side    = pos.get("side", "YES")
        amount  = float(pos.get("amount", 0))
        market  = pos.get("market", "?")

        current_yes_price, expires_at = get_market_price(market_id)
        if current_yes_price is None:
            continue

        # Flip price for NO positions (NO price = 1 - YES price)
        current_price = current_yes_price if side == "YES" else (1.0 - current_yes_price)

        # Calculate P&L
        pnl_pct = ((current_price - entry) / entry) * 100
        shares   = amount / entry
        current_value = shares * current_price
        pnl_dollar = current_value - amount

        alert_msg = None

        # Take profit check
        if current_price >= entry * TAKE_PROFIT_MULTIPLIER and not pos.get("alerted_tp"):
            alert_msg = (
                f"TAKE PROFIT\n"
                f"Market: {market}\n"
                f"Side: {side} | Entry: ${entry:.2f} | Current: ${current_price:.2f}\n"
                f"Gain: +{pnl_pct:.0f}% (${pnl_dollar:+.2f})\n"
                f"SUGGESTED ACTION: SELL NOW to lock in profit."
            )
            pos["alerted_tp"] = True
            updated = True

        # Stop loss check
        elif current_price <= entry * STOP_LOSS_FRACTION and not pos.get("alerted_sl"):
            alert_msg = (
                f"STOP LOSS WARNING\n"
                f"Market: {market}\n"
                f"Side: {side} | Entry: ${entry:.2f} | Current: ${current_price:.2f}\n"
                f"Loss: {pnl_pct:.0f}% (${pnl_dollar:+.2f})\n"
                f"SUGGESTED ACTION: Consider cutting this position to limit further losses."
            )
            pos["alerted_sl"] = True
            updated = True

        # Expiry warning check
        if expires_at and not pos.get("alerted_expiry"):
            try:
                exp_dt  = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                now_dt  = datetime.now(timezone.utc)
                hours_left = (exp_dt - now_dt).total_seconds() / 3600
                if 0 < hours_left <= EXPIRY_WARNING_HOURS:
                    expiry_alert = (
                        f"EXPIRY WARNING — {hours_left:.0f} hours left\n"
                        f"Market: {market}\n"
                        f"Side: {side} | Entry: ${entry:.2f} | Current: ${current_price:.2f}\n"
                        f"Current P&L: {pnl_pct:+.0f}% (${pnl_dollar:+.2f})\n"
                        f"SUGGESTED ACTION: Decide whether to sell before expiry or hold to resolution."
                    )
                    if alert_msg:
                        alert_msg += "\n\n---\n\n" + expiry_alert
                    else:
                        alert_msg = expiry_alert
                    pos["alerted_expiry"] = True
                    updated = True
            except Exception:
                pass

        if alert_msg:
            alerts.append(alert_msg)

    if updated:
        save_positions(positions)

    if alerts:
        logger.info(f"Sending {len(alerts)} sell alert(s)...")
        send_sell_email(alerts)


# ── Email helpers ───────────────────────────────────────────────────────────
def build_buy_html(analysis_text):
    cards_html = ""
    for block in analysis_text.split("---"):
        block = block.strip()
        if not block:
            continue
        card_lines = ""
        for line in block.splitlines():
            if line.startswith("ALERT:"):
                val = line.replace("ALERT:", "").strip()
                card_lines += f'<h3 style="margin:0 0 12px;color:#1a1a2e">{val}</h3>'
            elif line.startswith("CURRENT ODDS:"):
                val = line.replace("CURRENT ODDS:", "").strip()
                card_lines += f'<p style="margin:6px 0"><strong>Odds:</strong> {val}</p>'
            elif line.startswith("WHY IT LOOKS MISPRICED:"):
                val = line.replace("WHY IT LOOKS MISPRICED:", "").strip()
                card_lines += f'<p style="margin:6px 0"><strong>Why:</strong> {val}</p>'
            elif line.startswith("SUGGESTED PLAY:"):
                val = line.replace("SUGGESTED PLAY:", "").strip()
                card_lines += (
                    f'<p style="margin:6px 0;font-size:16px"><strong>Play:</strong> '
                    f'<span style="color:#2d6a4f;font-weight:bold">{val}</span></p>'
                )
            elif line.startswith("CONFIDENCE:"):
                level = line.replace("CONFIDENCE:", "").strip()
                colour = {"High": "#e63946", "Medium": "#f4a261", "Low": "#adb5bd"}.get(level, "#adb5bd")
                card_lines += (
                    f'<p style="margin:6px 0"><strong>Confidence:</strong> '
                    f'<span style="color:{colour};font-weight:bold">{level}</span></p>'
                )
        if card_lines:
            cards_html += (
                f'<div style="background:#f8f9ff;border-left:4px solid #4361ee;'
                f'padding:16px;border-radius:6px;margin-bottom:16px">{card_lines}</div>'
            )

    log_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "your-railway-url.railway.app")
    if not log_url.startswith("http"):
        log_url = "https://" + log_url

    return (
        '<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
        '<h2 style="color:#1a1a2e">🎯 Myriad Markets — Buy Opportunity</h2>'
        + cards_html
        + f'<a href="{log_url}/log" style="display:inline-block;background:#2d6a4f;'
        'color:#fff;padding:12px 28px;text-decoration:none;border-radius:6px;'
        'font-weight:bold;margin-top:8px;margin-right:12px">📋 Log This Trade</a>'
        '<a href="https://myriad.markets" style="display:inline-block;background:#4361ee;'
        'color:#fff;padding:12px 28px;text-decoration:none;border-radius:6px;'
        'font-weight:bold;margin-top:8px">Open Myriad Markets</a>'
        '<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice.</p>'
        '</body></html>'
    )


def build_sell_html(alerts):
    cards_html = ""
    for alert in alerts:
        lines = alert.strip().splitlines()
        title = lines[0] if lines else "Alert"
        body_lines = lines[1:] if len(lines) > 1 else []

        if "TAKE PROFIT" in title:
            border = "#2d6a4f"
            emoji  = "💰"
        elif "STOP LOSS" in title:
            border = "#e63946"
            emoji  = "🛑"
        else:
            border = "#f4a261"
            emoji  = "⏰"

        body_html = "".join(
            f'<p style="margin:4px 0;font-size:14px;color:#333">{line}</p>'
            for line in body_lines if line.strip() and line != "---"
        )
        cards_html += (
            f'<div style="background:#fff;border-left:5px solid {border};'
            f'padding:16px;border-radius:6px;margin-bottom:16px;'
            f'box-shadow:0 2px 8px rgba(0,0,0,0.06)">'
            f'<h3 style="margin:0 0 10px;color:#1a1a2e">{emoji} {title}</h3>'
            f'{body_html}</div>'
        )

    return (
        '<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">'
        '<h2 style="color:#1a1a2e">📊 Position Alert — Action Required</h2>'
        + cards_html
        + '<a href="https://myriad.markets" style="display:inline-block;background:#4361ee;'
        'color:#fff;padding:12px 28px;text-decoration:none;border-radius:6px;'
        'font-weight:bold;margin-top:8px">Go to Myriad Markets</a>'
        '<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice.</p>'
        '</body></html>'
    )


def send_email(subject, html, plain):
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": "Prediction Bot <onboarding@resend.dev>",
                "to": [ALERT_EMAIL],
                "subject": subject,
                "html": html,
                "text": plain
            },
            timeout=15
        )
        if resp.status_code in (200, 201):
            logger.info("Email sent successfully.")
        else:
            logger.error(f"Resend error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")


def send_buy_email(analysis_text):
    send_email(
        subject="🎯 Myriad Markets — Buy Opportunity Spotted",
        html=build_buy_html(analysis_text),
        plain=f"Buy Opportunity\n\n{analysis_text}\n\nGo to https://myriad.markets\nNot financial advice."
    )


def send_sell_email(alerts):
    plain = "Position Alert\n\n" + "\n\n---\n\n".join(alerts) + "\n\nGo to https://myriad.markets"
    send_email(
        subject="📊 Position Alert — Action May Be Required",
        html=build_sell_html(alerts),
        plain=plain
    )


# ── Main cycle ──────────────────────────────────────────────────────────────
def run_cycle():
    logger.info("Starting market check cycle")

    # 1. Check open positions for sell signals
    logger.info("Checking open positions...")
    check_positions()

    # 2. Scan for new buy opportunities
    markets = get_markets()
    if not markets:
        logger.info("No market data. Skipping buy scan.")
        return
    logger.info(f"Fetched {len(markets)} markets. Analysing...")
    analysis = analyze_markets(markets)
    if not analysis:
        return
    if analysis == "NO_ALERT":
        logger.info("No buy opportunities this cycle.")
        return
    if "ALERT:" in analysis:
        logger.info("Buy opportunity found! Sending email...")
        send_buy_email(analysis)
    else:
        logger.info(f"Unexpected Claude response: {analysis[:80]}")


# ── Entry point ─────────────────────────────────────────────────────────────
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    logger.info("Myriad Markets Bot (with sell alerts) starting up...")
    logger.info(f"Checking every {CHECK_INTERVAL_MINUTES} minutes.")
    logger.info(f"Alerts going to {ALERT_EMAIL}")

    # Run Flask in background thread so scheduler can also run
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Trade logging web form is live.")

    run_cycle()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_cycle, "interval", minutes=CHECK_INTERVAL_MINUTES)
    logger.info("Scheduler started. Bot is live.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")

