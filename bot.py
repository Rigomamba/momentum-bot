import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Momentum Sniper is LIVE\nUse /hot")


def get_trending():
    url = "https://api.dexscreener.com/latest/dex/search?q=sol"
    res = requests.get(url, timeout=15)
    res.raise_for_status()
    data = res.json()

    pairs = data.get("pairs", [])
    if not pairs:
        return "No tokens found right now."

    tokens = []

    for pair in pairs[:5]:
        base = pair.get("baseToken", {})
        volume = pair.get("volume", {})
        liquidity = pair.get("liquidity", {})

        name = base.get("name", "Unknown")
        price = pair.get("priceUsd", "N/A")
        vol24 = volume.get("h24", "N/A")
        liq_usd = liquidity.get("usd", "N/A")

        tokens.append(
            f"{name}\n💰 ${price}\n📊 Vol: {vol24}\n💧 Liq: {liq_usd}"
        )

    return "\n---\n".join(tokens)


async def hot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔥 Scanning...")

    try:
        data = get_trending()
        await update.message.reply_text(data)
    except Exception as e:
        await update.message.reply_text(f"Error scanning market: {e}")


def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN is missing from Railway variables")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("hot", hot))

    app.run_polling()


if __name__ == "__main__":
    main()
