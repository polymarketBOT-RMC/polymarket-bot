import os
import smtplib
import json
import requests
import anthropic
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from apscheduler.schedulers.blocking import BlockingScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ── Config (set these as environment variables in Railway) ──────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS     = os.environ["GMAIL_ADDRESS"]       # your Gmail address
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"] # Gmail App Password (not your login password)
ALERT_EMAIL       = os.environ["ALERT_EMAIL"]         # where to send alerts (can be same as GMAIL_ADDRESS)
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

GAMMA_API = "https://gamma-api.polymarket.com"


def get_polymarket_markets():
    """Fetch the top active markets from Polymarket sorted by volume."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "limit": 50, "order": "volume", "ascending": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch markets: {e}")
        return []


def analyze_markets(markets):
    """Send market data to Claude and get back an analysis."""
    if not markets:
        return None

    # Build a clean summary of the top 20 markets for Claude to read
    summary = []
    for m in markets[:20]:
        try:
            summary.append({
                "question": m.get("question", "?"),
                "volume_usd": round(float(m.get("volume", 0)), 2),
                "outcome_prices": m.get("outcomePrices", []),
                "closes": m.get("endDate", "?"),
            })
        except Exception:
            continue

    prompt = f"""You are a sharp, conservative prediction market analyst helping a retail investor in Southeast Asia.

Below are the top active Polymarket markets right now, ordered by trading volume:

{json.dumps(summary, indent=2)}

Your job:
1. Identify any markets where the current odds look clearly WRONG based on your knowledge.
2. Check whether YES + NO prices sum to approximately $1.00. If they sum to less than $0.97, that itself is a guaranteed-profit arbitrage — flag it immediately.
3. Flag only markets where you are genuinely confident there is an edge. Silence is better than noise.

For every opportunity you find, respond in EXACTLY this format (repeat the block for multiple alerts):

ALERT: [full market question]
CURRENT ODDS: YES = $[price] / NO = $[price]
WHY IT LOOKS MISPRICED: [2 sentences max]
SUGGESTED PLAY: BUY [YES or NO] at $[price]
CONFIDENCE: [Low / Medium / High]
---

If you find NO clear opportunities this cycle, respond with ONLY this word:
NO_ALERT

Do not add any other text. Be conservative — only alert on real edges."""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return None


def send_email(analysis_text):
    """Send a formatted alert email via Gmail SMTP."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "🎯 Polymarket Alert — Opportunity Spotted"
        msg["From"]    = GMAIL_ADDRESS
        msg["To"]      = ALERT_EMAIL

        plain = f"""Polymarket Research Bot — Opportunity Alert

{analysis_text}

──────────────────────────────
→ Go to https://polymarket.com to act on this.
This is NOT financial advice. Make your own decision.
Bot checks markets every {CHECK_INTERVAL_MINUTES} minutes.
"""
        # Format each ALERT block as an HTML card
        cards_html = ""
        for block in analysis_text.split("---"):
            block = block.strip()
            if not block:
                continue
            lines = block.splitlines()
            card_lines = ""
            for line in lines:
                if line.startswith("ALERT:"):
                    card_lines += f'<h3 style="margin:0 0 12px;color:#1a1a2e">{line.replace("ALERT:","").strip()}</h3>'
                elif line.startswith("CURRENT ODDS:"):
                    card_lines += f'<p style="margin:6px 0"><strong>Odds:</strong> {line.replace("CURRENT ODDS:","").strip()}</p>'
                elif line.startswith("WHY IT LOOKS MISPRICED:"):
                    card_lines += f'<p style="margin:6px 0"><strong>Why:</strong> {line.replace("WHY IT LOOKS MISPRICED:","").strip()}</p>'
                elif line.startswith("SUGGESTED PLAY:"):
                    play = line.replace("SUGGESTED PLAY:","").strip()
                    card_lines += f'<p style="margin:6px 0;font-size:16px"><strong>Play:</strong> <span style="color:#2d6a4f;font-weight:bold">{play}</span></p>'
                elif line.startswith("CONFIDENCE:"):
                    level = line.replace("CONFIDENCE:","").strip()
                    colour = {"High": "#e63946", "Medium": "#f4a261", "Low": "#adb5bd"}.get(level, "#adb5bd")
                    card_lines += f'<p style="margin:6px 0"><strong>Confidence:</strong> <span style="color:{colour};font-weight:bold">{level}</span></p>'
            if card_lines:
                cards_html += f'<div style="background:#f8f9ff;border-left:4px solid #4361ee;padding:16px;border-radius:6px;margin-bottom:16px">{card_lines}</div>'

        html = f"""<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">
<h2 style="color:#1a1a2e;margin-bottom:4px">🎯 Polymarket Opportunity Alert</h2>
<p style="color:#666;font-size:13px;margin-top:0">Bot scan completed — {len(analysis_text.split('ALERT:'))-1} opportunity(s) found</p>
{cards_html}
<a href="https://polymarket.com" style="display:inline-block;background:#4361ee;color:#fff;padding:12px 28px;text-decoration:none;border-radius:6px;font-weight:bold;margin-top:8px">Open Polymarket →</a>
<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice · Bot checks every {CHECK_INTERVAL_MINUTES} min · Reply to unsubscribe</p>
</body></html>"""

        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, ALERT_EMAIL, msg.as_string())

        logger.info("✅ Alert email sent.")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


def run_cycle():
    logger.info("── Starting market check cycle ──")
    markets = get_polymarket_markets()
    if not markets:
        logger.info("No market data retrieved. Skipping cycle.")
        return

    logger.info(f"Fetched {len(markets)} markets. Sending to Claude...")
    analysis = analyze_markets(markets)

    if not analysis:
        logger.info("No analysis returned. Skipping.")
        return

    if analysis == "NO_ALERT":
        logger.info("No opportunities found this cycle. Staying silent.")
        return

    if "ALERT:" in analysis:
        logger.info("Opportunity detected! Sending email alert...")
        send_email(analysis)
    else:
        logger.info(f"Unexpected response from Claude: {analysis[:80]}")


if __name__ == "__main__":
    logger.info("🚀 Polymarket Research Bot starting up...")
    logger.info(f"Checking every {CHECK_INTERVAL_MINUTES} minutes.")
    logger.info(f"Alerts → {ALERT_EMAIL}")

    # Run once immediately so you know it's working
    run_cycle()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_cycle, "interval", minutes=CHECK_INTERVAL_MINUTES)
    logger.info("Scheduler started. Bot is live.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
