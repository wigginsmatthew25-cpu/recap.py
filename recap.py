# === START OF PART 1 ===
import os, re, sys, json, hashlib, asyncio, aiohttp
import redis.asyncio as redis
from urllib.parse import quote

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
print(">>> BOOTING NFL DAILY / PRIMETIME RECAP: GEMINI 4 ARGON <<<")

KEY = os.environ.get("GEMINI_API_KEY", "").strip()
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
SERVER_URL = os.environ.get("SERVER_URL", "https://editor-production-fd79.up.railway.app").strip().rstrip("/")
if "localhost" in SERVER_URL:
    SERVER_URL = "https://editor-production-fd79.up.railway.app"
G_API = "https://generativelanguage.googleapis.com/v1beta/models/gemini-4-argon:generateContent"
QUEUE_NAME = "recap_events_daily"

FANTASY_POS = {"QB", "RB", "WR", "TE", "K", "PK"}
NON_FANTASY_RE = re.compile(r"\b(OL|OT|OG|C|DL|DE|DT|EDGE|LB|ILB|OLB|CB|S|DB|FS|SS|LS|P)\s+[A-Z]")

PROMPT = (
    "You are an expert NFL fantasy football editor creating a Daily/Primetime Fantasy Recap in strict JSON format.\n"
    "CRITICAL RULES:\n"
    "1. STRICTLY FANTASY-RELEVANT SKILL PLAYERS ONLY: Include ONLY fantasy-relevant QBs, RBs, WRs, TEs, and Kickers. "
    "COMPLETELY EXCLUDE all offensive linemen (OL, OT, OG, C), all defensive players (CB, S, LB, DE, DT, EDGE, DB), punters, and long snappers.\n"
    "2. Synthesize each player's updates into their latest status (keep every bullet to ONE short, scannable line—no fluff).\n"
    "3. Populate 'discord_recap' using native Discord Markdown (bold ** and underline __) so it renders cleanly for listeners:\n"
    "   🏈 **__NFL FANTASY WIRE RECAP__**\n\n"
    "   🔴 **__RULED OUT, INACTIVES & IR MOVES__**\n"
    "   • **Player Name** (POS, Team) — Short injury/status note\n\n"
    "   🟡 **__QUESTIONABLE, DNP & LIMITED__**\n"
    "   • **Player Name** (POS, Team) — Short practice/status note\n\n"
    "   🟢 **__CLEARED, ACTIVE & UPGRADES__**\n"
    "   • **Player Name** (POS, Team) — Short status note\n\n"
    "   🔄 **__NOTABLE FANTASY ROSTER MOVES__**\n"
    "   • **Player Name** (POS, Team) — Short transaction note\n"
    "   (Skip any section above that has zero players in this batch.)\n"
    "4. Populate 'social_cards' with up to 3 compact social summary cards covering the biggest updates in this batch: "
    "OUT & INACTIVES, INJURY & PRACTICE WATCH, and CLEARED & UPGRADES (skip any category with zero players).\n"
    "   - In each card's 'bullets' array, provide up to 5 ultra-short bullet strings (under 42 characters each, NO hashtags #, NO bold **, formatted as: POS Player (TM): Short status)."
)

DAILY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "discord_recap": {"type": "STRING"},
        "social_cards": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "category": {
                        "type": "STRING",
                        "enum": ["OUT & INACTIVES", "INJURY & PRACTICE WATCH", "CLEARED & UPGRADES"]
                    },
                    "bullets": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"}
                    }
                },
                "required": ["category", "bullets"]
            }
        }
    },
    "required": ["discord_recap", "social_cards"]
}

CARD_STYLES = {
    "OUT & INACTIVES": ("🚨 FANTASY OUT & INACTIVES REPORT", 15548997),
    "INJURY & PRACTICE WATCH": ("⏳ FANTASY INJURY & PRACTICE WATCH", 16753920),
    "CLEARED & UPGRADES": ("✅ FANTASY CLEARED & UPGRADES", 5763719)
}

async def fetch(session, method, url, **kwargs):
    for _ in range(4):
        try:
            async with session.request(method, url, timeout=180, **kwargs) as r:
                if r.status in [200, 201, 204]:
                    if r.status == 204:
                        return True
                    return await r.json() if "json" in r.headers.get("Content-Type", "") else await r.text()
                elif r.status == 429:
                    retry_after = 3.0
                    try:
                        err_json = await r.json()
                        retry_after = float(err_json.get("retry_after", 3.0)) + 0.5
                    except Exception:
                        pass
                    await asyncio.sleep(retry_after)
                else:
                    print(f"HTTP Error {r.status}: {await r.text()}")
        except Exception as e:
            print(f"Network Error: {e}")
            await asyncio.sleep(2)
    return None

def chunk_for_discord(text, max_len=1900):
    clean = re.sub(r"```[a-zA-Z]*\n?", "", text.strip())
    clean = clean.replace("```", "").strip()

    chunks = []
    current_lines = []
    current_len = 0

    for line in clean.split("\n"):
        if NON_FANTASY_RE.search(line):
            continue
        line_len = len(line) + 1
        is_section_hdr = any(line.strip().startswith(ic) for ic in ["🔴", "🟡", "🟢", "🔄"])
        if current_lines and (current_len + line_len > max_len or (is_section_hdr and current_len > 1200)):
            chunks.append("\n".join(current_lines).strip())
            current_lines = [line]
            current_len = line_len
        else:
            current_lines.append(line)
            current_len += line_len

    if current_lines:
        chunks.append("\n".join(current_lines).strip())
    return chunks
# === END OF PART 1 ===
# === START OF PART 2 ===
def format_card_post(header, raw_bullets, max_chars=265):
    lines = []
    for b in raw_bullets:
        clean_ln = str(b).replace("#", "").replace("**", "").strip()
        clean_ln = re.sub(r"^[•\-\*\s]+", "", clean_ln).strip()
        if not clean_ln or NON_FANTASY_RE.search(clean_ln):
            continue
        if any(clean_ln.lower().startswith(w) for w in ["none", "no updates", "no news", "n/a", "nothing"]):
            continue
        bullet_ln = f"• {clean_ln}"
        if len(bullet_ln) > 65:
            bullet_ln = bullet_ln[:62].strip() + "..."
        lines.append(bullet_ln)

    if not lines:
        return None

    fitted = []
    cur_len = len(header) + 1
    for ln in lines:
        if cur_len + len(ln) + 1 <= max_chars:
            fitted.append(ln)
            cur_len += len(ln) + 1
        else:
            break

    if not fitted:
        fitted.append(lines[0][:max_chars - len(header) - 5] + "...")

    return f"{header}\n" + "\n".join(fitted)

async def main():
    if not KEY or not WEBHOOK:
        print("❌ Missing API keys or Webhook URL. Exiting.")
        return

    db = redis.from_url(REDIS_URL, socket_timeout=30)
    print("🔄 Checking 'recap_events_daily' for new fantasy wire events...")

    raw_daily = await db.lrange(QUEUE_NAME, 0, -1)
    if not raw_daily:
        print("ℹ️ No new events in 'recap_events_daily' since last recap run. Exiting cleanly.")
        await db.aclose()
        return

    events = []
    for item in raw_daily:
        try:
            decoded = item.decode("utf-8") if isinstance(item, bytes) else str(item)
            events.append(json.loads(decoded))
        except Exception:
            pass

    grouped = {}
    for e in events:
        if isinstance(e, dict) and e.get("player"):
            pos = str(e.get("pos", "")).upper().strip()
            if pos and pos not in FANTASY_POS:
                continue
            tm = e.get("team", "NFL")
            plr = f"{pos} {e.get('player', '')}".strip()
            inj = e.get("injury", "")
            stat = e.get("status", "") or e.get("blurb", "")
            cat = e.get("category", "")
            entry = f"[{cat}] ({inj}) {stat}" if inj else f"[{cat}] {stat}"
            grouped.setdefault(tm, {}).setdefault(plr, [])
            if entry not in grouped[tm][plr]:
                grouped[tm][plr].append(entry)

    if not grouped:
        print("ℹ️ Only non-fantasy/IDP/OL events were in the queue. Clearing queue and exiting.")
        await db.delete(QUEUE_NAME)
        await db.aclose()
        return

    payload_data = {"fantasy_skill_players_by_team": grouped}
    print(f"✅ Filtered to fantasy skill players across {len(grouped)} teams. Dispatching to Gemini 4 Argon...")

    body = {
        "systemInstruction": {"parts": [{"text": PROMPT}]},
        "contents": [{"parts": [{"text": f"Wire Data:\n{json.dumps(payload_data)}"}]}],
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": "medium"},
            "responseMimeType": "application/json",
            "responseSchema": DAILY_SCHEMA,
            "maxOutputTokens": 32768
        }
    }

    async with aiohttp.ClientSession() as session:
        gem = await fetch(session, "POST", G_API, headers={"Content-Type": "application/json", "x-goog-api-key": KEY}, json=body)

        if not gem or not gem.get("candidates"):
            print("❌ Failed to generate recap. Events remain safely in Redis.")
            await db.aclose()
            return

        try:
            parts = gem["candidates"][0]["content"]["parts"]
            raw_json = [p["text"] for p in parts if "text" in p and not p.get("thought")][-1].strip()
            parsed_out = json.loads(raw_json)
        except Exception as e:
            print(f"❌ Failed to parse Gemini JSON response: {e}")
            await db.aclose()
            return

        full_recap = parsed_out.get("discord_recap", "").strip()
        social_cards = parsed_out.get("social_cards", [])

        chunks = chunk_for_discord(full_recap)
        print(f"🚀 Pushing reader-friendly Recap in {len(chunks)} message(s)...")

        all_sent = True
        for idx, chunk in enumerate(chunks):
            result = await fetch(session, "POST", WEBHOOK, json={"content": chunk})
            if result is None:
                all_sent = False
                print(f"❌ Failed to send chunk {idx + 1}/{len(chunks)}.")
                break
            await asyncio.sleep(1.5)

        embeds_to_send = []
        for card_obj in social_cards:
            cat_key = str(card_obj.get("category", "")).upper().strip()
            raw_bullets = card_obj.get("bullets", [])
            if cat_key not in CARD_STYLES or not raw_bullets:
                continue

            header, color = CARD_STYLES[cat_key]
            social_post = format_card_post(header, raw_bullets)
            if not social_post:
                continue

            cid = "dy_" + hashlib.md5(social_post.encode()).hexdigest()[:9]
            await db.set(f"draft:{cid}", social_post, ex=259200)

            pub_link = f"{SERVER_URL}/post?id={quote(cid)}"
            tw_link = f"https://twitter.com/intent/tweet?text={quote(social_post)}"
            bs_link = f"https://bsky.app/intent/compose?text={quote(social_post)}"

            embeds_to_send.append({
                "title": header,
                "description": f"```text\n{social_post}\n```",
                "color": color,
                "fields": [{
                    "name": "📲 1-Tap Social Actions",
                    "value": f"[🚀 1-Tap Publish]({pub_link}) | [🐦 Twitter]({tw_link}) | [🦋 Bsky]({bs_link})",
                    "inline": False
                }]
            })

        if embeds_to_send:
            await asyncio.sleep(1.5)
            await fetch(session, "POST", WEBHOOK, json={"embeds": embeds_to_send[:3]})
            print(f"✅ Posted {len(embeds_to_send[:3])} Recap social cards to Discord!")

        if all_sent:
            await db.delete(QUEUE_NAME)
            print("✅ Recap posted successfully and 'recap_events_daily' cleared for the next window!")

    await db.aclose()

if __name__ == "__main__":
    asyncio.run(main())
# === END OF PART 2 ===
