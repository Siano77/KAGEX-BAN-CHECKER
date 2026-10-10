#!/usr/bin/env python3
"""
KAGE X Ban Checker  v6.0
WhatsApp ban checker — cloud-server-safe, multi-signal detection.
Prefix: !  |  Simultaneous  |  Personal / Regional / Admin
"""

import os, asyncio, aiohttp, re, time, random, string, json
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from telegram.constants import ParseMode

# ═══════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_NAME  = "KAGE X Ban Checker"
BOT_VER   = "v6.0"
PREFIX    = "!"
MAX_NUM   = 20

# ═══════════════════════════════════════════════════
#  NUMBER UTILITIES
# ═══════════════════════════════════════════════════

CC_TABLE = sorted({
    "1","7","20","27","30","31","32","33","34","36","39","40","41",
    "43","44","45","46","47","48","49","51","52","53","54","55","56",
    "57","58","60","61","62","63","64","65","66","81","82","84","86",
    "90","91","92","93","94","95","98",
    "212","213","216","218","220","221","222","223","224","225","226",
    "227","228","229","230","231","232","233","234","235","236","237",
    "238","239","240","241","242","243","244","245","246","247","248",
    "249","250","251","252","253","254","255","256","257","258","260",
    "261","262","263","264","265","266","267","268","269","290","291",
    "297","298","299","350","351","352","353","354","355","356","357",
    "358","359","370","371","372","373","374","375","376","377","380",
    "381","382","385","386","387","389","420","421","423",
    "500","501","502","503","504","505","506","507","508","509",
    "590","591","592","593","594","595","596","597","598","599",
    "670","672","673","674","675","676","677","678","679","680",
    "681","682","683","685","686","687","688","689","690","691","692",
    "850","852","853","855","856","880","886",
    "960","961","962","963","964","965","966","967","968","970",
    "971","972","973","974","975","976","977",
    "992","993","994","995","996","998",
}, key=len, reverse=True)


def split_cc(digits: str):
    for cc in CC_TABLE:
        if digits.startswith(cc) and len(digits) - len(cc) >= 4:
            return cc, digits[len(cc):]
    for n in (1, 2, 3):
        if len(digits) > n + 3:
            return digits[:n], digits[n:]
    return digits[:1], digits[1:]


def only_digits(s: str) -> str:
    return re.sub(r"\D", "", s.strip())


def parse_numbers(text: str) -> list:
    seen, out = set(), []
    for tok in re.split(r"[\s,;|\n]+", text.strip()):
        d = only_digits(tok)
        if 7 <= len(d) <= 15 and d not in seen:
            seen.add(d); out.append(d)
    return out[:MAX_NUM]


# ═══════════════════════════════════════════════════
#  DETECTION ENGINE
# ═══════════════════════════════════════════════════

# Rotate between multiple realistic mobile + desktop UAs
USER_AGENTS = [
    # Android Chrome
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    # iPhone Safari
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    # Desktop Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Samsung Browser
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/24.0 Chrome/117.0.0.0 Mobile Safari/537.36",
]

WA_CLIENT_UAS = [
    "WhatsApp/2.24.10.84 A",
    "WhatsApp/2.24.8.78 A",
    "WhatsApp/2.24.6.77 A",
    "WhatsApp/2.23.25.84 A",
]


def _ua(mobile=True):
    pool = USER_AGENTS[:2] if mobile else USER_AGENTS
    return random.choice(pool)


def _rid(n=16):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


async def _get(session, url, *, params=None, headers=None, timeout=10):
    """Fetch URL. Returns (status_code, body_lower, final_url) or (-1,'','') on error."""
    try:
        async with session.get(
            url, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
            ssl=False, allow_redirects=True,
        ) as r:
            body = (await r.text(errors="ignore")).lower()
            return r.status, body, str(r.url)
    except asyncio.TimeoutError:
        return -2, "", ""
    except Exception:
        return -1, "", ""


# ---- Signal helpers ----------------------------------------------------------

_ACTIVE_KW   = [
    "send message", "open whatsapp", "continue to chat",
    "chat with", "message this contact", "whatsapp://send",
    "click to chat", "start chat", "wa.me/",
    "\"phone\":", "phoneNumber", "send_message",
    "og:title\">whatsapp", # og meta
]
_BANNED_KW   = [
    "this account has been banned",
    "account has been banned",
    "number is banned",
    "your account has been banned",
    "this phone number is banned",
    "account banned from whatsapp",
    "banned for violating",
]
_NOREG_KW    = [
    "phone number shared via url is invalid",
    "this link is not valid",
    "not on whatsapp",
    "number is not registered",
    "invalid phone number",
    "no whatsapp account",
    "doesn't have whatsapp",
    "does not have whatsapp",
]
_REGIONAL_KW = [
    "not available in your country",
    "not available in your region",
    "country is not supported",
    "service not available in",
    "unavailable in your region",
]
_ADMIN_KW    = [
    "terms of service", "policy violation",
    "violated our terms", "enforcement action",
    "reported for spam", "abuse",
]


def _scan(body):
    """Scan page body. Returns 'active'|'banned'|'not_registered'|None."""
    for kw in _ACTIVE_KW:
        if kw in body:
            return "active"
    for kw in _BANNED_KW:
        if kw in body:
            return "banned"
    for kw in _NOREG_KW:
        if kw in body:
            return "not_registered"
    return None


def _ban_type_from_body(body, http_code):
    if http_code == 403:
        return "Admin"
    for kw in _REGIONAL_KW:
        if kw in body:
            return "Regional"
    for kw in _ADMIN_KW:
        if kw in body:
            return "Admin"
    return "Personal"


# ---- Method 1: WhatsApp internal exist API ----------------------------------

async def _m1_exist_api(session, cc, nn):
    """
    WhatsApp v2/exist endpoint — official internal check.
    Returns 'active'|'banned'|'not_registered'|'rate_limited'|None
    """
    params  = {"cc": cc, "phone": nn, "id": _rid(), "lg": "en", "lc": "US"}
    headers = {"User-Agent": random.choice(WA_CLIENT_UAS), "Accept": "*/*"}
    code, body, _ = await _get(
        session, "https://v.whatsapp.net/v2/exist",
        params=params, headers=headers,
    )
    if code < 0:
        return None  # unreachable from this IP

    try:
        data = json.loads(body)
    except Exception:
        data = {}

    s  = str(data.get("status",  "")).lower()
    rs = str(data.get("result",  "")).lower()
    rn = str(data.get("reason",  "")).lower()

    if s == "ok" or rs in ("exist", "ok"):
        return "active"
    if rn in ("blocked", "banned", "gone", "suspended", "disabled"):
        return "banned"
    if rn in ("unregistered", "not_exist", "nonexistent", "missing"):
        return "not_registered"
    if rn in ("too_recent", "try_later") or code == 429:
        return "rate_limited"
    if code in (401, 403):
        return "banned"
    if code == 404:
        return "not_registered"
    # Last-resort body scan
    return _scan(body)


# ---- Method 2: wa.me with mobile UA -----------------------------------------

async def _m2_wame(session, digits):
    """
    wa.me/{digits} page check with mobile user agent.
    Returns 'active'|'banned'|'not_registered'|None
    """
    headers = {
        "User-Agent": _ua(mobile=True),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
    }
    code, body, final_url = await _get(
        session, f"https://wa.me/{digits}",
        headers=headers, timeout=10,
    )
    if code < 0:
        return None

    # Redirect to intent or whatsapp:// = active on mobile
    if "whatsapp://" in final_url or "intent://" in final_url:
        return "active"

    result = _scan(body)
    if result:
        return result

    # Check og:title specifically
    og = re.search(r'og:title["\s]+content=["\']([^"\']+)', body)
    if og:
        t = og.group(1).lower()
        if "whatsapp" in t and any(c.isdigit() for c in t):
            return "active"

    return None


# ---- Method 3: api.whatsapp.com/send ----------------------------------------

async def _m3_api_send(session, digits):
    """
    api.whatsapp.com/send?phone= page check.
    Returns 'active'|'banned'|'not_registered'|None
    """
    headers = {
        "User-Agent": _ua(mobile=False),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
    }
    code, body, _ = await _get(
        session, "https://api.whatsapp.com/send",
        params={"phone": digits}, headers=headers, timeout=10,
    )
    if code < 0:
        return None
    if code == 403:
        return "banned"  # API-level block
    return _scan(body)


# ---- Method 4: click.whatsapp.com shortlink ---------------------------------

async def _m4_click(session, digits):
    """
    click.whatsapp.com/send?phone= — alternative endpoint less likely to be blocked.
    Returns 'active'|'not_registered'|None
    """
    headers = {
        "User-Agent": _ua(mobile=True),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    code, body, final_url = await _get(
        session, "https://click.whatsapp.com/send",
        params={"phone": digits}, headers=headers, timeout=10,
    )
    if code < 0:
        return None
    if "whatsapp://" in final_url:
        return "active"
    return _scan(body)


# ---- Method 5: wa.me short redirect analysis --------------------------------

async def _m5_redirect(session, digits):
    """
    Check redirect chain of wa.me to determine registration.
    If WA redirects to a proper send page → active.
    If WA shows error page → not_registered.
    """
    headers = {
        "User-Agent": _ua(mobile=True),
        "Accept": "text/html,*/*",
    }
    try:
        # Don't follow redirects so we see the redirect target
        async with session.get(
            f"https://wa.me/{digits}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
            ssl=False,
            allow_redirects=False,
        ) as r:
            loc = r.headers.get("Location", "").lower()
            if "send" in loc or "whatsapp" in loc or "chat" in loc:
                return "active"
            if "invalid" in loc or "error" in loc:
                return "not_registered"
    except Exception:
        pass
    return None


# ---- Ban type classifier ----------------------------------------------------

async def _classify_ban(session, digits):
    """Determine ban type: Admin, Regional, or Personal."""
    headers = {"User-Agent": _ua(), "Accept-Language": "en-US,en;q=0.9"}
    code, body, _ = await _get(
        session, "https://api.whatsapp.com/send",
        params={"phone": digits}, headers=headers, timeout=8,
    )
    return _ban_type_from_body(body, code)


# ---- Master checker ---------------------------------------------------------

async def check_one(session, digits: str) -> dict:
    cc, nn  = split_cc(digits)
    display = "+" + digits
    t0      = time.monotonic()

    # Run all 5 methods simultaneously
    r1, r2, r3, r4, r5 = await asyncio.gather(
        _m1_exist_api(session, cc, nn),
        _m2_wame(session, digits),
        _m3_api_send(session, digits),
        _m4_click(session, digits),
        _m5_redirect(session, digits),
        return_exceptions=True,
    )

    # Normalize exception results
    signals = []
    for sig in (r1, r2, r3, r4, r5):
        if isinstance(sig, Exception) or sig is None:
            continue
        signals.append(sig)

    ping_ms = round((time.monotonic() - t0) * 1000)

    # ── Decision logic ──────────────────────────────────────────────
    counts = {
        "active":         signals.count("active"),
        "banned":         signals.count("banned"),
        "not_registered": signals.count("not_registered"),
        "rate_limited":   signals.count("rate_limited"),
    }

    total_signals = len(signals)

    # Any ban signal from official API (method 1) is authoritative
    if r1 == "banned":
        ban_type = await _classify_ban(session, digits)
        return _res(display, "banned", ban_type, ping_ms, "API confirmed")

    if r1 == "not_registered":
        return _res(display, "not_registered", None, ping_ms, "")

    # Active: if ANY method confirms active, trust it
    if counts["active"] >= 1:
        return _res(display, "active", None, ping_ms, "")

    # Banned: if 2+ methods agree, confident
    if counts["banned"] >= 2:
        ban_type = await _classify_ban(session, digits)
        return _res(display, "banned", ban_type, ping_ms, f"{counts['banned']}/5 signals")

    # Banned: even 1 signal from web methods
    if counts["banned"] == 1:
        ban_type = await _classify_ban(session, digits)
        return _res(display, "banned", ban_type, ping_ms, "1 signal detected")

    # Not registered: majority agree
    if counts["not_registered"] >= 2:
        return _res(display, "not_registered", None, ping_ms, "")

    if counts["not_registered"] == 1:
        return _res(display, "not_registered", None, ping_ms, "")

    # Rate limited
    if counts["rate_limited"] >= 1:
        return _res(display, "rate_limited", None, ping_ms,
                    "WhatsApp throttled — try again in 1 min")

    # All methods unreachable (server IP fully blocked by WA)
    if total_signals == 0:
        return _res(display, "blocked_ip", None, ping_ms,
                    "WA blocked server IP — check from personal device")

    # Partial signals but inconclusive
    return _res(display, "unknown", None, ping_ms,
                f"Weak signal ({total_signals} methods responded)")


def _res(display, status, ban_type, ping_ms, note):
    return {"display": display, "status": status,
            "ban_type": ban_type, "ping_ms": ping_ms, "note": note}


async def check_all(numbers: list) -> list:
    conn = aiohttp.TCPConnector(limit=60, ssl=False, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=conn) as sess:
        results = await asyncio.gather(
            *[check_one(sess, n) for n in numbers],
            return_exceptions=True,
        )
    out = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            out.append(_res("+" + numbers[i], "error", None, 0, str(r)[:80]))
        else:
            out.append(r)
    return out


# ═══════════════════════════════════════════════════
#  FORMATTERS
# ═══════════════════════════════════════════════════

STATUS_LBL = {
    "active":         ("🟢", "ACTIVE — Not Banned"),
    "banned":         ("🔴", "BANNED"),
    "not_registered": ("⚫", "NOT ON WHATSAPP"),
    "rate_limited":   ("⏳", "RATE LIMITED — Retry soon"),
    "blocked_ip":     ("🟠", "SERVER IP BLOCKED BY WA"),
    "timeout":        ("🟡", "TIMEOUT"),
    "error":          ("❌", "ERROR"),
    "unknown":        ("❓", "WEAK SIGNAL"),
}
BAN_LBL = {
    "Admin":    "👮 Admin Ban",
    "Regional": "🌍 Regional Ban",
    "Personal": "🔒 Personal Ban",
}


def build_msg(results, elapsed, requester):
    now    = datetime.now().strftime("%d %b %Y  %H:%M:%S")
    total  = len(results)
    banned = sum(1 for r in results if r["status"] == "banned")
    active = sum(1 for r in results if r["status"] == "active")
    other  = total - banned - active
    div    = "─" * 36

    lines = [
        "╔" + "═" * 38 + "╗",
        "║   ⚡  <b>KAGE X Ban Checker</b>  ⚡  v6.0  ║",
        f"║   🗓 {now:<31}║",
        "╚" + "═" * 38 + "╝",
        "",
        "📊 <b>Summary</b>",
        f"   • Total    : <code>{total}</code>",
        f"   • 🔴 Banned  : <code>{banned}</code>",
        f"   • 🟢 Active  : <code>{active}</code>",
        f"   • Other    : <code>{other}</code>",
        f"   • ⏱ Speed   : <code>{elapsed:.2f}s</code>",
        "", div, "",
    ]

    for i, r in enumerate(results, 1):
        icon, lbl = STATUS_LBL.get(r["status"], ("❓", r["status"].upper()))
        bt = BAN_LBL.get(r.get("ban_type"), "") if r.get("ban_type") else ""
        lines.append(f"<b>#{i}</b>  {icon}  <code>{r['display']}</code>")
        lines.append(f"      Status   : <b>{lbl}</b>")
        if bt:
            lines.append(f"      Ban Type : <b>{bt}</b>")
        lines.append(f"      Ping     : <code>{r['ping_ms']} ms</code>")
        if r.get("note"):
            lines.append(f"      Note     : <i>{r['note']}</i>")
        lines.append("")

    lines += [div, f"👤 By: <b>{requester}</b>  |  🤖 <b>{BOT_NAME}</b>"]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ═══════════════════════════════════════════════════

async def cmd_start(u: Update, _):
    name = u.effective_user.first_name
    msg  = (
        "╔" + "═" * 38 + "╗\n"
        "║   ⚡  <b>KAGE X Ban Checker</b>  ⚡  v6.0  ║\n"
        "╚" + "═" * 38 + "╝\n\n"
        f"👋 Welcome, <b>{name}</b>!\n\n"
        "5-method simultaneous WhatsApp detection.\n"
        "Every number gets its own accurate result.\n\n"
        "<b>Results:</b>\n"
        "  🟢 Active  •  🔴 Banned  •  ⚫ Not Registered\n"
        "  Ban types: 🔒 Personal  🌍 Regional  👮 Admin\n\n"
        "━" * 26 + "\n"
        "  <code>!check +2348012345678</code>\n"
        "  <code>!check 234xxx 447xxx 155xxx</code>\n"
        "  <code>!ping</code>  │  <code>!help</code>\n"
        "━" * 26 + "\n"
        f"⚡ Up to <b>{MAX_NUM}</b> numbers simultaneously!"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Help",  callback_data="help"),
        InlineKeyboardButton("ℹ️ About", callback_data="about"),
    ]])
    await u.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_help(u: Update, _):
    msg = (
        "⚡ <b>KAGE X — Commands</b>\n\n"
        "<code>!check +234xxxxxxxx</code> — single\n"
        "<code>!check 234xxx 447xxx</code> — multi\n"
        "<code>!ping</code> — latency\n"
        "<code>!about</code> — bot info\n\n"
        f"⚡ Max <b>{MAX_NUM}</b> numbers per request."
    )
    target = u.message or u.callback_query.message
    await target.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_about(u: Update, _):
    msg = (
        "╔" + "═" * 34 + "╗\n"
        f"║  ⚡ <b>{BOT_NAME}</b>  {BOT_VER}     ║\n"
        "╚" + "═" * 34 + "╝\n\n"
        "🔍 5-method WhatsApp detection\n"
        "⚡ All methods run simultaneously\n"
        "🎯 Detects Personal, Regional & Admin bans\n"
        "🌐 Global prefix: <code>!</code>"
    )
    target = u.message or u.callback_query.message
    await target.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_ping(u: Update, _):
    t0  = time.monotonic()
    msg = await u.message.reply_text("🏓 Pinging...")
    ms  = round((time.monotonic() - t0) * 1000)
    await msg.edit_text(f"🏓 <b>Pong!</b>\n⚡ Latency: <code>{ms} ms</code>",
                        parse_mode=ParseMode.HTML)


async def cmd_check(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args   = " ".join(ctx.args) if ctx.args else ""
    nums   = parse_numbers(args)
    user   = u.effective_user

    if not nums:
        await u.message.reply_text(
            "⚠️ <b>No numbers found.</b>\n\n"
            "Include country code:\n"
            "  <code>!check +2348012345678</code>\n"
            "  <code>!check 2348012345678 447911234567</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    wait = await u.message.reply_text(
        f"⏳ Checking <code>{len(nums)}</code> number(s) using 5-method engine... ✨",
        parse_mode=ParseMode.HTML,
    )
    t0      = time.monotonic()
    results = await check_all(nums)
    elapsed = time.monotonic() - t0
    req     = f"@{user.username}" if user.username else user.first_name
    await wait.delete()
    await u.message.reply_text(build_msg(results, elapsed, req),
                               parse_mode=ParseMode.HTML)


async def on_prefix(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (u.message.text or "").strip()
    if not text.startswith(PREFIX):
        return
    parts    = text[len(PREFIX):].split(None, 1)
    cmd      = parts[0].lower() if parts else ""
    ctx.args = parts[1].split() if len(parts) > 1 else []
    fn = {"check": cmd_check, "start": cmd_start,
          "help":  cmd_help,  "about": cmd_about,
          "ping":  cmd_ping}.get(cmd)
    if fn:
        await fn(u, ctx)
    else:
        await u.message.reply_text(
            f"❓ Unknown: <code>!{cmd}</code> — try <code>!help</code>",
            parse_mode=ParseMode.HTML,
        )


async def on_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    if q.data == "help":  await cmd_help(u, ctx)
    elif q.data == "about": await cmd_about(u, ctx)


async def on_err(update, ctx): print(f"[ERR] {ctx.error}")


# ═══════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════

def main():
    token = BOT_TOKEN.strip()
    if not token:
        print("⚠️  BOT_TOKEN not set."); return
    app = Application.builder().token(token).build()
    for c, f in [("start", cmd_start),("help", cmd_help),
                 ("about", cmd_about),("ping", cmd_ping),("check", cmd_check)]:
        app.add_handler(CommandHandler(c, f))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_prefix))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_error_handler(on_err)
    print(f"\n╔{'='*46}╗")
    print(f"║  ⚡  {BOT_NAME}  {BOT_VER}  ║")
    print(f"║  5-Method Engine — Running...                 ║")
    print(f"╚{'='*46}╝\n")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
