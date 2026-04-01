import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Momentum Sniper is LIVE\nUse /hot")


def get_trending():
    url = "https://api.dexscreener.com/latest/dex/search?q=sol"
    res = requests.get(url).json()

    tokens = []

    for pair in res.get("pairs", [])[:5]:
        name = pair.get("baseToken", {}).get("name", "Unknown")
        price = pair.get("priceUsd", "N/A")
        volume = pair.get("volume", {}).get("h24", "N/A")
        liquidity = pair.get("liquidity", {}).get("usd", "N/A")

        tokens.append(
            f"{name}\n💰 ${price}\n📊 Vol: {volume}\n💧 Liq: {liquidity}"
        )

    return "\n---\n".join(tokens)


async def hot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔥 Scanning...")
    data = get_trending()
    await update.message.reply_text(data)


def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("hot", hot))

    app.run_polling()


if __name__ == "__main__":
    main()
