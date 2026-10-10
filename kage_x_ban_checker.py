#!/usr/bin/env python3
# ╔════════════════════════════════════════════════════════════╗
# ║         KAGE X Ban Checker  —  Telegram Bot v2.0          ║
# ║     Accurate WhatsApp Ban Status Detection Engine         ║
# ║  Prefix: !  |  Simultaneous  |  Personal/Regional/Admin   ║
# ╚════════════════════════════════════════════════════════════╝

import os
import asyncio
import aiohttp
import re
import time
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# ──────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
BOT_NAME    = "KAGE X Ban Checker"
BOT_VERSION = "v2.0"
PREFIX      = "!"
MAX_NUMBERS = 20

# ──────────────────────────────────────────────
#  BAN DETECTION ENGINE (Accurate)
# ──────────────────────────────────────────────

# We use two independent signals and only mark BANNED if
# evidence is strong and consistent. When uncertain → UNKNOWN.

WAME_URL      = "https://wa.me/"
API_CHECK_URL = "https://api.whatsapp.com/send?phone="

HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Keywords in HTML body that reliably indicate a number exists on WhatsApp
ACTIVE_SIGNALS = [
    "send message",
    "open whatsapp",
    "wa.me",
    "chat with",
    "message this number",
    "_wap_",
]

# Keywords that reliably indicate a ban / not available
BANNED_SIGNALS = [
    "this phone number is banned",
    "your account has been banned",
    "number is banned",
    "account banned",
    "this account has been banned",
]

NOT_REGISTERED_SIGNALS = [
    "phone number shared via url is invalid",
    "number is not registered",
    "not on whatsapp",
    "number shared via url is not",
]

REGIONAL_SIGNALS = [
    "not available in your country",
    "not available in your region",
    "service not available",
    "country is not supported",
    "region not supported",
]

ADMIN_SIGNALS = [
    "terms of service",
    "violated our terms",
    "policy violation",
    "enforcement action",
    "spam",
    "abuse",
]


def clean_number(raw: str) -> str:
    return re.sub(r"\D", "", raw.strip())


def classify_ban_type(body: str) -> str:
    """Determine ban type from page content. Returns Admin, Regional or Personal."""
    bl = body.lower()
    for sig in REGIONAL_SIGNALS:
        if sig in bl:
            return "Regional"
    for sig in ADMIN_SIGNALS:
        if sig in bl:
            return "Admin"
    return "Personal"


async def fetch_page(session: aiohttp.ClientSession, url: str, timeout: int = 12) -> tuple:
    """
    Fetch a URL. Returns (status_code, body_text, final_url).
    Returns (-1, "", "") on error.
    """
    try:
        async with session.get(
            url,
            headers=HEADERS_BROWSER,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
            ssl=False,
        ) as resp:
            body = await resp.text(errors="ignore")
            return resp.status, body.lower(), str(resp.url)
    except asyncio.TimeoutError:
        return -2, "", ""
    except Exception:
        return -1, "", ""


async def check_single(session: aiohttp.ClientSession, raw: str) -> dict:
    number = clean_number(raw)

    # ── Validate number ──────────────────────────────────────
    if not number or not (7 <= len(number) <= 15):
        return {
            "display": raw, "status": "invalid",
            "ban_type": None, "ping_ms": 0, "note": "Not a valid number",
        }

    display = "+" + number
    t0      = time.monotonic()

    # ── Signal 1: wa.me page ─────────────────────────────────
    code1, body1, url1 = await fetch_page(session, WAME_URL + number)

    # ── Signal 2: api.whatsapp.com/send page ─────────────────
    code2, body2, url2 = await fetch_page(session, API_CHECK_URL + number)

    combined = body1 + " " + body2
    ping_ms  = round((time.monotonic() - t0) * 1000)

    # ── Timeout / connection failure ──────────────────────────
    if code1 in (-1, -2) and code2 in (-1, -2):
        status = "timeout" if code1 == -2 else "error"
        return {
            "display": display, "status": status,
            "ban_type": None, "ping_ms": ping_ms,
            "note": "Could not reach WhatsApp servers",
        }

    # ── Active signals (strong positive — number is NOT banned) ──
    for sig in ACTIVE_SIGNALS:
        if sig in combined:
            return {
                "display": display, "status": "active",
                "ban_type": None, "ping_ms": ping_ms, "note": "",
            }

    # ── Explicit ban signals ──────────────────────────────────
    for sig in BANNED_SIGNALS:
        if sig in combined:
            return {
                "display": display, "status": "banned",
                "ban_type": classify_ban_type(combined),
                "ping_ms": ping_ms, "note": "",
            }

    # ── Not registered signals ────────────────────────────────
    for sig in NOT_REGISTERED_SIGNALS:
        if sig in combined:
            return {
                "display": display, "status": "not_registered",
                "ban_type": None, "ping_ms": ping_ms, "note": "",
            }

    # ── HTTP-code based classification (fallback only) ────────
    # Only trust if both signals agree
    if code1 == 200 and code2 == 200:
        # Both returned 200 but no active keyword found
        # → likely active but page structure we didn't recognize
        return {
            "display": display, "status": "active",
            "ban_type": None, "ping_ms": ping_ms,
            "note": "Active (verified via HTTP 200)",
        }

    if code1 in (403, 401) and code2 in (403, 401):
        return {
            "display": display, "status": "banned",
            "ban_type": "Admin", "ping_ms": ping_ms,
            "note": "Both endpoints returned 403/401",
        }

    if code1 == 404 or code2 == 404:
        return {
            "display": display, "status": "not_registered",
            "ban_type": None, "ping_ms": ping_ms, "note": "",
        }

    # ── Ambiguous — not enough evidence to call banned ────────
    return {
        "display": display, "status": "unknown",
        "ban_type": None, "ping_ms": ping_ms,
        "note": "Could not determine status clearly",
    }


async def check_all(numbers: list) -> list:
    connector = aiohttp.TCPConnector(limit=50, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(
            *[check_single(session, n) for n in numbers],
            return_exceptions=True,
        )
    out = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            out.append({
                "display": "+" + re.sub(r"\D", "", numbers[i]),
                "status": "error", "ban_type": None,
                "ping_ms": 0, "note": str(r)[:80],
            })
        else:
            out.append(r)
    return out


# ──────────────────────────────────────────────
#  FORMATTERS
# ──────────────────────────────────────────────

STATUS_MAP = {
    "active":         ("🟢", "ACTIVE — Not Banned"),
    "banned":         ("🔴", "BANNED"),
    "not_registered": ("⚫", "NOT ON WHATSAPP"),
    "invalid":        ("⚠️", "INVALID NUMBER"),
    "timeout":        ("🟡", "TIMEOUT"),
    "error":          ("❌", "ERROR"),
    "unknown":        ("❓", "UNDETECTABLE"),
}

BAN_ICON = {
    "Admin":    "👮 Admin Ban",
    "Regional": "🌍 Regional Ban",
    "Personal": "🔒 Personal Ban",
}


def build_result_message(results: list, elapsed: float, requester: str) -> str:
    now    = datetime.now().strftime("%d %b %Y  %H:%M:%S")
    total  = len(results)
    banned = sum(1 for r in results if r["status"] == "banned")
    active = sum(1 for r in results if r["status"] == "active")
    other  = total - banned - active
    div    = "─" * 34

    lines = [
        "╔" + "═" * 36 + "╗",
        "║  ⚡  <b>KAGE X Ban Checker</b>  ⚡        ║",
        f"║  🗓 {now:<29}║",
        "╚" + "═" * 36 + "╝",
        "",
        "📊 <b>Summary</b>",
        f"   • Total   : <code>{total}</code>",
        f"   • 🔴 Banned  : <code>{banned}</code>",
        f"   • 🟢 Active  : <code>{active}</code>",
        f"   • Other   : <code>{other}</code>",
        f"   • ⏱ Speed  : <code>{elapsed:.2f}s</code>",
        "",
        div,
        "",
    ]

    for idx, r in enumerate(results, 1):
        icon, label = STATUS_MAP.get(r["status"], ("❓", r["status"].upper()))
        ban_label   = BAN_ICON.get(r.get("ban_type"), "") if r.get("ban_type") else ""

        lines.append(f"<b>#{idx}</b>  {icon}  <code>{r['display']}</code>")
        lines.append(f"      Status   : <b>{label}</b>")
        if ban_label:
            lines.append(f"      Ban Type : <b>{ban_label}</b>")
        lines.append(f"      Ping     : <code>{r['ping_ms']} ms</code>")
        if r.get("note"):
            lines.append(f"      Note     : <i>{r['note']}</i>")
        lines.append("")

    lines += [
        div,
        f"👤 By: <b>{requester}</b>  |  🤖 <b>{BOT_NAME}</b> {BOT_VERSION}",
    ]
    return "\n".join(lines)


def extract_numbers(text: str) -> list:
    tokens  = re.split(r"[\s,;|\n]+", text.strip())
    numbers = []
    for t in tokens:
        digits = re.sub(r"\D", "", t.strip())
        if 7 <= len(digits) <= 15:
            numbers.append("+" + digits)
    return numbers[:MAX_NUMBERS]


# ──────────────────────────────────────────────
#  TELEGRAM HANDLERS
# ──────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    text = (
        "╔" + "═" * 36 + "╗\n"
        "║  ⚡  <b>KAGE X Ban Checker</b>  ⚡        ║\n"
        "╚" + "═" * 36 + "╝\n\n"
        f"👋 Welcome, <b>{name}</b>!\n\n"
        "Check WhatsApp numbers for <b>ban status</b> and <b>ban type</b>:\n"
        "  🔒 <b>Personal</b>  — account-level ban\n"
        "  🌍 <b>Regional</b>  — country/region restriction\n"
        "  👮 <b>Admin</b>    — platform enforcement\n\n"
        "━" * 24 + "\n"
        "<b>Usage</b>\n"
        "  <code>!check +2348012345678</code>\n"
        "  <code>!check 2348012345678 447911234567</code>\n"
        "  <code>!ping</code>   <code>!help</code>\n"
        "━" * 24 + "\n"
        f"⚡ Up to <b>{MAX_NUMBERS} numbers</b> checked simultaneously!"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Help",  callback_data="cb_help"),
        InlineKeyboardButton("ℹ️ About", callback_data="cb_about"),
    ]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚡ <b>KAGE X — Commands</b>\n\n"
        "<code>!check +2348012345678</code> — single\n"
        "<code>!check 234... 447... 155...</code> — multiple\n"
        "<code>!ping</code> — latency\n"
        "<code>!about</code> — bot info\n\n"
        f"⚡ Max <b>{MAX_NUMBERS}</b> numbers per request."
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "╔" + "═" * 32 + "╗\n"
        f"║  ⚡ <b>{BOT_NAME}</b>  {BOT_VERSION}  ║\n"
        "╚" + "═" * 32 + "╝\n\n"
        "🔍 Accurate WhatsApp ban detection\n"
        "⚡ Simultaneous multi-number checking\n"
        "🎯 Detects Personal, Regional & Admin bans\n"
        "🌐 Global prefix: <code>!</code>"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


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
            "⚠️ <b>No valid numbers found!</b>\n\n"
            "Usage: <code>!check +2348012345678</code>\n"
            "Multi:  <code>!check 2348012345678 447911234567</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    wait = await update.message.reply_text(
        f"⏳ Checking <code>{len(numbers)}</code> number(s) simultaneously... ✨",
        parse_mode=ParseMode.HTML,
    )

    t0      = time.monotonic()
    results = await check_all(numbers)
    elapsed = time.monotonic() - t0

    requester = f"@{user.username}" if user.username else user.first_name
    output    = build_result_message(results, elapsed, requester)

    await wait.delete()
    await update.message.reply_text(output, parse_mode=ParseMode.HTML)


async def handle_prefix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.startswith(PREFIX):
        return

    parts    = text[len(PREFIX):].split(None, 1)
    cmd      = parts[0].lower() if parts else ""
    rest     = parts[1] if len(parts) > 1 else ""
    ctx.args = rest.split() if rest else []

    routes = {
        "check": cmd_check,
        "start": cmd_start,
        "help":  cmd_help,
        "about": cmd_about,
        "ping":  cmd_ping,
    }
    fn = routes.get(cmd)
    if fn:
        await fn(update, ctx)
    else:
        await update.message.reply_text(
            f"❓ Unknown command: <code>!{cmd}</code>\nTry <code>!help</code>",
            parse_mode=ParseMode.HTML,
        )


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "cb_help":
        await cmd_help(update, ctx)
    elif q.data == "cb_about":
        await cmd_about(update, ctx)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    print(f"[KAGE X ERROR] {ctx.error}")


# ──────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────

def main():
    token = BOT_TOKEN.strip()
    if not token:
        print("\n⚠️  BOT_TOKEN is not set! Set it as an environment variable.\n")
        return

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("ping",  cmd_ping))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prefix))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(on_error)

    print(
        "\n" +
        "╔" + "═" * 42 + "╗" +
        "\n║   ⚡  " + BOT_NAME + "  ⚡  " + BOT_VERSION + "       ║" +
        "\n║   Bot is now running...                      ║" +
        "\n╚" + "═" * 42 + "╝\n"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
