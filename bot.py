import requests
import os
from telegram.ext import Updater, CommandHandler

TOKEN = os.getenv("BOT_TOKEN")

def start(update, context):
    update.message.reply_text("🚀 Momentum Sniper is LIVE\nUse /hot")

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

def hot(update, context):
    update.message.reply_text("🔥 Scanning...")
    data = get_trending()
    update.message.reply_text(data)

updater = Updater(TOKEN, use_context=True)

dp = updater.dispatcher
dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("hot", hot))

updater.start_polling()
updater.idle()
