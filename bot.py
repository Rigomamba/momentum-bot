import os
import time
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


def send_message(chat_id, text):
    requests.post(
        f"{BASE_URL}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=20,
    )


def get_trending():
    url = "https://api.dexscreener.com/latest/dex/search?q=sol"
    res = requests.get(url, timeout=20)
    res.raise_for_status()
    data = res.json()

    pairs = data.get("pairs", [])
    if not pairs:
        return "No tokens found right now."

    tokens = []
    for pair in pairs[:5]:
        name = pair.get("baseToken", {}).get("name", "Unknown")
        price = pair.get("priceUsd", "N/A")
        volume = pair.get("volume", {}).get("h24", "N/A")
        liquidity = pair.get("liquidity", {}).get("usd", "N/A")

        tokens.append(
            f"{name}\n💰 ${price}\n📊 Vol: {volume}\n💧 Liq: {liquidity}"
        )

    return "\n---\n".join(tokens)


def handle_message(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text == "/start":
        send_message(chat_id, "🚀 Momentum Sniper is LIVE\nUse /hot")
    elif text == "/hot":
        send_message(chat_id, "🔥 Scanning...")
        try:
            trending = get_trending()
            send_message(chat_id, trending)
        except Exception as e:
            send_message(chat_id, f"Error scanning market: {e}")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing from Railway variables")

    offset = None

    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset

            response = requests.get(
                f"{BASE_URL}/getUpdates",
                params=params,
                timeout=35,
            )
            response.raise_for_status()
            data = response.json()

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    handle_message(message)

        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
