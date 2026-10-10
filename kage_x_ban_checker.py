#!/usr/bin/env python3
"""
KAGE X Ban Checker  —  Telegram Bot v5.0
WhatsApp number status checker.
Prefix: !   |   Simultaneous   |   Personal / Regional / Admin
"""

import os, asyncio, aiohttp, re, time, random, string, json
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
BOT_NAME    = "KAGE X Ban Checker"
BOT_VERSION = "v5.0"
PREFIX      = "!"
MAX_NUM     = 20

# ─────────────────────────────────────────────────────────────
#  NUMBER PARSING  — accepts ANY real-looking number, no rejections
# ─────────────────────────────────────────────────────────────

# Ordered longest-first so we match the most specific CC first
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


def get_cc_nn(digits: str):
    """
    Split digits into (country_code, national_number).
    Never fails for 7-15 digit strings.
    """
    for cc in CC_TABLE:
        if digits.startswith(cc) and len(digits) - len(cc) >= 4:
            return cc, digits[len(cc):]
    # Fallback: try 1/2/3-digit split
    for clen in (1, 2, 3):
        if len(digits) > clen + 3:
            return digits[:clen], digits[clen:]
    return digits[:1], digits[1:]


def clean(raw: str) -> str:
    """Strip everything except digits."""
    return re.sub(r"\D", "", raw.strip())


def extract_numbers(text: str) -> list:
    """Extract unique 7-15 digit numbers from free text."""
    tokens = re.split(r"[\s,;|\n]+", text.strip())
    seen, out = set(), []
    for t in tokens:
        d = clean(t)
        if 7 <= len(d) <= 15 and d not in seen:
            seen.add(d)
            out.append(d)
    return out[:MAX_NUM]


# ─────────────────────────────────────────────────────────────
#  WHATSAPP CHECK ENGINE  (two independent methods)
# ─────────────────────────────────────────────────────────────

WA_EXIST    = "https://v.whatsapp.net/v2/exist"
WA_WEB      = "https://api.whatsapp.com/send"
WA_ME       = "https://wa.me/"

WA_UAS = [
    "WhatsApp/2.24.8.78 A",
    "WhatsApp/2.24.6.77 A",
    "WhatsApp/2.23.25.84 A",
    "WhatsApp/2.24.10.84 A",
]
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _rand(n=16):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


# --- Method A: WhatsApp internal exist API ---

async def _method_a(session, cc, nn):
    """
    Returns dict with keys: status, reason, http
    status: "active" | "banned" | "not_registered" | "rate_limited" | "error" | None
    """
    params = {"cc": cc, "phone": nn, "id": _rand(), "lg": "en", "lc": "US"}
    headers = {"User-Agent": random.choice(WA_UAS), "Accept": "*/*"}
    try:
        async with session.get(
            WA_EXIST, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
            ssl=False, allow_redirects=True,
        ) as r:
            http = r.status
            try:
                body = await r.json(content_type=None)
            except Exception:
                raw = await r.text(errors="ignore")
                try:
                    body = json.loads(raw)
                except Exception:
                    body = {}

            s  = str(body.get("status",  "")).lower()
            rs = str(body.get("result",  "")).lower()
            rn = str(body.get("reason",  "")).lower()

            # active signals
            if s == "ok" or rs in ("exist", "ok", "1", "true"):
                return {"status": "active", "reason": rs, "http": http}

            # explicit ban
            if rn in ("blocked", "banned", "gone", "suspended", "disabled"):
                return {"status": "banned", "reason": rn, "http": http}

            # not registered
            if rn in ("unregistered", "not_exist", "nonexistent", "missing"):
                return {"status": "not_registered", "reason": rn, "http": http}

            # bad params
            if rn in ("bad_param", "country-code-invalid", "enum_mismatch"):
                return {"status": "param_error", "reason": rn, "http": http}

            # rate limited
            if rn in ("too_recent", "try_later") or http == 429:
                return {"status": "rate_limited", "reason": rn, "http": http}

            # HTTP-code fallback
            if http in (401, 403):
                return {"status": "banned", "reason": "http_403", "http": http}
            if http == 404:
                return {"status": "not_registered", "reason": "http_404", "http": http}
            if http == 200:
                # 200 but no clear JSON signal — check raw text
                raw = json.dumps(body).lower()
                if any(w in raw for w in ("exist", "login", "\"ok\"")):
                    return {"status": "active", "reason": "200_text", "http": http}

            return {"status": None, "reason": rn or str(http), "http": http}

    except asyncio.TimeoutError:
        return {"status": "timeout", "reason": "timeout", "http": 0}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:80], "http": 0}


# --- Method B: wa.me & api.whatsapp.com page check ---

async def _method_b(session, digits):
    """
    Browser-based check.
    Returns: "active" | "not_registered" | "banned" | None
    """
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    status = None

    # wa.me check
    try:
        async with session.get(
            WA_ME + digits, headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
            ssl=False, allow_redirects=True,
        ) as r:
            body = (await r.text(errors="ignore")).lower()
            if any(w in body for w in (
                "send message", "open whatsapp", "chat with",
                "message this number", "whatsapp://", "wa.me/",
            )):
                status = "active"
            elif any(w in body for w in (
                "phone number shared via url is invalid",
                "not on whatsapp",
                "number is not registered",
                "this link is not valid",
                "invalid link",
            )):
                status = "not_registered"
            elif any(w in body for w in (
                "this account has been banned",
                "account banned",
                "number is banned",
            )):
                status = "banned"
    except Exception:
        pass

    if status:
        return status

    # api.whatsapp.com/send check
    try:
        async with session.get(
            WA_WEB, params={"phone": digits}, headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
            ssl=False, allow_redirects=True,
        ) as r:
            body = (await r.text(errors="ignore")).lower()
            if any(w in body for w in (
                "send message", "open whatsapp", "continue to chat",
            )):
                return "active"
            elif any(w in body for w in (
                "not registered", "invalid", "not on whatsapp",
            )):
                return "not_registered"
            elif any(w in body for w in (
                "banned", "blocked", "suspended",
            )):
                return "banned"
    except Exception:
        pass

    return None


# --- Ban type classifier ---

async def _ban_type(session, digits):
    """Classify ban as Admin, Regional, or Personal."""
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    try:
        async with session.get(
            WA_WEB, params={"phone": digits}, headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
            ssl=False, allow_redirects=True,
        ) as r:
            if r.status == 403:
                return "Admin"
            body = (await r.text(errors="ignore")).lower()
            if any(w in body for w in (
                "not available in your country", "not available in your region",
                "service not available", "country is not supported",
            )):
                return "Regional"
            if any(w in body for w in (
                "terms of service", "policy violation", "enforcement",
                "violated our", "spam", "abuse",
            )):
                return "Admin"
    except Exception:
        pass
    return "Personal"


# --- Master check function ---

async def check_one(session, digits: str) -> dict:
    cc, nn  = get_cc_nn(digits)
    display = "+" + digits
    t0      = time.monotonic()

    # Run both methods concurrently
    a_task = asyncio.create_task(_method_a(session, cc, nn))
    b_task = asyncio.create_task(_method_b(session, digits))
    a, b   = await asyncio.gather(a_task, b_task)

    ping_ms = round((time.monotonic() - t0) * 1000)

    a_status = a.get("status")
    a_reason = a.get("reason", "")

    # ── Decide final status ─────────────────────────────────
    # Priority: if either method says active, trust it
    if a_status == "active" or b == "active":
        return {"display": display, "status": "active",
                "ban_type": None, "ping_ms": ping_ms, "note": ""}

    # Both agree: banned
    if a_status == "banned" or b == "banned":
        ban_type = await _ban_type(session, digits)
        note = f"API reason: {a_reason}" if a_reason and a_reason not in ("http_403",) else ""
        return {"display": display, "status": "banned",
                "ban_type": ban_type, "ping_ms": ping_ms, "note": note}

    # Not registered
    if a_status == "not_registered" or b == "not_registered":
        return {"display": display, "status": "not_registered",
                "ban_type": None, "ping_ms": ping_ms, "note": ""}

    # Param error from API — likely our CC split is off; still attempt B result
    if a_status == "param_error":
        if b == "active":
            return {"display": display, "status": "active",
                    "ban_type": None, "ping_ms": ping_ms, "note": ""}
        if b == "not_registered":
            return {"display": display, "status": "not_registered",
                    "ban_type": None, "ping_ms": ping_ms, "note": ""}
        # B gave nothing  — re-try API with alternate CC split
        for clen in (1, 2, 3):
            if str(len(cc)) != str(clen):
                alt_cc = digits[:clen]
                alt_nn = digits[clen:]
                retry  = await _method_a(session, alt_cc, alt_nn)
                if retry.get("status") in ("active", "banned", "not_registered"):
                    rs = retry["status"]
                    bt = None
                    if rs == "banned":
                        bt = await _ban_type(session, digits)
                    return {"display": display, "status": rs,
                            "ban_type": bt, "ping_ms": ping_ms,
                            "note": f"Retry CC len {clen}"}

    # Rate limited
    if a_status == "rate_limited":
        return {"display": display, "status": "rate_limited",
                "ban_type": None, "ping_ms": ping_ms,
                "note": "WhatsApp rate limit — try again shortly"}

    # Timeout / error
    if a_status in ("timeout", "error"):
        return {"display": display, "status": a_status,
                "ban_type": None, "ping_ms": ping_ms, "note": a_reason}

    # Final fallback: unknown
    return {"display": display, "status": "unknown",
            "ban_type": None, "ping_ms": ping_ms,
            "note": "Could not determine — no clear API signal"}


async def check_all(numbers: list) -> list:
    connector = aiohttp.TCPConnector(limit=60, ssl=False, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as sess:
        results = await asyncio.gather(
            *[check_one(sess, n) for n in numbers],
            return_exceptions=True,
        )
    out = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            out.append({"display": "+" + numbers[i], "status": "error",
                        "ban_type": None, "ping_ms": 0, "note": str(r)[:80]})
        else:
            out.append(r)
    return out


# ─────────────────────────────────────────────────────────────
#  FORMATTERS
# ─────────────────────────────────────────────────────────────

STATUS_LABEL = {
    "active":         ("🟢", "ACTIVE — Not Banned"),
    "banned":         ("🔴", "BANNED"),
    "not_registered": ("⚫", "NOT ON WHATSAPP"),
    "rate_limited":   ("⏳", "RATE LIMITED — Retry soon"),
    "timeout":        ("🟡", "TIMEOUT"),
    "error":          ("❌", "ERROR"),
    "unknown":        ("❓", "UNDETERMINED"),
}

BAN_LABEL = {
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
        "║   ⚡  <b>KAGE X Ban Checker</b>  ⚡  v5.0  ║",
        f"║   🗓 {now:<31}║",
        "╚" + "═" * 38 + "╝",
        "",
        "📊 <b>Summary</b>",
        f"   • Total    : <code>{total}</code>",
        f"   • 🔴 Banned  : <code>{banned}</code>",
        f"   • 🟢 Active  : <code>{active}</code>",
        f"   • Other    : <code>{other}</code>",
        f"   • ⏱ Speed   : <code>{elapsed:.2f}s</code>",
        "",
        div, "",
    ]

    for i, r in enumerate(results, 1):
        icon, lbl = STATUS_LABEL.get(r["status"], ("❓", r["status"].upper()))
        bt        = BAN_LABEL.get(r.get("ban_type"), "") if r.get("ban_type") else ""
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


# ─────────────────────────────────────────────────────────────
#  HANDLERS
# ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    msg  = (
        "╔" + "═" * 38 + "╗\n"
        "║   ⚡  <b>KAGE X Ban Checker</b>  ⚡  v5.0  ║\n"
        "╚" + "═" * 38 + "╝\n\n"
        f"👋 Welcome, <b>{name}</b>!\n\n"
        "Checks every WhatsApp number via <b>dual-method API</b>\n"
        "simultaneously — accurate, per-number results.\n\n"
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
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚡ <b>KAGE X — Commands</b>\n\n"
        "<code>!check +234xxxxxxxx</code> — single check\n"
        "<code>!check 234xxx 447xxx</code> — multi check\n"
        "<code>!ping</code> — latency\n"
        "<code>!about</code> — bot info\n\n"
        f"⚡ Max <b>{MAX_NUM}</b> numbers per request."
    )
    target = update.message or update.callback_query.message
    await target.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "╔" + "═" * 34 + "╗\n"
        f"║  ⚡ <b>{BOT_NAME}</b>  {BOT_VERSION}  ║\n"
        "╚" + "═" * 34 + "╝\n\n"
        "🔍 Dual-method WhatsApp detection\n"
        "⚡ Simultaneous per-number checking\n"
        "🎯 Detects Personal, Regional & Admin bans\n"
        "🌐 Global prefix: <code>!</code>"
    )
    target = update.message or update.callback_query.message
    await target.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t0  = time.monotonic()
    msg = await update.message.reply_text("🏓 Pinging...")
    ms  = round((time.monotonic() - t0) * 1000)
    await msg.edit_text(
        f"🏓 <b>Pong!</b>\n⚡ Latency: <code>{ms} ms</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args_text = " ".join(ctx.args) if ctx.args else ""
    numbers   = extract_numbers(args_text)
    user      = update.effective_user

    if not numbers:
        await update.message.reply_text(
            "⚠️ <b>No numbers found.</b>\n\n"
            "Include country code:\n"
            "  <code>!check +2348012345678</code>\n"
            "  <code>!check 2348012345678 447911234567</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    wait = await update.message.reply_text(
        f"⏳ Checking <code>{len(numbers)}</code> number(s) via dual-method API... ✨",
        parse_mode=ParseMode.HTML,
    )
    t0      = time.monotonic()
    results = await check_all(numbers)
    elapsed = time.monotonic() - t0

    requester = f"@{user.username}" if user.username else user.first_name
    await wait.delete()
    await update.message.reply_text(
        build_msg(results, elapsed, requester),
        parse_mode=ParseMode.HTML,
    )


async def on_prefix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.startswith(PREFIX):
        return
    parts    = text[len(PREFIX):].split(None, 1)
    cmd      = parts[0].lower() if parts else ""
    ctx.args = parts[1].split() if len(parts) > 1 else []
    await {
        "check": cmd_check, "start": cmd_start,
        "help":  cmd_help,  "about": cmd_about,
        "ping":  cmd_ping,
    }.get(cmd, _unknown)(update, ctx)


async def _unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").split()[0].lstrip(PREFIX)
    await update.message.reply_text(
        f"❓ Unknown: <code>!{text}</code> — try <code>!help</code>",
        parse_mode=ParseMode.HTML,
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "help":  await cmd_help(update, ctx)
    elif q.data == "about": await cmd_about(update, ctx)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    print(f"[KAGE X ERR] {ctx.error}")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def main():
    token = BOT_TOKEN.strip()
    if not token:
        print("⚠️  BOT_TOKEN not set."); return

    app = Application.builder().token(token).build()
    for cmd, fn in [("start", cmd_start), ("help", cmd_help),
                    ("about", cmd_about), ("ping", cmd_ping), ("check", cmd_check)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_prefix))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    print(f"╔{'='*46}╗")
    print(f"║  ⚡  {BOT_NAME}  ⚡  {BOT_VERSION}  ║")
    print(f"║  Dual-Method Engine — Running...              ║")
    print(f"╚{'='*46}╝\n")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
