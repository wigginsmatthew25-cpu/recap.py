import os, sys, json, asyncio, aiohttp
import redis.asyncio as redis

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

KEY = os.environ.get("GEMINI_API_KEY", "").strip()
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
G_API = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"

PROMPT = (
    "You are an expert NFL fantasy editor. Below is a raw list of today's confirmed injury, roster, and game status alerts.\n"
    "Write a concise, high-impact Daily Digest for Discord.\n"
    "Rules:\n"
    "- Group into 3 sections: 🚨 KEY INJURIES & OUTS, ⏳ GAME-TIME DECISIONS, 📋 ROSTER MOVES.\n"
    "- Use bullet points formatted as: **Player Name** (POS - Team): Status / Injury.\n"
    "- Keep each bullet to 1 line.\n"
    "- Do not include chatter or generic filler prose."
)

async def main():
    print(">>> RUNNING DAILY RECAP DIGEST <<<")
    db = redis.from_url(REDIS_URL, socket_timeout=30)
    
    # 1. Pull events collected by editor.py
    raw_events = await db.lrange("recap_events_daily", 0, -1)
    if not raw_events:
        print("ℹ️ No recap events found in Redis. Exiting.")
        return

    print(f"📦 Found {len(raw_events)} events to summarize.")
    events = [json.loads(e) for e in raw_events]
    
    # Format events for Gemini prompt
    event_lines = []
    for e in events:
        line = f"- {e.get('category')}: {e.get('blurb')} (#{e.get('team')})"
        event_lines.append(line)
    
    content = "\n".join(event_lines)

    # 2. Generate summary with Gemini
    async with aiohttp.ClientSession() as session:
        body = {
            "systemInstruction": {"parts": [{"text": PROMPT}]},
            "contents": [{"parts": [{"text": f"Today's Events:\n{content}"}]}],
            "generationConfig": {"temperature": 0.2}
        }
        
        digest_text = ""
        try:
            async with session.post(
                G_API,
                headers={"Content-Type": "application/json", "x-goog-api-key": KEY},
                json=body
            ) as r:
                if r.status == 200:
                    res = await r.json()
                    digest_text = res["candidates"][0]["content"]["parts"][0]["text"].strip()
                else:
                    print(f"⚠️ Gemini API error: {r.status}")
                    return
        except Exception as e:
            print(f"⚠️ Failed calling Gemini: {e}")
            return

        # 3. Post embed to Discord
        embed = {
            "title": "🏈 Daily NFL Fantasy Wire Digest",
            "description": digest_text[:4000],
            "color": 3447003,
            "footer": {"text": f"Recap of {len(events)} wire updates today"}
        }

        try:
            async with session.post(WEBHOOK, json={"embeds": [embed]}) as r:
                if r.status in [200, 204]:
                    print("✅ Daily recap posted successfully to Discord.")
                    # 4. Clear queue only after successful post
                    await db.delete("recap_events_daily")
                else:
                    print(f"⚠️ Discord webhook failed with status: {r.status}")
        except Exception as e:
            print(f"⚠️ Failed posting to Discord: {e}")

if __name__ == "__main__":
    asyncio.run(main())
