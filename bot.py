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

ALERT_MIN_SCORE = float(os.getenv("ALERT_MIN_SCORE", "62"))
ALERT_MIN_EARLY_SCORE = float(os.getenv("ALERT_MIN_EARLY_SCORE", "58"))
ALERT_INTERVAL_SECONDS = int(os.getenv("ALERT_INTERVAL_SECONDS", "25"))
REQUEST_TIMEOUT = 20

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)

WALLETS_FILE = DATA_DIR / "watched_wallets.json"
CHAT_SETTINGS_FILE = DATA_DIR / "chat_settings.json"
ALERT_STATE_FILE = DATA_DIR / "alert_state.json"
TOKEN_HISTORY_FILE = DATA_DIR / "token_history.json"

RESULT_LIMIT = 5

MIN_SEND_LIQ = 12000
MIN_SEND_VOL = 25000
MIN_EARLY_LIQ = 5000
MAX_EARLY_LIQ = 60000
MAX_SAFE_PRICE_CHANGE_24H = 85
MAX_SAFE_PRICE_CHANGE_1H = 35
IDEAL_MIN_AGE = 5
IDEAL_MAX_AGE = 180

RE_ALERT_SCORE_JUMP = float(os.getenv("RE_ALERT_SCORE_JUMP", "8"))
RE_ALERT_MIN_MINUTES = int(os.getenv("RE_ALERT_MIN_MINUTES", "8"))
TOKEN_HISTORY_TTL_SECONDS = int(os.getenv("TOKEN_HISTORY_TTL_SECONDS", "21600"))


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
TOKEN_HISTORY = load_json(TOKEN_HISTORY_FILE, {})


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


def st_top_traders():
    if not SOLANATRACKER_API_KEY:
        return []
    url = "https://data.solanatracker.io/top-traders/all"
    r = requests.get(
        url,
        headers={"x-api-key": SOLANATRACKER_API_KEY},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("wallets", [])


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


def get_conviction_label(score, phase, wallet_cluster_count, pc1, pc24):
    if pc24 > 100 or pc1 > 45:
        return "TOO EXTENDED"
    if wallet_cluster_count >= 2 and score >= 75:
        return "SMART MONEY CONFIRMED"
    if phase == "EARLY" and score >= 78:
        return "SNIPER ENTRY"
    if phase in ("EARLY", "MID") and score >= 68:
        return "WATCH CLOSELY"
    if score >= 58:
        return "CONFIRMATION NEEDED"
    return "AVOID"


def market_cap_bonus(market_cap):
    mc = safe_float(market_cap)
    if 20000 <= mc <= 300000:
        return 12
    if 300001 <= mc <= 800000:
        return 8
    if 800001 <= mc <= 2000000:
        return 4
    if 0 < mc < 10000:
        return -8
    if mc > 5000000:
        return -8
    if mc > 2000000:
        return -4
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
    market_cap = safe_float(pair.get("marketCap"))
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

    mc_bonus = market_cap_bonus(market_cap)

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

    score = momentum + quality + freshness + wallet_score + mc_bonus - risk_penalty
    score = round(clamp(score, 0, 100), 1)
    phase = get_phase(age_mins, pc1, pc24, liquidity)
    label = "WATCH"
    if score >= 84:
        label = "A-TIER RUNNER"
    elif score >= 74:
        label = "B-TIER SEND"
    elif score >= 68:
        label = "C-TIER SCALP"

    conviction = get_conviction_label(score, phase, wallet_cluster["count"], pc1, pc24)

    return {
        "score": score,
        "label": label,
        "phase": phase,
        "conviction": conviction,
        "wallet_cluster": wallet_cluster,
        "metrics": {
            "volume": volume,
            "liquidity": liquidity,
            "pc24": pc24,
            "pc6": pc6,
            "pc1": pc1,
            "age_mins": age_mins,
            "boosted": addr in boost_lookup,
            "market_cap": market_cap,
            "fdv": fdv,
            "mc_bonus": mc_bonus,
        },
    }


def score_early(pair, boost_lookup):
    token = pair.get("baseToken", {})
    addr = token.get("address", "")
    volume = safe_float(pair.get("volume", {}).get("h24"))
    liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
    pc24 = safe_float(pair.get("priceChange", {}).get("h24"))
    pc1 = safe_float(pair.get("priceChange", {}).get("h1"))
    market_cap = safe_float(pair.get("marketCap"))
    fdv = safe_float(pair.get("fdv"))
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

    mc_bonus = market_cap_bonus(market_cap)
    score += mc_bonus

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

    conviction = get_conviction_label(score, phase, wallet_cluster["count"], pc1, pc24)

    return {
        "score": score,
        "label": label,
        "phase": phase,
        "conviction": conviction,
        "wallet_cluster": wallet_cluster,
        "metrics": {
            "volume": volume,
            "liquidity": liquidity,
            "pc24": pc24,
            "pc1": pc1,
            "age_mins": age_mins,
            "boosted": addr in boost_lookup,
            "market_cap": market_cap,
            "fdv": fdv,
            "mc_bonus": mc_bonus,
        },
    }


def wallet_trust_score(summary, activity_trades=0):
    total = safe_float(summary.get("total"))
    invested = safe_float(summary.get("totalInvested"))
    wins = safe_float(summary.get("totalWins"))
    win_pct = safe_float(summary.get("winPercentage"))
    avg_buy = safe_float(summary.get("averageBuyAmount"))

    score = 0
    score += clamp(total / 10000, 0, 30)
    score += clamp(win_pct / 3, 0, 30)
    score += clamp(wins / 5, 0, 20)
    score += clamp(invested / 5000, 0, 10)
    score += clamp(activity_trades / 3, 0, 10)

    if avg_buy <= 0:
        score -= 5

    return round(clamp(score, 0, 100), 1)


def get_top_wallets(mode="daily", limit=10):
    if not SOLANATRACKER_API_KEY:
        raise ValueError("SOLANATRACKER_API_KEY is missing")

    wallets = st_top_traders()
    ranked = []

    for item in wallets[:50]:
        wallet = item.get("wallet")
        summary = item.get("summary", {}) or {}
        recent_trades = []

        try:
            recent_trades = st_wallet_trades(wallet)[:25]
        except Exception:
            recent_trades = []

        if mode == "daily":
            cutoff_ms = (now_ts() - 86400) * 1000
        else:
            cutoff_ms = (now_ts() - 7 * 86400) * 1000

        active_recent = [t for t in recent_trades if int(t.get("time", 0)) >= cutoff_ms]

        if mode == "daily" and len(active_recent) < 1:
            continue
        if mode == "weekly" and len(active_recent) < 3:
            continue

        trust = wallet_trust_score(summary, activity_trades=len(active_recent))

        ranked.append(
            {
                "wallet": wallet,
                "trust": trust,
                "summary": summary,
                "recent_trade_count": len(active_recent),
            }
        )

    ranked.sort(key=lambda x: x["trust"], reverse=True)
    return ranked[:limit]


def format_top_wallets_message(mode="daily", limit=10):
    rows = get_top_wallets(mode=mode, limit=limit)

    if not rows:
        return f"No {mode} top wallets found right now."

    title = "🧠 <b>Top wallets today</b>" if mode == "daily" else "📅 <b>Top wallets this week</b>"
    lines = [title, ""]

    for i, row in enumerate(rows, start=1):
        s = row["summary"]
        lines.append(f"{i}. <code>{escape(row['wallet'])}</code>")
        lines.append(f"⭐ <b>Trust:</b> {row['trust']}/100")
        lines.append(f"💵 <b>Total PnL:</b> {format_num(safe_float(s.get('total')))}")
        lines.append(f"🎯 <b>Win %:</b> {safe_float(s.get('winPercentage')):.2f}%")
        lines.append(f"✅ <b>Wins:</b> {int(safe_float(s.get('totalWins')))}")
        lines.append(f"📦 <b>Invested:</b> {format_num(safe_float(s.get('totalInvested')))}")
        lines.append(f"🔄 <b>Recent trades:</b> {row['recent_trade_count']}")
        action = "ADD" if row["trust"] >= 70 else "WATCH" if row["trust"] >= 55 else "IGNORE"
        lines.append(f"🛠️ <b>Action:</b> {action}")
        lines.append("")

    return "\n".join(lines).strip()


def add_top_wallets(count=5, mode="daily"):
    rows = get_top_wallets(mode=mode, limit=max(count, 10))
    added = []

    for row in rows:
        wallet = row["wallet"]
        if wallet not in WATCHED_WALLETS and row["trust"] >= 60:
            WATCHED_WALLETS.append(wallet)
            added.append(wallet)
        if len(added) >= count:
            break

    save_json(WALLETS_FILE, WATCHED_WALLETS)
    return added


def format_pair_message(pair, scored, mode="send", header_prefix=None):
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

    parts = []
    if header_prefix:
        parts.append(header_prefix)
        parts.append("")

    parts += [
        f"🚨 <b>{escape(scored['label'])}</b>",
        f"🎯 <b>{escape(scored['conviction'])}</b>",
        "",
        title,
        f"🔥 <b>Score:</b> {scored['score']}/100",
        f"🧠 <b>Phase:</b> {escape(scored['phase'])}",
        f"💰 <b>Price:</b> ${escape(format_price(price))}",
        f"🏦 <b>Market Cap:</b> {escape(format_num(m['market_cap']))}" if m["market_cap"] > 0 else "🏦 <b>Market Cap:</b> N/A",
        f"🧮 <b>FDV:</b> {escape(format_num(m['fdv']))}" if m["fdv"] > 0 else "🧮 <b>FDV:</b> N/A",
        f"🎁 <b>MC Bonus:</b> {m['mc_bonus']:+.0f}",
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


def update_token_history(mode, pair, result):
    token_addr = pair.get("baseToken", {}).get("address")
    if not token_addr:
        return

    key = f"{mode}:{token_addr}"
    existing = TOKEN_HISTORY.get(key, {})

    TOKEN_HISTORY[key] = {
        "name": pair.get("baseToken", {}).get("name", "Unknown"),
        "symbol": pair.get("baseToken", {}).get("symbol", ""),
        "score": result["score"],
        "market_cap": result["metrics"].get("market_cap", 0),
        "volume": result["metrics"].get("volume", 0),
        "liquidity": result["metrics"].get("liquidity", 0),
        "wallet_cluster": result["wallet_cluster"]["count"],
        "updated_at": now_ts(),
        "first_seen": existing.get("first_seen", now_ts()),
    }


def cleanup_token_history():
    cutoff = now_ts() - TOKEN_HISTORY_TTL_SECONDS
    dead_keys = [k for k, v in TOKEN_HISTORY.items() if v.get("updated_at", 0) < cutoff]
    for k in dead_keys:
        del TOKEN_HISTORY[k]


def should_realert(mode, pair, result):
    token_addr = pair.get("baseToken", {}).get("address")
    if not token_addr:
        return False, None

    key = f"{mode}:{token_addr}"
    old = TOKEN_HISTORY.get(key)

    if not old:
        return False, None

    minutes_since = (now_ts() - old.get("updated_at", now_ts())) / 60
    score_jump = result["score"] - safe_float(old.get("score", 0))
    mc_jump = safe_float(result["metrics"].get("market_cap", 0)) - safe_float(old.get("market_cap", 0))
    wallet_jump = result["wallet_cluster"]["count"] - int(old.get("wallet_cluster", 0))

    if minutes_since < RE_ALERT_MIN_MINUTES:
        return False, None

    if score_jump >= RE_ALERT_SCORE_JUMP:
        reason = f"Score jumped +{score_jump:.1f}"
        if wallet_jump > 0:
            reason += f" | Wallet cluster +{wallet_jump}"
        if mc_jump > 0:
            reason += f" | MC +{format_num(mc_jump)}"
        return True, reason

    if wallet_jump >= 1:
        return True, f"Wallet cluster increased to {result['wallet_cluster']['count']}"

    return False, None


def get_alpha_summary():
    send_candidates = []
    early_candidates = []

    try:
        send_candidates = get_send_candidates()
    except Exception:
        pass

    try:
        early_candidates = get_early_candidates()
    except Exception:
        pass

    lines = ["🧠 <b>Alpha Summary</b>", ""]

    if send_candidates:
        pair, result = send_candidates[0]
        name = pair.get("baseToken", {}).get("name", "Unknown")
        symbol = pair.get("baseToken", {}).get("symbol", "")
        label = name if not symbol else f"{name} ({symbol})"
        lines.append(f"🔥 <b>Best send:</b> {escape(label)} — {result['score']}/100")
    else:
        lines.append("🔥 <b>Best send:</b> None right now")

    if early_candidates:
        pair, result = early_candidates[0]
        name = pair.get("baseToken", {}).get("name", "Unknown")
        symbol = pair.get("baseToken", {}).get("symbol", "")
        label = name if not symbol else f"{name} ({symbol})"
        lines.append(f"🆕 <b>Best early:</b> {escape(label)} — {result['score']}/100")
    else:
        lines.append("🆕 <b>Best early:</b> None right now")

    improving = []
    for k, v in TOKEN_HISTORY.items():
        if now_ts() - v.get("updated_at", 0) <= 3600:
            improving.append((safe_float(v.get("score", 0)), v))

    improving.sort(reverse=True, key=lambda x: x[0])

    if improving:
        v = improving[0][1]
        label = v.get("name", "Unknown")
        if v.get("symbol"):
            label += f" ({v['symbol']})"
        lines.append(f"📈 <b>Strongest tracked:</b> {escape(label)} — {safe_float(v.get('score', 0))}/100")

    return "\n".join(lines)


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
            "🚀 <b>Balanced Sniper V2 Brain is LIVE</b>\n\n"
            "<b>Commands</b>\n"
            "/send - best current setups\n"
            "/early - balanced early setups\n"
            "/alpha - quick command center summary\n"
            "/smart - tracked-wallet cluster buys\n"
            "/topwallets daily - best wallets today\n"
            "/topwallets weekly - best wallets this week\n"
            "/addtopwallets 5 daily - add best wallets to watchlist\n"
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

    if cmd == "/topwallets":
        if len(parts) < 2:
            send_message(chat_id, "Use: /topwallets daily or /topwallets weekly")
            return
        mode = parts[1].lower()
        if mode not in ("daily", "weekly"):
            send_message(chat_id, "Use: /topwallets daily or /topwallets weekly")
            return
        try:
            send_message(chat_id, format_top_wallets_message(mode=mode, limit=10))
        except Exception as e:
            send_message(chat_id, f"Top wallets error: {escape(e)}")
        return

    if cmd == "/addtopwallets":
        if len(parts) < 3:
            send_message(chat_id, "Use: /addtopwallets 5 daily")
            return
        try:
            count = int(parts[1])
        except ValueError:
            send_message(chat_id, "Count must be a number. Example: /addtopwallets 5 daily")
            return

        mode = parts[2].lower()
        if mode not in ("daily", "weekly"):
            send_message(chat_id, "Use mode daily or weekly. Example: /addtopwallets 5 daily")
            return

        try:
            added = add_top_wallets(count=count, mode=mode)
            if not added:
                send_message(chat_id, "No top wallets were added.")
                return
            msg = "✅ <b>Added top wallets</b>\n\n" + "\n".join(f"• <code>{escape(w)}</code>" for w in added)
            send_message(chat_id, msg)
        except Exception as e:
            send_message(chat_id, f"Add top wallets error: {escape(e)}")
        return

    if cmd == "/alpha":
        send_message(chat_id, get_alpha_summary())
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
            f"Watched wallets: <b>{len(WATCHED_WALLETS)}</b>\n"
            f"Re-alert jump: <b>{RE_ALERT_SCORE_JUMP}</b>"
        )
        return

    if cmd == "/send":
        send_message(chat_id, "🔥 Scanning balanced send setups...")
        try:
            scored = get_send_candidates()
            if not scored:
                send_message(chat_id, "No good send setups right now.")
                return
            for pair, result in scored:
                update_token_history("send", pair, result)
            save_json(TOKEN_HISTORY_FILE, TOKEN_HISTORY)
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
            for pair, result in scored:
                update_token_history("early", pair, result)
            save_json(TOKEN_HISTORY_FILE, TOKEN_HISTORY)
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

    cleanup_token_history()
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

        send_type = None
        reason = None

        if now_ts() - last_sent >= 1800:
            send_type = "fresh"

        relert, reason = should_realert(mode, pair, result)
        if relert:
            send_type = "realert"

        if not send_type:
            update_token_history(mode, pair, result)
            continue

        if send_type == "fresh":
            header = "🚨 <b>Auto send alert</b>\n\n" if mode == "send" else "🆕 <b>Auto early alert</b>\n\n"
            msg = format_pair_message(pair, result, mode="send" if mode == "send" else "early", header_prefix=header.strip())
        else:
            header = "📈 <b>Re-alert: setup improving</b>"
            msg = format_pair_message(
                pair,
                result,
                mode="send" if mode == "send" else "early",
                header_prefix=f"{header}\n<b>Reason:</b> {escape(reason)}"
            )

        for chat_id in enabled_chats:
            try:
                send_message(chat_id, msg)
            except Exception:
                pass

        ALERT_STATE["seen_tokens"][key] = now_ts()
        update_token_history(mode, pair, result)

    save_json(ALERT_STATE_FILE, ALERT_STATE)
    save_json(TOKEN_HISTORY_FILE, TOKEN_HISTORY)


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing")

    print("Balanced sniper V2 brain starting...")
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
