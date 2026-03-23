import os
import smtplib
import json
import requests
import anthropic
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from apscheduler.schedulers.blocking import BlockingScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY      = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS          = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD     = os.environ["GMAIL_APP_PASSWORD"]
ALERT_EMAIL            = os.environ["ALERT_EMAIL"]
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
GAMMA_API = "https://gamma-api.polymarket.com"


def get_polymarket_markets():
    try:
        resp = requests.get(f"{GAMMA_API}/markets",
            params={"active": "true", "limit": 50, "order": "volume", "ascending": "false"}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch markets: {e}")
        return []


def analyze_markets(markets):
    if not markets:
        return None
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
2. Check whether YES + NO prices sum to approximately $1.00. If they sum to less than $0.97, flag it as arbitrage.
3. Flag only markets where you are genuinely confident there is an edge. Silence is better than noise.

For every opportunity found, respond in EXACTLY this format:

ALERT: [full market question]
CURRENT ODDS: YES = $[price] / NO = $[price]
WHY IT LOOKS MISPRICED: [2 sentences max]
SUGGESTED PLAY: BUY [YES or NO] at $[price]
CONFIDENCE: [Low / Medium / High]
---

If NO clear opportunities, respond with ONLY: NO_ALERT"""

    try:
        resp = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1000,
            messages=[{"role": "user", "content": prompt}])
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return None


def send_email(analysis_text):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Polymarket Alert - Opportunity Spotted"
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = ALERT_EMAIL

        plain = f"Polymarket Bot Alert\n\n{analysis_text}\n\nGo to https://polymarket.com\nNot financial advice."

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
                    colour = {"High":"#e63946","Medium":"#f4a261","Low":"#adb5bd"}.get(level,"#adb5bd")
                    card_lines += f'<p style="margin:6px 0"><strong>Confidence:</strong> <span style="color:{colour};font-weight:bold">{level}</span></p>'
            if card_lines:
                cards_html += f'<div style="background:#f8f9ff;border-left:4px solid #4361ee;padding:16px;border-radius:6px;margin-bottom:16px">{card_lines}</div>'

        html = f"""<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px">
<h2 style="color:#1a1a2e">Polymarket Opportunity Alert</h2>
{cards_html}
<a href="https://polymarket.com" style="display:inline-block;background:#4361ee;color:#fff;padding:12px 28px;text-decoration:none;border-radius:6px;font-weight:bold;margin-top:8px">Open Polymarket</a>
<p style="color:#aaa;font-size:11px;margin-top:24px">Not financial advice. Bot checks every {CHECK_INTERVAL_MINUTES} min.</p>
</body></html>"""

        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))

        # PORT 587 + STARTTLS — works on Railway (port 465 is blocked)
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, ALERT_EMAIL, msg.as_string())

        logger.info("✅ Alert email sent successfully.")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


def run_cycle():
    logger.info("── Starting market check cycle ──")
    markets = get_polymarket_markets()
    if not markets:
        logger.info("No market data. Skipping.")
        return
    logger.info(f"Fetched {len(markets)} markets. Sending to Claude...")
    analysis = analyze_markets(markets)
    if not analysis:
        return
    if analysis == "NO_ALERT":
        logger.info("No opportunities this cycle. Staying silent.")
        return
    if "ALERT:" in analysis:
        logger.info("Opportunity detected! Sending email...")
        send_email(analysis)
    else:
        logger.info(f"Unexpected Claude response: {analysis[:80]}")


if __name__ == "__main__":
    logger.info("🚀 Polymarket Research Bot starting up...")
    logger.info(f"Checking every {CHECK_INTERVAL_MINUTES} minutes.")
    logger.info(f"Alerts → {ALERT_EMAIL}")
    run_cycle()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_cycle, "interval", minutes=CHECK_INTERVAL_MINUTES)
    logger.info("Scheduler started. Bot is live.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
