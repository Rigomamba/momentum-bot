import os
import time
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

MIN_LIQUIDITY = 10000
MIN_VOLUME_24H = 20000
MIN_PRICE_CHANGE_24H = 2
RESULT_LIMIT = 5


def send_message(chat_id, text):
    requests.post(
        f"{BASE_URL}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )


def safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def format_money(value):
    num = safe_float(value, 0.0)
    if num >= 1:
        return f"{num:,.2f}"
    return f"{num:.8f}".rstrip("0").rstrip(".")


def score_pair(pair):
    volume = safe_float(pair.get("volume", {}).get("h24"))
    liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
    price_change = safe_float(pair.get("priceChange", {}).get("h24"))

    score = 0
    score += min(volume / 10000, 40)
    score += min(liquidity / 10000, 30)
    score += min(max(price_change, 0), 30)
    return round(score, 1)


def pair_to_message(pair, include_score=True):
    base = pair.get("baseToken", {})
    name = base.get("name", "Unknown")
    symbol = base.get("symbol", "")
    address = base.get("address", "N/A")
    price = pair.get("priceUsd", "N/A")
    volume = safe_float(pair.get("volume", {}).get("h24"))
    liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
    price_change = safe_float(pair.get("priceChange", {}).get("h24"))
    dex_url = pair.get("url", "")

    title = f"{name}"
    if symbol:
        title += f" ({symbol})"

    parts = [title]

    if include_score:
        parts.append(f"🔥 *Score:* {score_pair(pair)}")

    parts.append(f"💰 *Price:* ${format_money(price)}")
    parts.append(f"📈 *24h Change:* {price_change:.2f}%")
    parts.append(f"📊 *Vol:* {volume:,.2f}")
    parts.append(f"💧 *Liq:* {liquidity:,.2f}")
    parts.append("")
    parts.append(f"📋 *CA:*")
    parts.append(f"`{address}`")

    if dex_url:
        parts.append("")
        parts.append(f"🔗 *Chart:* {dex_url}")

    return "\n".join(parts)


def fetch_pairs(search_term="solana"):
    url = f"https://api.dexscreener.com/latest/dex/search?q={search_term}"
    res = requests.get(url, timeout=20)
    res.raise_for_status()
    data = res.json()
    return data.get("pairs", [])


def get_hot_tokens():
    pairs = fetch_pairs("solana")

    if not pairs:
        return "No tokens found right now."

    filtered = []
    seen = set()

    for pair in pairs:
        base = pair.get("baseToken", {})
        address = base.get("address", "")
        chain_id = pair.get("chainId", "")

        if chain_id != "solana":
            continue

        if not address or address in seen:
            continue

        volume = safe_float(pair.get("volume", {}).get("h24"))
        liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
        price_change = safe_float(pair.get("priceChange", {}).get("h24"))

        if liquidity < MIN_LIQUIDITY:
            continue

        if volume < MIN_VOLUME_24H:
            continue

        if price_change < MIN_PRICE_CHANGE_24H:
            continue

        seen.add(address)
        filtered.append(pair)

    if filtered:
        filtered.sort(key=score_pair, reverse=True)
        messages = [pair_to_message(pair, include_score=True) for pair in filtered[:RESULT_LIMIT]]
        return "🔥 *Strong momentum setups:*\n\n" + "\n\n---\n\n".join(messages)

    fallback = []
    seen = set()

    for pair in pairs:
        base = pair.get("baseToken", {})
        address = base.get("address", "")
        chain_id = pair.get("chainId", "")

        if chain_id != "solana":
            continue

        if not address or address in seen:
            continue

        seen.add(address)
        fallback.append(pair)

    fallback.sort(
        key=lambda x: (
            safe_float(x.get("volume", {}).get("h24")),
            safe_float(x.get("liquidity", {}).get("usd")),
        ),
        reverse=True,
    )

    if not fallback:
        return "No strong setups and no fallback tokens found right now."

    messages = [pair_to_message(pair, include_score=False) for pair in fallback[:RESULT_LIMIT]]
    return "⚠️ *No strong setups right now — showing top volume instead:*\n\n" + "\n\n---\n\n".join(messages)


def get_new_tokens():
    pairs = fetch_pairs("sol")

    if not pairs:
        return "No new Solana tokens found right now."

    seen = set()
    fresh = []

    for pair in pairs:
        base = pair.get("baseToken", {})
        address = base.get("address", "")
        chain_id = pair.get("chainId", "")
        liquidity = safe_float(pair.get("liquidity", {}).get("usd"))

        if chain_id != "solana":
            continue

        if not address or address in seen:
            continue

        if liquidity < 5000:
            continue

        seen.add(address)
        fresh.append(pair)

    if not fresh:
        return "No fresh Solana pairs with usable liquidity found right now."

    fresh.sort(
        key=lambda x: (
            safe_float(x.get("priceChange", {}).get("h24")),
            safe_float(x.get("volume", {}).get("h24")),
        ),
        reverse=True,
    )

    messages = [pair_to_message(pair, include_score=True) for pair in fresh[:RESULT_LIMIT]]
    return "🆕 *Fresh / newer Solana setups:*\n\n" + "\n\n---\n\n".join(messages)


def handle_message(message):
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip().lower()

    if text == "/start":
        send_message(
            chat_id,
            "🚀 *Momentum Sniper is LIVE*\n\n"
            "*Commands:*\n"
            "/hot - strongest filtered Solana momentum setups\n"
            "/new - fresher Solana setups\n"
            "/help - show commands",
        )

    elif text == "/help":
        send_message(
            chat_id,
            "*Commands:*\n"
            "/hot - strongest filtered Solana momentum setups\n"
            "/new - fresher Solana setups\n"
            "/help - show commands",
        )

    elif text == "/hot":
        send_message(chat_id, "🔥 Scanning for strong momentum setups...")
        try:
            result = get_hot_tokens()
            send_message(chat_id, result)
        except Exception as e:
            send_message(chat_id, f"Error scanning market: {e}")

    elif text == "/new":
        send_message(chat_id, "🆕 Scanning for fresher Solana setups...")
        try:
            result = get_new_tokens()
            send_message(chat_id, result)
        except Exception as e:
            send_message(chat_id, f"Error scanning new tokens: {e}")


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
