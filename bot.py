import os
import time
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

MIN_LIQUIDITY = 20000
MIN_VOLUME_24H = 50000
MIN_PRICE_CHANGE_24H = 5


def send_message(chat_id, text):
    requests.post(
        f"{BASE_URL}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=20,
    )


def score_pair(pair):
    volume = float(pair.get("volume", {}).get("h24") or 0)
    liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
    price_change = float(pair.get("priceChange", {}).get("h24") or 0)

    score = 0
    score += min(volume / 10000, 40)
    score += min(liquidity / 10000, 30)
    score += min(max(price_change, 0), 30)
    return round(score, 1)


def get_hot_tokens():
    url = "https://api.dexscreener.com/latest/dex/search?q=solana"
    res = requests.get(url, timeout=20)
    res.raise_for_status()
    data = res.json()

    pairs = data.get("pairs", [])
    if not pairs:
        return "No tokens found right now."

    filtered = []
    seen = set()

    for pair in pairs:
        base = pair.get("baseToken", {})
        name = base.get("name", "Unknown")
        address = base.get("address", "")
        volume = float(pair.get("volume", {}).get("h24") or 0)
        liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
        price_change = float(pair.get("priceChange", {}).get("h24") or 0)
        price = pair.get("priceUsd", "N/A")
        chain_id = pair.get("chainId", "")

        if chain_id != "solana":
            continue

        if not address or address in seen:
            continue

        if liquidity < MIN_LIQUIDITY:
            continue

        if volume < MIN_VOLUME_24H:
            continue

        if price_change < MIN_PRICE_CHANGE_24H:
            continue

        seen.add(address)

        filtered.append(
            {
                "name": name,
                "price": price,
                "volume": volume,
                "liquidity": liquidity,
                "price_change": price_change,
                "score": score_pair(pair),
            }
        )

    if not filtered:
        return "No strong momentum tokens found right now."

    filtered.sort(key=lambda x: x["score"], reverse=True)

    messages = []
    for token in filtered[:5]:
        messages.append(
            f"{token['name']}\n"
            f"🔥 Score: {token['score']}\n"
            f"💰 Price: ${token['price']}\n"
            f"📈 24h Change: {token['price_change']}%\n"
            f"📊 Vol: {token['volume']:.2f}\n"
            f"💧 Liq: {token['liquidity']:.2f}"
        )

    return "\n---\n".join(messages)


def handle_message(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text == "/start":
        send_message(
            chat_id,
            "🚀 Momentum Sniper is LIVE\n\nCommands:\n/hot - strongest filtered tokens",
        )
    elif text == "/hot":
        send_message(chat_id, "🔥 Scanning for strong momentum setups...")
        try:
            result = get_hot_tokens()
            send_message(chat_id, result)
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
