import os
import time
import json
import html
import requests
from pathlib import Path
from urllib.parse import quote

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

SOLANATRACKER_API_KEY = os.getenv("SOLANATRACKER_API_KEY", "").strip()
ALERT_MIN_SCORE = float(os.getenv("ALERT_MIN_SCORE", "72"))
ALERT_INTERVAL_SECONDS = int(os.getenv("ALERT_INTERVAL_SECONDS", "90"))
REQUEST_TIMEOUT = 20

DATA_DIR = Path(".")
WALLETS_FILE = DATA_DIR / "watched_wallets.json"
CHAT_SETTINGS_FILE = DATA_DIR / "chat_settings.json"
ALERT_STATE_FILE = DATA_DIR / "alert_state.json"

MIN_SEND_LIQ = 12000
MIN_SEND_VOL = 25000
MIN_EARLY_LIQ = 4000
RESULT_LIMIT = 5

# =========================
# FILE HELPERS
# =========================
def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2))


WATCHED_WALLETS = load_json(WALLETS_FILE, [])
CHAT_SETTINGS = load_json(CHAT_SETTINGS_FILE, {})
ALERT_STATE = load_json(ALERT_STATE_FILE, {"seen_tokens": {}})

# =========================
# UTILS
# =========================
def safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def now_ts():
    return int(time.time())


def format_price(value):
    num = safe_float(value, 0.0)
    if num >= 1:
        return f"{num:,.4f}"
    if num >= 0.0001:
        return f"{num:.8f}".rstrip("0").rstrip(".")
    return f"{num:.12f}".rstrip("0").rstrip(".")


def format_num(value):
    num = safe_float(value, 0.0)
    if num >= 1_000_000:
        return f"{num/1_000_000:.2f}M"
    if num >= 1_000:
        return f"{num/1_000:.2f}K"
    return f"{num:.2f}"


def clamp(value, low, high):
    return max(low, min(high, value))


def escape(text):
    return html.escape(str(text))


def make_buy_link(token_address):
    # Generic Jupiter swap page link for quick manual action
    return f"https://jup.ag/swap/SOL-{quote(token_address)}"


def send_message(chat_id, text):
    requests.post(
        f"{BASE_URL}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=REQUEST_TIMEOUT,
    )


# =========================
# TELEGRAM HELPERS
# =========================
def get_updates(offset=None):
    params = {"timeout": 30}
    if offset is not None:
        params["offset"] = offset

    r = requests.get(f"{BASE_URL}/getUpdates", params=params, timeout=35)
    r.raise_for_status()
    return r.json()


# =========================
# DEXSCREENER HELPERS
# =========================
def dex_latest_profiles():
    r = requests.get(
        "https://api.dexscreener.com/token-profiles/latest/v1",
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def dex_boosts_latest():
    r = requests.get(
        "https://api.dexscreener.com/token-boosts/latest/v1",
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def dex_search(query):
    r = requests.get(
        "https://api.dexscreener.com/latest/dex/search",
        params={"q": query},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("pairs", [])


def dex_tokens(chain_id, token_addresses):
    if not token_addresses:
        return []
    joined = ",".join(token_addresses[:30])
    url = f"https://api.dexscreener.com/tokens/v1/{chain_id}/{joined}"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def dex_paid_orders(chain_id, token_address):
    url = f"https://api.dexscreener.com/orders/v1/{chain_id}/{token_address}"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


# =========================
# SOLANATRACKER HELPERS (OPTIONAL)
# =========================
def st_wallet_trades(wallet):
    if not SOLANATRACKER_API_KEY:
        return []
    url = f"https://data.solanatracker.io/wallet/{wallet}/trades"
    r = requests.get(
        url,
        headers={"x-api-key": SOLANATRACKER_API_KEY},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("trades", [])


# =========================
# SCORING
# =========================
def build_boost_lookup():
    lookup = {}
    try:
        boosts = dex_boosts_latest()
        for item in boosts:
            if item.get("chainId") == "solana":
                lookup[item.get("tokenAddress")] = item
    except Exception:
        pass
    return lookup


def pick_best_pairs(pairs):
    best = {}
    for pair in pairs:
        if pair.get("chainId") != "solana":
            continue
        addr = pair.get("baseToken", {}).get("address")
        if not addr:
            continue

        liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
        current = best.get(addr)
        if current is None or liquidity > safe_float(current.get("liquidity", {}).get("usd")):
            best[addr] = pair
    return list(best.values())


def token_age_minutes(pair):
    created_ms = pair.get("pairCreatedAt")
    if not created_ms:
        return 999999
    return max(0, int((now_ts() * 1000 - created_ms) / 60000))


def wallet_cluster_for_token(token_address, minutes=20):
    if not SOLANATRACKER_API_KEY or not WATCHED_WALLETS:
        return {"count": 0, "wallets": []}

    cutoff_ms = (now_ts() - minutes * 60) * 1000
    matched = []

    for wallet in WATCHED_WALLETS:
        try:
            trades = st_wallet_trades(wallet)
            for trade in trades[:25]:
                trade_time = int(trade.get("time", 0))
                to_addr = trade.get("to", {}).get("address")
                if trade_time >= cutoff_ms and to_addr == token_address:
                    matched.append(wallet)
                    break
        except Exception:
            continue

    unique = sorted(set(matched))
    return {"count": len(unique), "wallets": unique}


def paid_order_penalty(token_address):
    try:
        orders = dex_paid_orders("solana", token_address)
        approved = [o for o in orders if o.get("status") == "approved"]
        return 8 if approved else 0
    except Exception:
        return 0


def score_send(pair, boost_lookup):
    token = pair.get("baseToken", {})
    addr = token.get("address", "")
    volume = safe_float(pair.get("volume", {}).get("h24"))
    liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
    pc24 = safe_float(pair.get("priceChange", {}).get("h24"))
    pc6 = safe_float(pair.get("priceChange", {}).get("h6"))
    pc1 = safe_float(pair.get("priceChange", {}).get("h1"))
    fdv = safe_float(pair.get("fdv"))
    mcap = safe_float(pair.get("marketCap"))
    age_mins = token_age_minutes(pair)

    momentum = 0
    momentum += clamp(volume / 60000, 0, 22)
    momentum += clamp(max(pc1, 0) / 2, 0, 10)
    momentum += clamp(max(pc6, 0) / 3, 0, 8)
    momentum += clamp(max(pc24, 0) / 6, 0, 10)

    quality = 0
    quality += clamp(liquidity / 10000, 0, 18)
    liq_to_fdv = liquidity / fdv if fdv > 0 else 0
    if 0.01 <= liq_to_fdv <= 0.35:
        quality += 7
    elif liq_to_fdv > 0:
        quality += 3

    freshness = 0
    if age_mins <= 60:
        freshness = 10
    elif age_mins <= 240:
        freshness = 7
    elif age_mins <= 1440:
        freshness = 3

    wallet_cluster = wallet_cluster_for_token(addr, minutes=20)
    wallet_score = 0
    if wallet_cluster["count"] >= 3:
        wallet_score = 18
    elif wallet_cluster["count"] == 2:
        wallet_score = 12
    elif wallet_cluster["count"] == 1:
        wallet_score = 6

    risk_penalty = 0
    if liquidity < MIN_SEND_LIQ:
        risk_penalty += 10
    if pc24 > 120:
        risk_penalty += 8
    if fdv > 0 and liquidity > 0 and fdv / liquidity > 80:
        risk_penalty += 6
    if addr in boost_lookup:
        risk_penalty += 6
    risk_penalty += paid_order_penalty(addr)

    score = momentum + quality + freshness + wallet_score - risk_penalty
    score = round(clamp(score, 0, 100), 1)

    label = "WATCH"
    if score >= 82:
        label = "A-TIER RUNNER"
    elif score >= 72:
        label = "B-TIER SEND"
    elif score >= 60:
        label = "C-TIER SCALP"

    return {
        "score": score,
        "label": label,
        "wallet_cluster": wallet_cluster,
        "metrics": {
            "volume": volume,
            "liquidity": liquidity,
            "pc24": pc24,
            "pc6": pc6,
            "pc1": pc1,
            "fdv": fdv,
            "mcap": mcap,
            "age_mins": age_mins,
            "boosted": addr in boost_lookup,
        },
    }


def score_early(pair, boost_lookup):
    token = pair.get("baseToken", {})
    addr = token.get("address", "")
    volume = safe_float(pair.get("volume", {}).get("h24"))
    liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
    pc24 = safe_float(pair.get("priceChange", {}).get("h24"))
    pc1 = safe_float(pair.get("priceChange", {}).get("h1"))
    age_mins = token_age_minutes(pair)

    score = 0
    score += clamp((60 - min(age_mins, 60)) / 2, 0, 30)  # newest gets more
    score += clamp(liquidity / 3000, 0, 20)
    score += clamp(volume / 12000, 0, 20)
    score += clamp(max(pc1, 0) / 2, 0, 12)
    score += clamp(max(pc24, 0) / 5, 0, 10)

    wallet_cluster = wallet_cluster_for_token(addr, minutes=15)
    if wallet_cluster["count"] >= 2:
        score += 12
    elif wallet_cluster["count"] == 1:
        score += 6

    if liquidity < MIN_EARLY_LIQ:
        score -= 12
    if pc24 > 150:
        score -= 8
    if addr in boost_lookup:
        score -= 4
    score -= paid_order_penalty(addr)

    score = round(clamp(score, 0, 100), 1)

    label = "EARLY WATCH"
    if score >= 80:
        label = "EARLY HOT"
    elif score >= 68:
        label = "EARLY RUNNER"

    return {
        "score": score,
        "label": label,
        "wallet_cluster": wallet_cluster,
        "metrics": {
            "volume": volume,
            "liquidity": liquidity,
            "pc24": pc24,
            "pc1": pc1,
            "age_mins": age_mins,
            "boosted": addr in boost_lookup,
        },
    }


# =========================
# FORMATTERS
# =========================
def format_pair_message(pair, scored, mode="send"):
    token = pair.get("baseToken", {})
    addr = token.get("address", "N/A")
    name = token.get("name", "Unknown")
    symbol = token.get("symbol", "")
    url = pair.get("url", "")
    price = pair.get("priceUsd", "0")
    m = scored["metrics"]

    wallet_line = "0"
    if scored["wallet_cluster"]["count"] > 0:
        wallet_line = f"{scored['wallet_cluster']['count']} tracked wallet(s)"

    title = f"{escape(name)}"
    if symbol:
        title += f" ({escape(symbol)})"

    parts = [
        f"🚨 <b>{escape(scored['label'])}</b>",
        "",
        title,
        f"🔥 <b>Score:</b> {scored['score']}/100",
        f"💰 <b>Price:</b> ${escape(format_price(price))}",
        f"📊 <b>Vol 24h:</b> {escape(format_num(m['volume']))}",
        f"💧 <b>Liq:</b> {escape(format_num(m['liquidity']))}",
    ]

    if mode == "send":
        parts += [
            f"📈 <b>1h:</b> {m['pc1']:.2f}%",
            f"📈 <b>6h:</b> {m['pc6']:.2f}%",
            f"📈 <b>24h:</b> {m['pc24']:.2f}%",
        ]
    else:
        parts += [
            f"📈 <b>1h:</b> {m['pc1']:.2f}%",
            f"📈 <b>24h:</b> {m['pc24']:.2f}%",
        ]

    parts += [
        f"⏱️ <b>Age:</b> {m['age_mins']} min",
        f"👛 <b>Wallet cluster:</b> {escape(wallet_line)}",
        f"📢 <b>Boosted:</b> {'Yes' if m['boosted'] else 'No'}",
        "",
        "📋 <b>CA:</b>",
        f"<code>{escape(addr)}</code>",
        "",
        f"⚡ <b>Buy:</b> {escape(make_buy_link(addr))}",
    ]

    if url:
        parts.append(f"🔗 <b>Chart:</b> {escape(url)}")

    return "\n".join(parts)


# =========================
# SCANNERS
# =========================
def get_send_candidates():
    boost_lookup = build_boost_lookup()

    queries = ["solana", "raydium solana", "pump solana", "bonk solana"]
    raw_pairs = []
    for q in queries:
        try:
            raw_pairs.extend(dex_search(q))
        except Exception:
            continue

    pairs = pick_best_pairs(raw_pairs)
    scored = []

    for pair in pairs:
        liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
        volume = safe_float(pair.get("volume", {}).get("h24"))
        if liquidity < MIN_SEND_LIQ or volume < MIN_SEND_VOL:
            continue

        result = score_send(pair, boost_lookup)
        if result["score"] >= 55:
            scored.append((pair, result))

    scored.sort(key=lambda x: x[1]["score"], reverse=True)
    return scored[:RESULT_LIMIT]


def get_early_candidates():
    boost_lookup = build_boost_lookup()

    profiles = dex_latest_profiles()
    sol_addrs = []
    for item in profiles:
        if item.get("chainId") == "solana" and item.get("tokenAddress"):
            sol_addrs.append(item["tokenAddress"])

    # newest first, take a chunk, hydrate pairs
    sol_addrs = sol_addrs[:30]
    raw_pairs = dex_tokens("solana", sol_addrs)
    pairs = pick_best_pairs(raw_pairs)

    scored = []
    for pair in pairs:
        liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
        age_mins = token_age_minutes(pair)
        if liquidity < MIN_EARLY_LIQ:
            continue
        if age_mins > 720:
            continue

        result = score_early(pair, boost_lookup)
        if result["score"] >= 50:
            scored.append((pair, result))

    scored.sort(key=lambda x: x[1]["score"], reverse=True)
    return scored[:RESULT_LIMIT]


def get_wallet_cluster_summary():
    if not SOLANATRACKER_API_KEY:
        return "Set SOLANATRACKER_API_KEY first to use wallet clustering."

    if not WATCHED_WALLETS:
        return "No watched wallets yet. Use /watchwallet <address>"

    counts = {}
    cutoff_ms = (now_ts() - 20 * 60) * 1000

    for wallet in WATCHED_WALLETS:
        try:
            trades = st_wallet_trades(wallet)
            for trade in trades[:25]:
                trade_time = int(trade.get("time", 0))
                to_addr = trade.get("to", {}).get("address")
                token_name = trade.get("to", {}).get("token", {}).get("name", "Unknown")
                token_symbol = trade.get("to", {}).get("token", {}).get("symbol", "")
                if trade_time < cutoff_ms or not to_addr:
                    continue

                item = counts.setdefault(
                    to_addr,
                    {"wallets": set(), "name": token_name, "symbol": token_symbol},
                )
                item["wallets"].add(wallet)
        except Exception:
            continue

    rows = []
    for addr, item in counts.items():
        if len(item["wallets"]) >= 2:
            label = item["name"]
            if item["symbol"]:
                label += f" ({item['symbol']})"
            rows.append((len(item["wallets"]), label, addr))

    rows.sort(reverse=True)

    if not rows:
        return "No multi-wallet cluster buys in the last 20 minutes."

    lines = ["🧠 <b>Wallet cluster alerts</b>", ""]
    for count, label, addr in rows[:8]:
        lines.append(f"• {escape(label)} — {count} tracked wallets")
        lines.append(f"<code>{escape(addr)}</code>")
        lines.append("")

    return "\n".join(lines).strip()


# =========================
# COMMAND HANDLERS
# =========================
def ensure_chat(chat_id):
    key = str(chat_id)
    if key not in CHAT_SETTINGS:
        CHAT_SETTINGS[key] = {"alerts": False}
        save_json(CHAT_SETTINGS_FILE, CHAT_SETTINGS)
    return CHAT_SETTINGS[key]


def handle_command(chat_id, text):
    ensure_chat(chat_id)
    parts = text.strip().split()
    cmd = parts[0].lower()

    if cmd == "/start" or cmd == "/help":
        send_message(
            chat_id,
            "🚀 <b>Sniper Bot is LIVE</b>\n\n"
            "<b>Commands</b>\n"
            "/send - best current setups\n"
            "/early - fresher Solana setups\n"
            "/smart - tracked-wallet cluster buys\n"
            "/watchwallet &lt;address&gt; - add wallet\n"
            "/unwatchwallet &lt;address&gt; - remove wallet\n"
            "/wallets - list watched wallets\n"
            "/alerts on - enable push alerts\n"
            "/alerts off - disable push alerts",
        )
        return

    if cmd == "/send":
        send_message(chat_id, "🔥 Scanning live send setups...")
        try:
            scored = get_send_candidates()
            if not scored:
                send_message(chat_id, "No good send setups right now.")
                return
            msg = "🔥 <b>Top send setups</b>\n\n" + "\n\n---\n\n".join(
                format_pair_message(pair, result, mode="send") for pair, result in scored
            )
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"Send scan error: {escape(e)}")
        return

    if cmd == "/early":
        send_message(chat_id, "🆕 Scanning early runners...")
        try:
            scored = get_early_candidates()
            if not scored:
                send_message(chat_id, "No early runners right now.")
                return
            msg = "🆕 <b>Top early runners</b>\n\n" + "\n\n---\n\n".join(
                format_pair_message(pair, result, mode="early") for pair, result in scored
            )
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"Early scan error: {escape(e)}")
        return

    if cmd == "/watchwallet":
        if len(parts) < 2:
            send_message(chat_id, "Use: /watchwallet <wallet_address>")
            return
        wallet = parts[1].strip()
        if wallet not in WATCHED_WALLETS:
            WATCHED_WALLETS.append(wallet)
            save_json(WALLETS_FILE, WATCHED_WALLETS)
        send_message(chat_id, f"Added watched wallet:\n<code>{escape(wallet)}</code>")
        return

    if cmd == "/unwatchwallet":
        if len(parts) < 2:
            send_message(chat_id, "Use: /unwatchwallet <wallet_address>")
            return
        wallet = parts[1].strip()
        if wallet in WATCHED_WALLETS:
            WATCHED_WALLETS.remove(wallet)
            save_json(WALLETS_FILE, WATCHED_WALLETS)
            send_message(chat_id, f"Removed watched wallet:\n<code>{escape(wallet)}</code>")
        else:
            send_message(chat_id, "That wallet was not in your list.")
        return

    if cmd == "/wallets":
        if not WATCHED_WALLETS:
            send_message(chat_id, "No watched wallets yet.")
            return
        msg = "👛 <b>Watched wallets</b>\n\n" + "\n".join(
            f"• <code>{escape(w)}</code>" for w in WATCHED_WALLETS
        )
        send_message(chat_id, msg)
        return

    if cmd == "/smart":
        try:
            send_message(chat_id, get_wallet_cluster_summary())
        except Exception as e:
            send_message(chat_id, f"Wallet cluster error: {escape(e)}")
        return

    if cmd == "/alerts":
        if len(parts) < 2:
            state = "on" if CHAT_SETTINGS[str(chat_id)].get("alerts") else "off"
            send_message(chat_id, f"Alerts are currently {state}. Use /alerts on or /alerts off")
            return
        toggle = parts[1].lower()
        if toggle not in ("on", "off"):
            send_message(chat_id, "Use /alerts on or /alerts off")
            return
        CHAT_SETTINGS[str(chat_id)]["alerts"] = toggle == "on"
        save_json(CHAT_SETTINGS_FILE, CHAT_SETTINGS)
        send_message(chat_id, f"Alerts turned {toggle}.")
        return


# =========================
# AUTO ALERT LOOP
# =========================
def alert_loop():
    enabled_chats = [cid for cid, s in CHAT_SETTINGS.items() if s.get("alerts")]
    if not enabled_chats:
        return

    try:
        setups = get_send_candidates()
    except Exception as e:
        print(f"alert scan error: {e}")
        return

    for pair, result in setups:
        token_addr = pair.get("baseToken", {}).get("address")
        if not token_addr or result["score"] < ALERT_MIN_SCORE:
            continue

        last_sent = ALERT_STATE["seen_tokens"].get(token_addr, 0)
        if now_ts() - last_sent < 3600:
            continue

        msg = "🚨 <b>Auto alert</b>\n\n" + format_pair_message(pair, result, mode="send")
        for chat_id in enabled_chats:
            try:
                send_message(chat_id, msg)
            except Exception:
                pass

        ALERT_STATE["seen_tokens"][token_addr] = now_ts()
        save_json(ALERT_STATE_FILE, ALERT_STATE)


# =========================
# MAIN LOOP
# =========================
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing")

    offset = None
    last_alert_run = 0

    while True:
        try:
            # periodic alerts
            if now_ts() - last_alert_run >= ALERT_INTERVAL_SECONDS:
                alert_loop()
                last_alert_run = now_ts()

            # telegram updates
            data = get_updates(offset)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue

                chat_id = message["chat"]["id"]
                text = message.get("text", "").strip()
                if text.startswith("/"):
                    handle_command(chat_id, text)

        except Exception as e:
            print(f"main loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
