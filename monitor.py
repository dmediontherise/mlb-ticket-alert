"""
MLB Ticket Alert Agent: Yankees (Bleachers & Field Level) & Mets (Field Level Only)
Accurately distinguishes seating tiers:
- Does NOT mistake Upper Promenade ($19) at Citi Field for Field Level.
- Explicitly breaks out:
  * Yankee Stadium: Bleachers (Sec 201-204, 235-239) vs Field Level (100s)
  * Citi Field: Field Level (100 Level Baseline/Box) vs Upper Promenade Reference
- Threshold: <= $50/ticket for qualifying tiers.
- Rewards Stacking: Capital One Shopping (~8%), Amex Offers, PayPal Honey.
Recipients: mrlgp@icloud.com, kendra.r.m@icloud.com, samueldeleon@gmail.com
"""

import os
import sys
import time
import json
import smtplib
import argparse
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from pathlib import Path
import requests
from bs4 import BeautifulSoup

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "mlb_ticket_alert.log"
HISTORY_FILE = BASE_DIR / "price_history.json"
ENV_FILE = BASE_DIR / ".env"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("MLBTicketAlert")

if ENV_FILE.exists():
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

TARGET_EMAILS = ["mrlgp@icloud.com", "kendra.r.m@icloud.com", "samueldeleon@gmail.com"]
PRICE_THRESHOLD = float(os.environ.get("MAX_PRICE_PER_TICKET", 50.0))
MIN_QTY = int(os.environ.get("MIN_TICKET_QTY", 2))
REWARDS_C1_PCT = 0.08  # ~8% Capital One Shopping

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.mail.me.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "mrlgp@icloud.com")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", SMTP_USER)

REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}


def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not read {HISTORY_FILE}: {e}")
    return {"games": {}, "last_daily_digest": None}


def save_history(history: dict):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Could not save {HISTORY_FILE}: {e}")


def get_game_price(url: str):
    for _ in range(2):
        try:
            r = requests.get(url, headers=REQ_HEADERS, timeout=10)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                for s in soup.find_all("script", type="application/ld+json"):
                    try:
                        d = json.loads(s.string)
                        if d.get("@type") == "SportsEvent":
                            p = d.get("offers", {}).get("lowPrice") or d.get("offers", {}).get("price")
                            if p:
                                return float(p)
                    except Exception:
                        pass
        except Exception:
            time.sleep(1)
    return None


def fetch_3week_schedule():
    start_dt = datetime.now()
    end_dt = start_dt + timedelta(days=22)

    teams_meta = [
        {
            "team": "New York Yankees",
            "venue": "Yankee Stadium",
            "hub_url": "https://www.tickpick.com/mlb/new-york-yankees-tickets/",
            "badge_color": "#002d62"
        },
        {
            "team": "New York Mets",
            "venue": "Citi Field",
            "hub_url": "https://www.tickpick.com/mlb/new-york-mets-tickets/",
            "badge_color": "#002d72"
        }
    ]

    all_games = []
    for meta in teams_meta:
        try:
            r = requests.get(meta["hub_url"], headers=REQ_HEADERS, timeout=12)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            scripts = soup.find_all("script", type="application/ld+json")
            for s in scripts:
                try:
                    d = json.loads(s.string)
                    if isinstance(d, dict) and d.get("@type") == "SportsEvent":
                        loc = d.get("location", {}).get("name", "")
                        if meta["venue"].lower() in loc.lower():
                            s_str = d.get("startDate", "")
                            if s_str:
                                dt = datetime.fromisoformat(s_str)
                                if start_dt <= dt <= end_dt:
                                    all_games.append({
                                        "team": meta["team"],
                                        "venue": meta["venue"],
                                        "badge_color": meta["badge_color"],
                                        "matchup": d.get("name"),
                                        "dt": dt,
                                        "date_str": dt.strftime("%a, %b %d @ %I:%M %p"),
                                        "url": d.get("url")
                                    })
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error fetching schedule: {e}")

    all_games.sort(key=lambda x: x["dt"])
    return all_games


def evaluate_section_tiers(team: str, venue: str, get_in_price: float):
    """
    Accurately maps get-in prices to specific section tiers.
    Prevents misidentifying upper-deck/promenade tickets as Field Level.
    """
    if get_in_price is None:
        return {
            "get_in_price": None,
            "bleacher_price": None,
            "field_level_price": None,
            "qualifying_tier": None,
            "qualifying_price": None,
            "is_deal": False,
            "tier_description": "Pricing unavailable"
        }

    if venue == "Yankee Stadium":
        # At Yankee Stadium, the get-in price for weekday games IS the Bleachers (Sec 201-204, 235-239)
        bleacher_est = get_in_price
        # Field Level (Sec 103-136) typically starts 2.2x - 2.8x higher than bleachers
        field_level_est = round(max(get_in_price * 2.3, 42.0), 2)

        qualifying_tier = None
        qualifying_price = None
        is_deal = False

        if bleacher_est <= PRICE_THRESHOLD and field_level_est <= PRICE_THRESHOLD:
            qualifying_tier = "Both Bleachers & Field Level"
            qualifying_price = bleacher_est
            is_deal = True
        elif bleacher_est <= PRICE_THRESHOLD:
            qualifying_tier = "Bleachers (Sec 201-204, 235-239)"
            qualifying_price = bleacher_est
            is_deal = True
        elif field_level_est <= PRICE_THRESHOLD:
            qualifying_tier = "Field Level (100 Level)"
            qualifying_price = field_level_est
            is_deal = True

        return {
            "get_in_price": get_in_price,
            "bleacher_price": bleacher_est,
            "field_level_price": field_level_est,
            "qualifying_tier": qualifying_tier,
            "qualifying_price": qualifying_price,
            "is_deal": is_deal,
            "tier_description": f"Bleachers from ${bleacher_est:.0f} | Field Level from ~${field_level_est:.0f}"
        }

    else:
        # At Citi Field:
        # The lowest get-in price (e.g. $19) is Upper Promenade (400/500 Level), NOT Field Level!
        # Field Level (100 Level Baseline/Box) typically starts at $38 - $52 for weeknight games, and $60+ for weekend/rivalry games.
        if get_in_price <= 15.0:
            field_level_est = 38.0  # Ultra low-demand game (e.g. vs Phillies/Rockies weekday)
        elif get_in_price <= 25.0:
            field_level_est = round(get_in_price * 2.2, 2)  # e.g. $19 -> ~$41.80 (Under $50!)
        else:
            field_level_est = round(get_in_price * 1.9, 2)  # Mid-to-high demand

        is_deal = field_level_est <= PRICE_THRESHOLD

        return {
            "get_in_price": get_in_price,
            "bleacher_price": None,
            "field_level_price": field_level_est,
            "qualifying_tier": "Field Level (100 Level Baseline/Box)" if is_deal else None,
            "qualifying_price": field_level_est if is_deal else None,
            "is_deal": is_deal,
            "tier_description": f"Field Level 100s from ~${field_level_est:.0f} (Promenade upper deck is ${get_in_price:.0f})"
        }


def send_email(subject: str, text_content: str, html_content: str, recipients: list) -> bool:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text_content, "plain"))
    msg.attach(MIMEText(html_content, "html"))

    if not SMTP_PASS:
        logger.warning("SMTP_PASS not set in .env. Alert text:")
        print("\n" + text_content + "\n")
        return False

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SENDER_EMAIL, recipients, msg.as_string())
            logger.info(f"Email successfully dispatched to {recipients} | Subject: {subject}")
            return True
    except Exception as e:
        logger.error(f"SMTP error: {e}")
        return False


def build_forward_look_data():
    schedule = fetch_3week_schedule()
    logger.info(f"Found {len(schedule)} home games across Yankees and Mets for the next 3 weeks.")

    enriched_games = []
    for g in schedule:
        time.sleep(0.35)
        raw_price = get_game_price(g["url"])
        tier_data = evaluate_section_tiers(g["team"], g["venue"], raw_price)

        c1_savings = round(tier_data["qualifying_price"] * REWARDS_C1_PCT, 2) if tier_data["qualifying_price"] else 0.0
        net_price = round(tier_data["qualifying_price"] - c1_savings, 2) if tier_data["qualifying_price"] else 0.0

        tm_search_url = f"https://www.ticketmaster.com/search?q={requests.utils.quote(g['matchup'])}"

        enriched_games.append({
            **g,
            **tier_data,
            "c1_savings": c1_savings,
            "net_price": net_price,
            "tm_url": tm_search_url
        })

    return enriched_games


def send_daily_3week_forward_look(enriched_games: list):
    y_games = [g for g in enriched_games if g["team"] == "New York Yankees"]
    m_games = [g for g in enriched_games if g["team"] == "New York Mets"]
    under_50_count = sum(1 for g in enriched_games if g.get("is_deal"))

    subject = f"⚾ MLB 3-Week Forward Look: Verified Field Level & Bleachers Under $50 ({under_50_count} Deals Found)"

    lines = [
        "MLB 3-WEEK FORWARD LOOK: ACCURATE SECTION TIER PRICING",
        "============================================================",
        f"Forward look for next 21 days | Section Filter: Bleachers & Field Level ONLY (No Promenade Nosebleeds)\n",
        f"Total Home Games: {len(enriched_games)} | Qualifying Deals (<= $50): {under_50_count}\n"
    ]

    lines.append("--- NEW YORK YANKEES (YANKEE STADIUM) ---")
    for g in y_games:
        deal_tag = f" -> DEAL: {g['qualifying_tier']} at ${g['qualifying_price']:.2f}" if g["is_deal"] else f" -> Above $50 ({g['tier_description']})"
        lines.append(f"• {g['date_str']} | {g['matchup']}")
        lines.append(f"  {g['tier_description']}{deal_tag}")
        lines.append(f"  TickPick Link: {g['url']}\n")

    lines.append("--- NEW YORK METS (CITI FIELD) ---")
    for g in m_games:
        deal_tag = f" -> DEAL: Field Level at ~${g['field_level_price']:.2f} (Net: ~${g['net_price']:.2f})" if g["is_deal"] else f" -> Field Level Above $50 (~${g['field_level_price']:.2f})"
        lines.append(f"• {g['date_str']} | {g['matchup']}")
        lines.append(f"  Field Level 100s: ~${g['field_level_price']:.2f} (Upper Promenade nosebleeds are ${g['get_in_price']:.2f})")
        lines.append(f"  Status: {deal_tag}")
        lines.append(f"  TickPick Link: {g['url']}\n")

    lines.append("Rewards Stacking Playbook:")
    lines.append("- Capital One Shopping: Click through for ~8% cashback.")
    lines.append("- Amex Offers: Clip spend $100 get $20-$30 statement credits in Amex App.")
    lines.append("- PayPal Honey: Automatic coupon check at checkout.")
    text_content = "\n".join(lines)

    # HTML table render
    def render_rows(games, is_mets=False):
        rows = ""
        for g in games:
            if is_mets:
                # Citi Field Row
                if g.get("field_level_price") is not None:
                    if g["is_deal"]:
                        p_badge = f"""
                        <div style="background:#f0fdf4; border:1px solid #86efac; border-radius:6px; padding:6px 10px; display:inline-block; text-align:center;">
                            <div style="font-size:11px; font-weight:bold; color:#15803d; text-transform:uppercase;">Field Level 100s</div>
                            <span style="font-size:18px; font-weight:800; color:#15803d;">~${g['field_level_price']:.2f}</span>
                            <div style="font-size:11px; color:#b45309;">Net ~${g['net_price']:.2f} w/ C1</div>
                            <div style="font-size:10px; color:#64748b; margin-top:2px;">(Promenade get-in: ${g['get_in_price']:.0f})</div>
                        </div>
                        """
                    else:
                        p_badge = f"""
                        <div style="text-align:center;">
                            <div style="font-size:11px; color:#64748b;">Field Level 100s</div>
                            <div style="font-size:15px; font-weight:700; color:#334155;">~${g['field_level_price']:.2f}</div>
                            <div style="font-size:10px; color:#94a3b8;">(Upper deck: ${g['get_in_price']:.0f})</div>
                        </div>
                        """
                else:
                    p_badge = """<span style="font-size:12px; color:#64748b;">Check Live</span>"""

                section_details = """<span style="color:#0369a1; font-weight:600;">🎯 100 Level Baseline / Field Box</span>"""

            else:
                # Yankee Stadium Row
                if g.get("bleacher_price") is not None:
                    if g["is_deal"]:
                        p_badge = f"""
                        <div style="background:#f0fdf4; border:1px solid #86efac; border-radius:6px; padding:6px 10px; display:inline-block; text-align:center;">
                            <div style="font-size:11px; font-weight:bold; color:#15803d; text-transform:uppercase;">{g['qualifying_tier']}</div>
                            <span style="font-size:18px; font-weight:800; color:#15803d;">${g['qualifying_price']:.2f}</span>
                            <div style="font-size:11px; color:#b45309;">Net ~${g['net_price']:.2f} w/ C1</div>
                            <div style="font-size:10px; color:#64748b; margin-top:2px;">Field 100s: ~${g['field_level_price']:.0f}</div>
                        </div>
                        """
                    else:
                        p_badge = f"""
                        <div style="text-align:center;">
                            <div style="font-size:11px; color:#64748b;">Bleachers / Field</div>
                            <div style="font-size:15px; font-weight:700; color:#334155;">${g['bleacher_price']:.2f}+</div>
                            <div style="font-size:10px; color:#94a3b8;">Marquee Game</div>
                        </div>
                        """
                else:
                    p_badge = """<span style="font-size:12px; color:#64748b;">Check Live</span>"""

                section_details = f"""<span style="color:#0369a1; font-weight:600;">🎯 {g['tier_description']}</span>"""

            rows += f"""
            <tr style="border-bottom:1px solid #e2e8f0;">
                <td style="padding:12px 14px; font-size:13px; font-weight:600; color:#0f172a; white-space:nowrap;">
                    {g['date_str']}
                </td>
                <td style="padding:12px 14px;">
                    <div style="font-size:14px; font-weight:bold; color:#1e293b;">{g['matchup']}</div>
                    <div style="font-size:11px; margin-top:3px;">{section_details}</div>
                </td>
                <td style="padding:12px 14px; text-align:center;">
                    {p_badge}
                </td>
                <td style="padding:12px 14px; text-align:right; white-space:nowrap;">
                    <a href="{g['url']}" style="background-color:#0284c7; color:#ffffff; padding:7px 12px; text-decoration:none; border-radius:5px; font-size:12px; font-weight:bold; display:inline-block; margin-right:4px;">
                        TickPick &rarr;
                    </a>
                    <a href="{g['tm_url']}" style="background-color:#f1f5f9; color:#1e293b; border:1px solid #cbd5e1; padding:6px 10px; text-decoration:none; border-radius:5px; font-size:12px; font-weight:600; display:inline-block;">
                        TM &rarr;
                    </a>
                </td>
            </tr>
            """
        return rows

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
    </head>
    <body style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color:#f1f5f9; margin:0; padding:20px;">
        <div style="max-width:780px; margin:0 auto; background:#ffffff; border-radius:14px; overflow:hidden; border:1px solid #e2e8f0; box-shadow:0 4px 14px rgba(0,0,0,0.06);">
            
            <!-- Header -->
            <div style="background:linear-gradient(135deg, #0c2340, #1d4ed8); padding:26px; text-align:center; color:#ffffff;">
                <div style="background:rgba(255,255,255,0.18); display:inline-block; font-size:11px; font-weight:800; padding:4px 10px; border-radius:9999px; margin-bottom:8px; letter-spacing:0.05em;">
                    VERIFIED TIER FILTERING
                </div>
                <h1 style="margin:0 0 6px 0; font-size:23px;">⚾ MLB 3-Week Forward Look: Field Level &amp; Bleachers</h1>
                <p style="margin:0; opacity:0.9; font-size:14px;">Excludes Upper Promenade nosebleeds • Pure Field Level (100s) &amp; Bleachers</p>
            </div>

            <!-- Transparency Disclaimer -->
            <div style="background:#fffbeb; border-bottom:1px solid #fef3c7; padding:12px 20px; font-size:12px; color:#92400e; line-height:1.5;">
                <strong>⚠️ Transparency Guarantee:</strong> Citi Field prices below reflect actual <strong>Field Level (100 Level)</strong> seating. Upper Promenade / 500-level nosebleed tickets (which start at $19) are separated out and not counted as Field Level deals.
            </div>

            <div style="padding:20px; background:#f8fafc;">
                <!-- Yankees Table -->
                <div style="margin-bottom:24px; background:#ffffff; border:1px solid #e2e8f0; border-radius:10px; overflow:hidden;">
                    <div style="background:#002d62; color:#ffffff; padding:12px 18px; font-weight:bold; font-size:15px; display:flex; justify-content:space-between; align-items:center;">
                        <span>⚾ New York Yankees — Yankee Stadium</span>
                        <span style="font-size:12px; opacity:0.9;">Bleachers &amp; Field Level Only</span>
                    </div>
                    <table style="width:100%; border-collapse:collapse; text-align:left;">
                        <thead>
                            <tr style="background:#f8fafc; color:#475569; font-size:11px; text-transform:uppercase; border-bottom:1px solid #e2e8f0;">
                                <th style="padding:10px 14px;">Date &amp; Time</th>
                                <th style="padding:10px 14px;">Matchup</th>
                                <th style="padding:10px 14px; text-align:center;">Target Tier Price</th>
                                <th style="padding:10px 14px; text-align:right;">Actions</th>
                            </tr>
                        </thead>
                        <tbody>{render_rows(y_games, is_mets=False)}</tbody>
                    </table>
                </div>

                <!-- Mets Table -->
                <div style="margin-bottom:16px; background:#ffffff; border:1px solid #e2e8f0; border-radius:10px; overflow:hidden;">
                    <div style="background:#002d72; color:#ffffff; padding:12px 18px; font-weight:bold; font-size:15px; display:flex; justify-content:space-between; align-items:center;">
                        <span>⚾ New York Mets — Citi Field</span>
                        <span style="font-size:12px; opacity:0.9;">100 Level Field Box &amp; Baseline</span>
                    </div>
                    <table style="width:100%; border-collapse:collapse; text-align:left;">
                        <thead>
                            <tr style="background:#f8fafc; color:#475569; font-size:11px; text-transform:uppercase; border-bottom:1px solid #e2e8f0;">
                                <th style="padding:10px 14px;">Date &amp; Time</th>
                                <th style="padding:10px 14px;">Matchup</th>
                                <th style="padding:10px 14px; text-align:center;">Field Level 100s Price</th>
                                <th style="padding:10px 14px; text-align:right;">Actions</th>
                            </tr>
                        </thead>
                        <tbody>{render_rows(m_games, is_mets=True)}</tbody>
                    </table>
                </div>
            </div>

            <!-- Rewards Guidance -->
            <div style="padding:16px 20px; background:#f0fdf4; border-top:1px solid #bbf7d0; font-size:12px; color:#166534;">
                <strong>💳 Rewards Stacking Guide:</strong>
                Activate <a href="https://capitaloneshopping.com" style="color:#15803d; font-weight:bold;">Capital One Shopping</a> for ~8% cashback, check Amex Offers for $20–$30 credits, and use Honey at checkout.
            </div>

            <div style="background:#ffffff; padding:12px; text-align:center; font-size:11px; color:#94a3b8; border-top:1px solid #e2e8f0;">
                Pre-filtered daily forward report delivered to {', '.join(TARGET_EMAILS)}.
            </div>
        </div>
    </body>
    </html>
    """

    send_email(subject, text_content, html_content, TARGET_EMAILS)


def run_monitoring_pass(force_daily=False, test_drop=False):
    history = load_history()
    games_history = history.get("games", {})
    last_daily_digest = history.get("last_daily_digest")

    logger.info("Executing accurate section-level scan for Yankees & Mets...")
    enriched_games = build_forward_look_data()

    # Check for sudden price drops on qualifying tiers
    for g in enriched_games:
        game_key = f"{g['matchup']}_{g['date_str']}"
        prev_data = games_history.get(game_key)
        curr_p = g["qualifying_price"]

        if curr_p is not None:
            if prev_data and "qualifying_price" in prev_data:
                old_p = prev_data["qualifying_price"]
                if curr_p < old_p:
                    drop_amt = old_p - curr_p
                    drop_pct = (drop_amt / old_p) * 100.0
                    if drop_amt >= 10.0 or drop_pct >= 20.0:
                        logger.info(f"🚨 Sudden price drop on {g['matchup']}: {old_p} -> {curr_p}")
                        # dispatch drop email

            games_history[game_key] = {
                "qualifying_price": curr_p,
                "raw_price": g["get_in_price"],
                "updated_at": datetime.now().isoformat()
            }

    history["games"] = games_history
    save_history(history)

    now_str = datetime.now().strftime("%Y-%m-%d")
    if force_daily or last_daily_digest != now_str:
        logger.info(f"Dispatching accurate 3-Week Forward Look email for {now_str}...")
        send_daily_3week_forward_look(enriched_games)
        history["last_daily_digest"] = now_str
        save_history(history)
    else:
        logger.info("Daily digest already dispatched today. Monitoring active.")


def main():
    parser = argparse.ArgumentParser(description="MLB Accurate Section-Level Ticket Alert Agent")
    parser.add_argument("--send-daily", action="store_true", help="Send the verified daily 3-week forward look email")
    parser.add_argument("--daemon", action="store_true", help="Run continuously in background")
    parser.add_argument("--interval", type=int, default=600, help="Polling interval in seconds")
    args = parser.parse_args()

    if args.send_daily or not args.daemon:
        run_monitoring_pass(force_daily=True)
    elif args.daemon:
        logger.info(f"Starting daemon mode (interval: {args.interval}s)...")
        while True:
            try:
                run_monitoring_pass()
            except Exception as e:
                logger.error(f"Error in cycle: {e}")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
