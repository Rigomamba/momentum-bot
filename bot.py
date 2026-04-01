import requests
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Momentum Sniper is LIVE\nUse /hot")

def get_trending():
    url = "https://api.dexscreener.com/latest/dex/search?q=sol"
    res = requests.get(url).json()

    tokens = []

    for pair in res["pairs"][:5]:
        name = pair["baseToken"]["name"]
        price = pair["priceUsd"]
        volume = pair["volume"]["h24"]
        liquidity = pair["liquidity"]["usd"]

        tokens.append(
            f"{name}\n💰 ${price}\n📊 Vol: {volume}\n💧 Liq: {liquidity}\n"
        )

    return "\n---\n".join(tokens)

async def hot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔥 Scanning...")
    data = get_trending()
    await update.message.reply_text(data)

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("hot", hot))

app.run_polling()
