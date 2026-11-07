"""
Blitz Top Performer Bot
-----------------------
When an NFL game goes final:
  1. pulls the box score (ESPN public JSON)
  2. finds the top PPR fantasy performer
  3. finds the highest scorer who has a Blitz market
  4. screenshots his Blitz page (1W chart); IMAGE_MODE=card uses a branded card instead
  5. writes a caption (Claude API, or a safe template fallback)
  6. MODE=draft  -> sends card + caption to a Discord channel for you to review
     MODE=live   -> posts to X
Already-handled games are stored in posted.json so nothing double-posts.

Usage:
  python bot.py              # normal run (used by GitHub Actions)
  python bot.py --test       # offline test using tests/sample_summary.json
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "posted.json"
OUT_DIR = ROOT / "out"

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={id}"

MODE = os.getenv("MODE", "draft").lower()  # "draft" or "live"

# Words the caption must never contain (keeps posts away from real-money CTAs)
BANNED_WORDS = ["bet", "wager", "deposit", "free money", "cash out", "guaranteed", "win money", "risk-free"]

# Brand colors (from the Blitz site)
RED = (225, 45, 45)
BLACK = (14, 14, 16)
WHITE = (245, 245, 245)
GREY = (150, 150, 155)
GREEN = (46, 160, 100)


# ---------------------------------------------------------------- state
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"posted_game_ids": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------- ESPN data
def get_finished_games():
    data = requests.get(SCOREBOARD_URL, timeout=20).json()
    games = []
    for ev in data.get("events", []):
        if ev.get("status", {}).get("type", {}).get("completed"):
            games.append({"id": ev["id"], "name": ev.get("shortName", ev.get("name", ""))})
    return games


def get_summary(game_id):
    return requests.get(SUMMARY_URL.format(id=game_id), timeout=20).json()


def _num(v):
    try:
        return float(str(v).split("/")[0].split("-")[0])
    except (ValueError, TypeError):
        return 0.0


def parse_players(summary):
    """Returns {athlete_id: {name, team, opp, pos, stats{...}}} from an ESPN summary."""
    teams = summary.get("boxscore", {}).get("players", [])
    abbrs = [t.get("team", {}).get("abbreviation", "") for t in teams]
    players = {}

    for i, team in enumerate(teams):
        abbr = abbrs[i] if i < len(abbrs) else ""
        opp = abbrs[1 - i] if len(abbrs) == 2 else ""
        for cat in team.get("statistics", []):
            cname = cat.get("name", "")
            keys = cat.get("keys") or cat.get("labels") or []
            for a in cat.get("athletes", []):
                ath = a.get("athlete", {})
                pid = ath.get("id")
                if not pid:
                    continue
                p = players.setdefault(pid, {
                    "id": pid,
                    "name": ath.get("displayName", "Unknown"),
                    "team": abbr,
                    "opp": opp,
                    "pos": (ath.get("position") or {}).get("abbreviation", ""),
                    "stats": {},
                })
                for k, v in zip(keys, a.get("stats", [])):
                    p["stats"][f"{cname}.{k}"] = v
    return players


def fantasy_points(s):
    g = lambda k: _num(s.get(k, 0))
    # passing "completions/passingAttempts" is stored under that combined key
    pts = 0.0
    pts += g("passing.passingYards") * 0.04
    pts += g("passing.passingTouchdowns") * 4
    pts -= g("passing.interceptions") * 2
    pts += g("rushing.rushingYards") * 0.1
    pts += g("rushing.rushingTouchdowns") * 6
    pts += g("receiving.receptions") * 1
    pts += g("receiving.receivingYards") * 0.1
    pts += g("receiving.receivingTouchdowns") * 6
    pts -= g("fumbles.fumblesLost") * 2
    return round(pts, 1)


def stat_line(p):
    s = p["stats"]
    g = lambda k: int(_num(s.get(k, 0)))
    parts = []
    if g("passing.passingYards"):
        comp = s.get("passing.completions/passingAttempts", "")
        parts.append(f"{comp} {g('passing.passingYards')} pass yds, {g('passing.passingTouchdowns')} TD"
                     + (f", {g('passing.interceptions')} INT" if g("passing.interceptions") else ""))
    if g("rushing.rushingYards") or g("rushing.rushingTouchdowns"):
        parts.append(f"{g('rushing.rushingAttempts')} car, {g('rushing.rushingYards')} rush yds"
                     + (f", {g('rushing.rushingTouchdowns')} TD" if g("rushing.rushingTouchdowns") else ""))
    if g("receiving.receptions"):
        parts.append(f"{g('receiving.receptions')} rec, {g('receiving.receivingYards')} yds"
                     + (f", {g('receiving.receivingTouchdowns')} TD" if g("receiving.receivingTouchdowns") else ""))
    return " | ".join(parts) or "Big day"


def ranked_performers(summary, limit=10):
    """Players in the game sorted by PPR points, best first."""
    players = list(parse_players(summary).values())
    for p in players:
        p["fpts"] = fantasy_points(p["stats"])
        p["line"] = stat_line(p)
    players.sort(key=lambda p: p["fpts"], reverse=True)
    return [p for p in players if p["fpts"] > 0][:limit]


def top_performer(summary):
    ranked = ranked_performers(summary, 1)
    return ranked[0] if ranked else None


# ---------------------------------------------------------------- Blitz screenshot
BLITZ_MARKETS = "https://www.getblitz.pro/markets"


def blitz_screenshot(page, p, path):
    """
    Searches Blitz for the player, opens his market page, switches the chart to 1W,
    and screenshots the name/price/chart section. Returns price info, or None if
    the player has no Blitz market.
    """
    page.goto(BLITZ_MARKETS, wait_until="domcontentloaded", timeout=45000)
    box = page.locator('input[placeholder="Search players"], input[type="search"]').first
    try:
        box.wait_for(state="attached", timeout=20000)
    except Exception:
        body = page.evaluate("() => document.body ? document.body.innerText.slice(0, 300) : ''")
        raise RuntimeError(f"Blitz search box not found. title={page.title()!r} body={body!r}")
    box.fill(p["name"])
    # wait for the result count ("1 result" / "0 results")
    page.wait_for_function(
        r"""() => /\d+ results?/i.test(document.querySelector('header')?.innerText || '')""",
        timeout=15000)
    page.wait_for_timeout(800)
    buttons = page.locator("header ul li button")
    if buttons.count() == 0:
        return None
    # Blitz uses ESPN headshots, so match the exact ESPN athlete id when possible
    target = page.locator(f'header ul li button:has(img[src*="/full/{p["id"]}.png"])')
    if target.count() > 0:
        target.first.click()
    else:
        first_name = buttons.first.inner_text().split("\n")[0].strip().lower()
        if first_name != p["name"].lower():
            return None
        buttons.first.click()

    page.wait_for_url("**/market/**", timeout=15000)
    page.wait_for_selector("h1", timeout=15000)
    page.wait_for_timeout(1200)
    one_week = page.locator("button", has_text=re.compile(r"^\s*1W\s*$")).first
    if one_week.count():
        one_week.click()
        page.wait_for_timeout(1800)

    info = page.evaluate(r"""() => {
        const h = document.querySelector('h1');
        const section = h.parentElement.parentElement.parentElement;
        const r = section.getBoundingClientRect();
        const txt = section.innerText;
        const price = (txt.match(/\$([\d,]+\.\d{2})/) || [])[1];
        const chg = (txt.match(/([+-]\$[\d,.]+)\s*\(([+-][\d.]+%)\)/) || []);
        return {x: r.x, y: r.y, w: r.width, h: r.height,
                price: price ? parseFloat(price.replace(',', '')) : null,
                change: chg[1] || null, pct: chg[2] || null, url: location.href};
    }""")
    pad = 24
    page.screenshot(path=str(path), clip={
        "x": max(info["x"] - pad, 0), "y": max(info["y"] - pad, 0),
        "width": info["w"] + pad * 2, "height": info["h"] + pad * 2})
    return info


def find_on_blitz(summary, game_id):
    """Tries the game's top scorers in order; returns (player, price_info, image) for the
    best one that has a Blitz market."""
    from playwright.sync_api import sync_playwright
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"{game_id}.png"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        page = browser.new_page(
            viewport={"width": 1280, "height": 900}, device_scale_factor=2,
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            locale="en-US")
        try:
            for p in ranked_performers(summary, 8):
                try:
                    info = blitz_screenshot(page, p, path)
                except Exception as e:
                    print(f"  ! Blitz lookup failed for {p['name']}: {str(e)[:400]}")
                    if "search box not found" in str(e):
                        break  # site problem, no point trying more players
                    info = None
                if info:
                    return p, info, path
                print(f"  - {p['name']} ({p['fpts']} pts) not on Blitz, trying next")
        finally:
            browser.close()
    return None, None, None


# ---------------------------------------------------------------- Blitz price
def get_blitz_price(player_name):
    """
    Ask your friend for an endpoint like:
      GET {BLITZ_PRICE_URL}?name=Trey%20McBride
      -> {"price": 29.82, "prev_price": 26.10}
    If BLITZ_PRICE_URL isn't set, the card just shows stats.
    """
    url = os.getenv("BLITZ_PRICE_URL")
    if not url:
        return None
    try:
        headers = {}
        if os.getenv("BLITZ_API_KEY"):
            headers["Authorization"] = f"Bearer {os.getenv('BLITZ_API_KEY')}"
        r = requests.get(url, params={"name": player_name}, headers=headers, timeout=15)
        r.raise_for_status()
        d = r.json()
        return {"price": float(d["price"]), "prev_price": float(d["prev_price"])}
    except Exception as e:
        print(f"  ! Blitz price lookup failed: {e}")
        return None


# ---------------------------------------------------------------- card image
def _font(size, bold=True):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        str(ROOT / "fonts" / ("bold.ttf" if bold else "regular.ttf")),
    ]
    for c in candidates:
        if Path(c).exists():
            return ImageFont.truetype(c, size)
    return ImageFont.load_default(size=size)


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_card(p, price, path):
    W, H = 1200, 675  # X's preferred 16:9
    img = Image.new("RGB", (W, H), BLACK)
    d = ImageDraw.Draw(img)

    # red accent bar + header
    d.rectangle([0, 0, 18, H], fill=RED)
    d.text((70, 50), "BLITZ", font=_font(46), fill=RED)
    d.text((70 + d.textlength("BLITZ ", font=_font(46)), 62), "TOP PERFORMER", font=_font(30), fill=GREY)

    # player
    d.text((70, 140), p["name"].upper(), font=_font(84), fill=WHITE)
    d.text((72, 245), f"{(p['pos'] + ' · ') if p['pos'] else ''}{p['team']} vs {p['opp']}   ·   {p['fpts']} PPR pts",
           font=_font(30, bold=False), fill=GREY)

    # stat line
    y = 310
    for line in _wrap(d, p["line"], _font(38), W - 140):
        d.text((70, y), line, font=_font(38), fill=WHITE)
        y += 52

    # price block
    if price:
        chg = price["price"] - price["prev_price"]
        pct = chg / price["prev_price"] * 100 if price["prev_price"] else 0
        color = GREEN if chg >= 0 else RED
        arrow = "▲" if chg >= 0 else "▼"
        d.text((70, 470), f"${price['price']:.2f}", font=_font(110), fill=WHITE)
        x = 70 + d.textlength(f"${price['price']:.2f}  ", font=_font(110))
        d.text((x, 515), f"{arrow} {chg:+.2f} ({pct:+.1f}%)", font=_font(44), fill=color)
        d.text((72, 600), f"Blitz price · was ${price['prev_price']:.2f}", font=_font(26, bold=False), fill=GREY)
    else:
        d.text((70, 520), "Trade him on Blitz →", font=_font(56), fill=RED)

    d.text((W - 300, H - 55), "getblitz.pro", font=_font(30), fill=GREY)
    OUT_DIR.mkdir(exist_ok=True)
    img.save(path)
    return path


# ---------------------------------------------------------------- caption
def fallback_caption(p, price):
    cap = f"🚨 {p['name']} ({p['team']}) vs {p['opp']}\n{p['line']}\n"
    if price and price.get("price"):
        cap += f"Blitz price: ${price['price']:.2f}"
        if price.get("pct"):
            cap += f" ({price['pct']} this week)"
        cap += "\n"
    cap += "Long or short? 📈📉"
    return cap


def write_caption(p, price):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return fallback_caption(p, price)
    facts = {"player": p["name"], "team": p["team"], "opponent": p["opp"],
             "stat_line": p["line"], "ppr_points": p["fpts"],
             "blitz_price": price and {"price": price.get("price"), "change_this_week": price.get("pct")}}
    prompt = (
        "Write one X post (max 240 characters) for Blitz, an app where fans go long or short on NFL players. "
        "Hype this game's top performer using ONLY these facts, don't invent stats:\n"
        f"{json.dumps(facts)}\n"
        "Rules: start with one emoji, include the stat line, include the Blitz price change if given, "
        "end with a short question like 'Long or short?'. No hashtags spam (max 1). "
        "Never mention betting, wagering, deposits, cash, money-winning, or guarantees. No links. "
        "Return only the post text."
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": os.getenv("CAPTION_MODEL", "claude-haiku-5-5"), "max_tokens": 200,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip().strip('"')
        if len(text) > 280 or any(b in text.lower() for b in BANNED_WORDS):
            print("  ! AI caption failed checks, using template")
            return fallback_caption(p, price)
        return text
    except Exception as e:
        print(f"  ! Caption API failed ({e}), using template")
        return fallback_caption(p, price)


# ---------------------------------------------------------------- outputs
def send_to_discord(caption, image_path, header="**DRAFT — copy/paste to X if it looks good:**"):
    hook = os.getenv("DISCORD_WEBHOOK_URL")
    if not hook:
        print("  (no DISCORD_WEBHOOK_URL set — draft saved in out/ only)")
        return
    with open(image_path, "rb") as f:
        requests.post(hook, data={"content": f"{header}\n{caption}"},
                      files={"file": ("card.png", f, "image/png")}, timeout=30).raise_for_status()
    print("  → sent draft to Discord")


def post_to_x(caption, image_path):
    from requests_oauthlib import OAuth1
    auth = OAuth1(os.environ["X_API_KEY"], os.environ["X_API_SECRET"],
                  os.environ["X_ACCESS_TOKEN"], os.environ["X_ACCESS_SECRET"])
    with open(image_path, "rb") as f:
        up = requests.post("https://api.x.com/2/media/upload", auth=auth,
                           files={"media": f}, data={"media_category": "tweet_image"}, timeout=60)
    up.raise_for_status()
    body = up.json()
    media_id = (body.get("data") or {}).get("id") or body.get("media_id_string")
    r = requests.post("https://api.x.com/2/tweets", auth=auth,
                      json={"text": caption, "media": {"media_ids": [media_id]}}, timeout=30)
    r.raise_for_status()
    tweet_id = r.json().get("data", {}).get("id")
    print(f"  → posted to X: {tweet_id}")
    return tweet_id


# ---------------------------------------------------------------- main
def handle_game(game_id, summary):
    use_cards = os.getenv("IMAGE_MODE", "screenshot") == "card"
    if use_cards:
        p = top_performer(summary)
        if not p:
            print("  no player stats yet, skipping")
            return False
        price = get_blitz_price(p["name"])
        img = render_card(p, price, OUT_DIR / f"{game_id}.png")
    else:
        if not ranked_performers(summary, 1):
            print("  no player stats yet, skipping")
            return False
        p, price, img = find_on_blitz(summary, game_id)
        if not p:
            print("  none of this game's top scorers are on Blitz, skipping")
            return True  # mark handled so it doesn't retry forever
    caption = write_caption(p, price)
    print(f"  top performer: {p['name']} ({p['fpts']} pts)\n  caption: {caption!r}")
    if MODE == "live":
        try:
            tweet_id = post_to_x(caption, img)
        except Exception as e:
            detail = getattr(getattr(e, "response", None), "text", "")[:300]
            hook = os.getenv("DISCORD_WEBHOOK_URL")
            if hook:
                requests.post(hook, data={"content": f"⚠️ X post failed: {e} {detail}"}, timeout=30)
            raise
        send_to_discord(caption, img, header=f"✅ **POSTED to X:** https://x.com/i/status/{tweet_id}")
    else:
        send_to_discord(caption, img)
    return True


def main():
    if "--test" in sys.argv:
        summary = json.loads((ROOT / "tests" / "sample_summary.json").read_text())
        handle_game("test", summary)
        return

    state = load_state()
    forced = os.getenv("FORCE_GAME_ID", "").strip()
    if forced:
        print(f"Forced run for game {forced}, mode={MODE}")
        handle_game(forced, get_summary(forced))
        return
    games = get_finished_games()
    print(f"{len(games)} finished games on the scoreboard, mode={MODE}")
    for g in games:
        if g["id"] in state["posted_game_ids"]:
            continue
        print(f"- {g['name']} ({g['id']})")
        try:
            if handle_game(g["id"], get_summary(g["id"])):
                state["posted_game_ids"].append(g["id"])
                save_state(state)
        except Exception as e:
            print(f"  ! failed: {e}")  # will retry next run


if __name__ == "__main__":
    main()
