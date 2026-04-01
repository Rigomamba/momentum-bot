import os
import time
import json
import html
import requests
from pathlib import Path
from urllib.parse import quote

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

SOLANATRACKER_API_KEY = os.getenv("SOLANATRACKER_API_KEY", "").strip()
ALERT_MIN_SCORE = float(os.getenv("ALERT_MIN_SCORE", "68"))
ALERT_MIN_EARLY_SCORE = float(os.getenv("ALERT_MIN_EARLY_SCORE", "70"))
ALERT_INTERVAL_SECONDS = int(os.getenv("ALERT_INTERVAL_SECONDS", "30"))
REQUEST_TIMEOUT = 20

DATA_DIR = Path(".")
WALLETS_FILE = DATA_DIR / "watched_wallets.json"
CHAT_SETTINGS_FILE = DATA_DIR / "chat_settings.json"
ALERT_STATE_FILE = DATA_DIR / "alert_state.json"

RESULT_LIMIT = 5

# Balanced sniper settings
MIN_SEND_LIQ = 12000
MIN_SEND_VOL = 25000
MIN_EARLY_LIQ = 5000
MAX_EARLY_LIQ = 60000
MAX_SAFE_PRICE_CHANGE_24H = 85
MAX_SAFE_PRICE_CHANGE_1H = 35
IDEAL_MIN_AGE = 5
IDEAL_MAX_AGE = 180


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
    if num >= 1_000_000_000:
        return f"{num / 1_000_000_000:.2f}B"
    if num >= 1_000_000:
        return f"{num / 1_000_000:.2f}M"
    if num >= 1_000:
        return f"{num / 1_000:.2f}K"
    return f"{num:.2f}"


def clamp(value, low, high):
    return max(low, min(high, value))


def escape(text):
    return html.escape(str(text))


def make_buy_link(token_address):
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


def get_updates(offset=None):
    params = {"timeout": 25}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{BASE_URL}/getUpdates", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


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


def get_phase(age_mins, pc1, pc24, liquidity):
    if age_mins <= 45 and pc1 <= 18 and pc24 <= 35 and liquidity <= 40000:
        return "EARLY"
    if age_mins <= 240 and pc1 <= 35 and pc24 <= 85:
        return "MID"
    return "LATE"


def score_send(pair, boost_lookup):
    token = pair.get("baseToken", {})
    addr = token.get("address", "")
    volume = safe_float(pair.get("volume", {}).get("h24"))
    liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
    pc24 = safe_float(pair.get("priceChange", {}).get("h24"))
    pc6 = safe_float(pair.get("priceChange", {}).get("h6"))
    pc1 = safe_float(pair.get("priceChange", {}).get("h1"))
    fdv = safe_float(pair.get("fdv"))
    age_mins = token_age_minutes(pair)

    momentum = 0
    momentum += clamp(volume / 50000, 0, 24)
    momentum += clamp(max(pc1, 0) / 2, 0, 12)
    momentum += clamp(max(pc6, 0) / 3, 0, 10)
    momentum += clamp(max(pc24, 0) / 6, 0, 10)

    quality = 0
    quality += clamp(liquidity / 10000, 0, 18)

    liq_to_fdv = liquidity / fdv if fdv > 0 else 0
    if 0.02 <= liq_to_fdv <= 0.30:
        quality += 10
    elif 0.01 <= liq_to_fdv <= 0.40:
        quality += 6
    elif liq_to_fdv > 0:
        quality += 2

    freshness = 0
    if IDEAL_MIN_AGE <= age_mins <= 45:
        freshness = 12
    elif 46 <= age_mins <= IDEAL_MAX_AGE:
        freshness = 8
    elif age_mins <= 720:
        freshness = 4

    wallet_cluster = wallet_cluster_for_token(addr, minutes=20)
    wallet_score = 0
    if wallet_cluster["count"] >= 3:
        wallet_score = 20
    elif wallet_cluster["count"] == 2:
        wallet_score = 14
    elif wallet_cluster["count"] == 1:
        wallet_score = 6

    risk_penalty = 0
    if liquidity < MIN_SEND_LIQ:
        risk_penalty += 10
    if pc24 > MAX_SAFE_PRICE_CHANGE_24H:
        risk_penalty += 12
    if pc1 > MAX_SAFE_PRICE_CHANGE_1H:
        risk_penalty += 10
    if age_mins < 2:
        risk_penalty += 8
    if fdv > 0 and liquidity > 0 and fdv / liquidity > 90:
        risk_penalty += 6
    if addr in boost_lookup:
        risk_penalty += 4
    risk_penalty += paid_order_penalty(addr)

    score = momentum + quality + freshness + wallet_score - risk_penalty
    score = round(clamp(score, 0, 100), 1)

    phase = get_phase(age_mins, pc1, pc24, liquidity)

    label = "WATCH"
    if score >= 84:
        label = "A-TIER RUNNER"
    elif score >= 74:
        label = "B-TIER SEND"
    elif score >= 68:
        label = "C-TIER SCALP"

    return {
        "score": score,
        "label": label,
        "phase": phase,
        "wallet_cluster": wallet_cluster,
        "metrics": {
            "volume": volume,
            "liquidity": liquidity,
            "pc24": pc24,
            "pc6": pc6,
            "pc1": pc1,
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
    score += clamp((120 - min(age_mins, 120)) / 3, 0, 24)
    score += clamp(liquidity / 3500, 0, 20)
    score += clamp(volume / 12000, 0, 18)
    score += clamp(max(pc1, 0) / 2, 0, 12)
    score += clamp(max(pc24, 0) / 5, 0, 10)

    if 10 <= age_mins <= 90:
        score += 8
    if 8000 <= liquidity <= 40000:
        score += 6

    wallet_cluster = wallet_cluster_for_token(addr, minutes=15)
    if wallet_cluster["count"] >= 2:
        score += 14
    elif wallet_cluster["count"] == 1:
        score += 6

    if liquidity < MIN_EARLY_LIQ:
        score -= 12
    if liquidity > MAX_EARLY_LIQ:
        score -= 8
    if pc24 > 70:
        score -= 12
    if pc1 > 25:
        score -= 10
    if age_mins < 3:
        score -= 8
    if addr in boost_lookup:
        score -= 3
    score -= paid_order_penalty(addr)

    score = round(clamp(score, 0, 100), 1)
    phase = get_phase(age_mins, pc1, pc24, liquidity)

    label = "EARLY WATCH"
    if score >= 82:
        label = "EARLY HOT"
    elif score >= 70:
        label = "EARLY RUNNER"

    return {
        "score": score,
        "label": label,
        "phase": phase,
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


def format_pair_message(pair, scored, mode="send"):
    token = pair.get("baseToken", {})
    addr = token.get("address", "N/A")
    name = token.get("name", "Unknown")
    symbol = token.get("symbol", "")
    url = pair.get("url", "")
    price = pair.get("priceUsd", "0")
    market_cap = safe_float(pair.get("marketCap"))
    fdv = safe_float(pair.get("fdv"))
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
        f"🧠 <b>Phase:</b> {escape(scored['phase'])}",
        f"💰 <b>Price:</b> ${escape(format_price(price))}",
        f"🏦 <b>Market Cap:</b> {escape(format_num(market_cap))}" if market_cap > 0 else "🏦 <b>Market Cap:</b> N/A",
        f"🧮 <b>FDV:</b> {escape(format_num(fdv))}" if fdv > 0 else "🧮 <b>FDV:</b> N/A",
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


def get_send_candidates():
    boost_lookup = build_boost_lookup()
    queries = ["solana", "pump solana", "bonk solana", "raydium solana", "new solana"]
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
        pc24 = safe_float(pair.get("priceChange", {}).get("h24"))
        pc1 = safe_float(pair.get("priceChange", {}).get("h1"))
        age_mins = token_age_minutes(pair)

        if liquidity < MIN_SEND_LIQ or volume < MIN_SEND_VOL:
            continue
        if pc24 > 120:
            continue
        if pc1 > 45:
            continue
        if age_mins < 3:
            continue

        result = score_send(pair, boost_lookup)
        if result["score"] >= 58:
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

    sol_addrs = sol_addrs[:30]
    raw_pairs = dex_tokens("solana", sol_addrs)
    pairs = pick_best_pairs(raw_pairs)

    scored = []
    for pair in pairs:
        liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
        age_mins = token_age_minutes(pair)
        pc24 = safe_float(pair.get("priceChange", {}).get("h24"))
        pc1 = safe_float(pair.get("priceChange", {}).get("h1"))

        if liquidity < MIN_EARLY_LIQ:
            continue
        if liquidity > 90000:
            continue
        if age_mins > 360:
            continue
        if pc24 > 90:
            continue
        if pc1 > 30:
            continue

        result = score_early(pair, boost_lookup)
        if result["score"] >= 55:
            scored.append((pair, result))

    scored.sort(key=lambda x: x[1]["score"], reverse=True)
    return scored[:RESULT_LIMIT]


def get_wallet_cluster_summary():
    if not SOLANATRACKER_API_KEY:
        return "Set SOLANATRACKER_API_KEY first to use wallet clustering."

    if not WATCHED_WALLETS:
        return "No watched wallets yet. Use /watchwallet &lt;address&gt;"

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


def ensure_chat(chat_id):
    key = str(chat_id)
    if key not in CHAT_SETTINGS:
        CHAT_SETTINGS[key] = {"alerts": False}
        save_json(CHAT_SETTINGS_FILE, CHAT_SETTINGS)
    return CHAT_SETTINGS[key]


def handle_command(chat_id, text):
    ensure_chat(chat_id)
    raw = text.strip()
    parts = raw.split()
    cmd = parts[0].lower()

    if cmd in ("/start", "/help"):
        send_message(
            chat_id,
            "🚀 <b>Balanced Sniper Bot is LIVE</b>\n\n"
            "<b>Commands</b>\n"
            "/send - best current setups\n"
            "/early - balanced early setups\n"
            "/smart - tracked-wallet cluster buys\n"
            "/watchwallet &lt;address&gt; - add wallet\n"
            "/unwatchwallet &lt;address&gt; - remove wallet\n"
            "/wallets - list watched wallets\n"
            "/alerts on - enable push alerts\n"
            "/alerts off - disable push alerts\n"
            "/alertson - enable push alerts\n"
            "/alertsoff - disable push alerts\n"
            "/status - bot status",
        )
        return

    if cmd == "/status":
        state = "ON" if CHAT_SETTINGS[str(chat_id)].get("alerts") else "OFF"
        send_message(
            chat_id,
            "📡 <b>Status</b>\n\n"
            f"Alerts: <b>{state}</b>\n"
            f"Alert interval: <b>{ALERT_INTERVAL_SECONDS}s</b>\n"
            f"Send threshold: <b>{ALERT_MIN_SCORE}</b>\n"
            f"Early threshold: <b>{ALERT_MIN_EARLY_SCORE}</b>\n"
            f"Watched wallets: <b>{len(WATCHED_WALLETS)}</b>"
        )
        return

    if cmd == "/send":
        send_message(chat_id, "🔥 Scanning balanced send setups...")
        try:
            scored = get_send_candidates()
            if not scored:
                send_message(chat_id, "No good send setups right now.")
                return
            msg = "🔥 <b>Top balanced send setups</b>\n\n" + "\n\n---\n\n".join(
                format_pair_message(pair, result, mode="send") for pair, result in scored
            )
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"Send scan error: {escape(e)}")
        return

    if cmd == "/early":
        send_message(chat_id, "🆕 Scanning balanced early runners...")
        try:
            scored = get_early_candidates()
            if not scored:
                send_message(chat_id, "No balanced early runners right now.")
                return
            msg = "🆕 <b>Top balanced early setups</b>\n\n" + "\n\n---\n\n".join(
                format_pair_message(pair, result, mode="early") for pair, result in scored
            )
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"Early scan error: {escape(e)}")
        return

    if cmd == "/watchwallet":
        if len(parts) < 2:
            send_message(chat_id, "Use: /watchwallet &lt;wallet_address&gt;")
            return
        wallet = parts[1].strip()
        if wallet not in WATCHED_WALLETS:
            WATCHED_WALLETS.append(wallet)
            save_json(WALLETS_FILE, WATCHED_WALLETS)
        send_message(chat_id, f"Added watched wallet:\n<code>{escape(wallet)}</code>")
        return

    if cmd == "/unwatchwallet":
        if len(parts) < 2:
            send_message(chat_id, "Use: /unwatchwallet &lt;wallet_address&gt;")
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

    if cmd == "/alertson":
        CHAT_SETTINGS[str(chat_id)]["alerts"] = True
        save_json(CHAT_SETTINGS_FILE, CHAT_SETTINGS)
        send_message(chat_id, "Alerts turned on.")
        return

    if cmd == "/alertsoff":
        CHAT_SETTINGS[str(chat_id)]["alerts"] = False
        save_json(CHAT_SETTINGS_FILE, CHAT_SETTINGS)
        send_message(chat_id, "Alerts turned off.")
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


def alert_loop():
    enabled_chats = [cid for cid, s in CHAT_SETTINGS.items() if s.get("alerts")]
    if not enabled_chats:
        return

    candidates = []

    try:
        for pair, result in get_send_candidates():
            if result["score"] >= ALERT_MIN_SCORE and result["phase"] != "LATE":
                candidates.append(("send", pair, result))
    except Exception as e:
        print(f"send alert scan error: {e}")

    try:
        for pair, result in get_early_candidates():
            if result["score"] >= ALERT_MIN_EARLY_SCORE and result["phase"] in ("EARLY", "MID"):
                candidates.append(("early", pair, result))
    except Exception as e:
        print(f"early alert scan error: {e}")

    for mode, pair, result in candidates:
        token_addr = pair.get("baseToken", {}).get("address")
        if not token_addr:
            continue

        key = f"{mode}:{token_addr}"
        last_sent = ALERT_STATE["seen_tokens"].get(key, 0)

        if now_ts() - last_sent < 1800:
            continue

        header = "🚨 <b>Auto send alert</b>\n\n" if mode == "send" else "🆕 <b>Auto early alert</b>\n\n"
        msg = header + format_pair_message(pair, result, mode="send" if mode == "send" else "early")

        for chat_id in enabled_chats:
            try:
                send_message(chat_id, msg)
            except Exception:
                pass

        ALERT_STATE["seen_tokens"][key] = now_ts()
        save_json(ALERT_STATE_FILE, ALERT_STATE)


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing")

    print("Balanced sniper bot starting...")
    offset = None
    last_alert_run = 0

    while True:
        try:
            if now_ts() - last_alert_run >= ALERT_INTERVAL_SECONDS:
                alert_loop()
                last_alert_run = now_ts()

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
