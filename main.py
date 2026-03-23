import os
import json
import requests
import anthropic
import logging
from apscheduler.schedulers.blocking import BlockingScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY      = os.environ["ANTHROPIC_API_KEY"]
RESEND_API_KEY         = os.environ["RESEND_API_KEY"]
ALERT_EMAIL            = os.environ["ALERT_EMAIL"]
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
GAMMA_API = "https://gamma-api.polymarket.com"


def get_polymarket_markets():
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "limit": 50, "order": "volume", "ascending": "false"},
            timeout=15
        )
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

    prompt = (
        "You are a sharp, conservative prediction market analyst helping a retail investor in Southeast Asia.\n\n"
        "Below are the top active Polymarket markets right now, ordered by trading volume:\n\n"
        + json.dumps(summary, indent=2)
        + "\n\nYour job:\n"
        "1. Identify any markets where the current odds look clearly WRONG based on your knowledge.\n"
        "2. Check whether YES + NO prices sum to approximately $1.00. If they sum to less than $0.97, flag it as arbitrage.\n"
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


def build_html(analysis_text):
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

    return (
        "<html><body style=\"font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:24px\">"
        "<h2 style=\"color:#1a1a2e\">Polymarket Opportunity Alert</h2>"
        + cards_html
        + "<a href=\"https://polymarket.com\" style=\"display:inline-block;background:#4361ee;"
        "color:#fff;padding:12px 28px;text-decoration:none;border-radius:6px;"
        "font-weight:bold;margin-top:8px\">Open Polymarket</a>"
        "<p style=\"color:#aaa;font-size:11px;margin-top:24px\">Not financial advice.</p>"
        "</body></html>"
    )


def send_email(analysis_text):
    try:
        html = build_html(analysis_text)
        plain = "Polymarket Bot Alert\n\n" + analysis_text + "\n\nGo to https://polymarket.com\nNot financial advice."

        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": "Polymarket Bot <onboarding@resend.dev>",
                "to": [ALERT_EMAIL],
                "subject": "Polymarket Alert - Opportunity Spotted",
                "html": html,
                "text": plain
            },
            timeout=15
        )

        if resp.status_code in (200, 201):
            logger.info("Alert email sent successfully.")
            return True
        else:
            logger.error(f"Resend API error {resp.status_code}: {resp.text}")
            return False

    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


def run_cycle():
    logger.info("Starting market check cycle")
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
    logger.info("Polymarket Research Bot starting up...")
    logger.info(f"Checking every {CHECK_INTERVAL_MINUTES} minutes.")
    logger.info(f"Alerts going to {ALERT_EMAIL}")
    run_cycle()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_cycle, "interval", minutes=CHECK_INTERVAL_MINUTES)
    logger.info("Scheduler started. Bot is live.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")

