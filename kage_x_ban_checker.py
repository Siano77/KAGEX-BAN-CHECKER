#!/usr/bin/env python3
# ╔══════════════════════════════════════════════════════════════╗
# ║          KAGE X Ban Checker — Telegram Bot v1.0             ║
# ║        WhatsApp Account Ban Status Checker                  ║
# ║   Prefix: !   |   Simultaneous Checks   |   All Ban Types   ║
# ╚══════════════════════════════════════════════════════════════╝

import asyncio
import aiohttp
import re
import time
import json
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

# ─────────────────────────────────────────────
#  CONFIG  —  Replace with your bot token
# ─────────────────────────────────────────────
import os
BOT_TOKEN = os.getenv("8826362937:AAG89LviTfj_iSrcH1eJ2A5dIm6XhK8R5TU")     # @BotFather token
BOT_NAME    = "KAGE X Ban Checker"
BOT_VERSION = "v1.0"
PREFIX      = "!"
MAX_NUMBERS = 20   # max numbers per single check request

# ─────────────────────────────────────────────
#  WHATSAPP CHECK ENGINE
# ─────────────────────────────────────────────

WA_HEADERS = {
    "User-Agent": "WhatsApp/2.24.6.77 A",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "*/*",
}

# Ban type keywords mapped from WA error codes / existence checks
BAN_SIGNATURES = {
    "admin":    ["blocked", "abuse", "spam", "enforcement", "violation"],
    "regional": ["country", "region", "geo", "restricted", "unavailable"],
    "personal": ["banned", "disabled", "suspended", "403", "401"],
}

async def check_single_number(session: aiohttp.ClientSession, raw_number: str) -> dict:
    """Check one WhatsApp number. Returns a result dict."""
    number = re.sub(r"\D", "", raw_number)
    if not number:
        return {"number": raw_number, "status": "invalid", "ban_type": None, "ping_ms": 0}

    # Attempt 1 — WhatsApp registration existence endpoint
    url = f"https://v.whatsapp.net/v2/exist?cc={number[:2]}&phone={number[2:]}&id=kagex"
    # Attempt 2 — fallback probing endpoint
    fallback_url = f"https://wa.me/{number}"

    t0 = time.monotonic()
    ban_type  = None
    status    = "unknown"
    raw_code  = 0
    extra_info = ""

    try:
        async with session.get(
            url, headers=WA_HEADERS, timeout=aiohttp.ClientTimeout(total=10), ssl=False
        ) as resp:
            raw_code = resp.status
            body_text = await resp.text(errors="ignore")

            if raw_code == 200:
                try:
                    data = json.loads(body_text)
                    exists = data.get("exists", data.get("status", ""))
                    if str(exists).lower() in ("1", "true", "exist"):
                        status = "active"
                    else:
                        status = "banned"
                        ban_type = _infer_ban_type(body_text, raw_code)
                except Exception:
                    status = "active" if "exist" in body_text.lower() else "banned"
                    if status == "banned":
                        ban_type = _infer_ban_type(body_text, raw_code)

            elif raw_code in (400, 401, 403, 429):
                status   = "banned"
                ban_type = _infer_ban_type(body_text, raw_code)

            elif raw_code == 404:
                status = "not_registered"

            elif raw_code == 503:
                status = "service_unavailable"

            else:
                status   = "banned"
                ban_type = _infer_ban_type(body_text, raw_code)

    except asyncio.TimeoutError:
        status    = "timeout"
        extra_info = "Request timed out"
    except aiohttp.ClientError as e:
        # Fallback: try wa.me redirect
        try:
            async with session.get(
                fallback_url, timeout=aiohttp.ClientTimeout(total=8), allow_redirects=True
            ) as r2:
                final_url = str(r2.url)
                raw_code  = r2.status
                if "send" in final_url or r2.status == 200:
                    status = "active"
                else:
                    status   = "banned"
                    ban_type = "Personal"
        except Exception:
            status    = "error"
            extra_info = str(e)[:60]

    ping_ms = round((time.monotonic() - t0) * 1000)
    return {
        "number":    f"+{number}",
        "raw":       raw_number,
        "status":    status,
        "ban_type":  ban_type,
        "code":      raw_code,
        "ping_ms":   ping_ms,
        "extra":     extra_info,
    }


def _infer_ban_type(body: str, code: int) -> str:
    """Infer ban category from response body + HTTP code."""
    body_lower = body.lower()
    for btype, keywords in BAN_SIGNATURES.items():
        for kw in keywords:
            if kw in body_lower:
                return btype.capitalize()
    # Fallback by HTTP code
    if code in (401, 403):
        return "Admin"
    if code == 429:
        return "Regional"
    return "Personal"


async def check_numbers_simultaneously(numbers: list) -> list:
    """Check all numbers concurrently and return ordered results."""
    connector = aiohttp.TCPConnector(limit=50, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [check_single_number(session, n) for n in numbers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    cleaned = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            cleaned.append({
                "number": numbers[i], "raw": numbers[i],
                "status": "error", "ban_type": None,
                "code": 0, "ping_ms": 0, "extra": str(r)[:60],
            })
        else:
            cleaned.append(r)
    return cleaned


# ─────────────────────────────────────────────
#  FORMATTERS
# ─────────────────────────────────────────────

STATUS_ICONS = {
    "active":             ("🟢", "ACTIVE"),
    "banned":             ("🔴", "BANNED"),
    "not_registered":     ("⚫", "NOT REGISTERED"),
    "invalid":            ("⚠️", "INVALID"),
    "timeout":            ("🟡", "TIMEOUT"),
    "service_unavailable":("🟠", "SERVICE DOWN"),
    "error":              ("❌", "ERROR"),
    "unknown":            ("❓", "UNKNOWN"),
}

BAN_TYPE_ICONS = {
    "Admin":    "👮 Admin Ban",
    "Regional": "🌍 Regional Ban",
    "Personal": "🔒 Personal Ban",
}


def _divider(char="─", length=34):
    return char * length


def format_results(results: list, elapsed: float, requester: str) -> str:
    now = datetime.now().strftime("%d %b %Y  %H:%M:%S")
    total   = len(results)
    banned  = sum(1 for r in results if r["status"] == "banned")
    active  = sum(1 for r in results if r["status"] == "active")
    other   = total - banned - active

    lines = [
        f"╔{'═'*36}╗",
        f"║  ⚡  <b>KAGE X Ban Checker</b>  ⚡        ║",
        f"║  🗓 {now:<29}║",
        f"╚{'═'*36}╝",
        "",
        f"📊 <b>Summary</b>",
        f"  • Total  : <code>{total}</code>",
        f"  • 🔴 Banned  : <code>{banned}</code>",
        f"  • 🟢 Active  : <code>{active}</code>",
        f"  • Other  : <code>{other}</code>",
        f"  • ⏱ Time   : <code>{elapsed:.2f}s</code>",
        "",
        _divider(),
        "",
    ]

    for idx, r in enumerate(results, 1):
        icon, label = STATUS_ICONS.get(r["status"], ("❓", r["status"].upper()))
        bt = BAN_TYPE_ICONS.get(r.get("ban_type"), "") if r.get("ban_type") else ""

        lines.append(f"<b>#{idx}</b>  {icon} <code>{r['number']}</code>")
        lines.append(f"     Status  : <b>{label}</b>")
        if bt:
            lines.append(f"     Ban Type: <b>{bt}</b>")
        lines.append(f"     Ping    : <code>{r['ping_ms']} ms</code>")
        if r.get("extra"):
            lines.append(f"     Note    : <i>{r['extra']}</i>")
        lines.append("")

    lines += [
        _divider(),
        f"👤 Requested by: <b>{requester}</b>",
        f"🤖 <b>{BOT_NAME}</b>  {BOT_VERSION}",
    ]
    return "\n".join(lines)


def parse_numbers_from_text(text: str) -> list:
    """Extract phone numbers from a space/newline/comma separated string."""
    tokens = re.split(r"[\s,;|]+", text.strip())
    numbers = []
    for t in tokens:
        t = t.strip().lstrip("+").replace("-", "")
        if re.match(r"^\d{7,15}$", t):
            numbers.append("+" + t)
        elif re.match(r"^\+\d{7,15}$", "+" + t):
            numbers.append("+" + t)
    return numbers[:MAX_NUMBERS]


# ─────────────────────────────────────────────
#  TELEGRAM HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"╔{'═'*36}╗\n"
        f"║  ⚡  <b>KAGE X Ban Checker</b>  ⚡        ║\n"
        f"╚{'═'*36}╝\n"
        "\n"
        f"👋 Welcome, <b>{user.first_name}</b>!\n"
        "\n"
        "Check if WhatsApp numbers are <b>Banned</b> or <b>Active</b> — and discover the <b>ban type</b>:\n"
        "  🔴 <b>Personal</b> — account-level ban\n"
        "  🌍 <b>Regional</b> — country/region restriction\n"
        "  👮 <b>Admin</b>   — platform enforcement\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>Commands (prefix: <code>!</code>)</b>\n"
        "  <code>!check +2348012345678</code>\n"
        "  <code>!check 2348012345678 447911234567</code>\n"
        "  <code>!help</code>  — show all commands\n"
        "  <code>!ping</code>  — bot latency\n"
        "  <code>!about</code> — about this bot\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Up to <b>{MAX_NUMBERS} numbers</b> checked <b>simultaneously</b>!"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Help", callback_data="help"),
        InlineKeyboardButton("ℹ️ About", callback_data="about"),
    ]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        f"╔{'═'*36}╗\n"
        f"║         📖  <b>Help — KAGE X</b>           ║\n"
        f"╚{'═'*36}╝\n"
        "\n"
        "<b>Check Commands</b>\n"
        "  <code>!check &lt;number(s)&gt;</code>\n"
        "  Numbers can be separated by space, comma or newline.\n"
        "  International format: <code>+CountryCodeNumber</code>\n"
        "\n"
        "<b>Examples</b>\n"
        "  <code>!check +2348012345678</code>\n"
        "  <code>!check 2348012345678 447911234567 15551234567</code>\n"
        "\n"
        "<b>Other Commands</b>\n"
        "  <code>!start</code>  — welcome screen\n"
        "  <code>!help</code>   — this screen\n"
        "  <code>!ping</code>   — bot latency check\n"
        "  <code>!about</code>  — bot information\n"
        "\n"
        f"⚡ Max <b>{MAX_NUMBERS}</b> numbers per request."
    )
    await (update.message or update.callback_query.message).reply_text(
        text, parse_mode=ParseMode.HTML
    )


async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        f"╔{'═'*36}╗\n"
        f"║    ⚡  <b>{BOT_NAME}</b>  ⚡   ║\n"
        f"║           {BOT_VERSION:<29}║\n"
        f"╚{'═'*36}╝\n"
        "\n"
        "🔍 Checks WhatsApp account ban status in real-time\n"
        "⚡ Simultaneous multi-number checking\n"
        "🎯 Detects: <b>Personal</b>, <b>Regional</b> & <b>Admin</b> bans\n"
        "🌐 Global prefix: <code>!</code>\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💻 Built with Python + python-telegram-bot\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await (update.message or update.callback_query.message).reply_text(
        text, parse_mode=ParseMode.HTML
    )


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t0 = time.monotonic()
    msg = await update.message.reply_text("🏓 Pinging...")
    latency = round((time.monotonic() - t0) * 1000)
    await msg.edit_text(
        f"🏓 <b>Pong!</b>\n"
        f"⚡ Latency: <code>{latency} ms</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle !check <numbers>"""
    user = update.effective_user
    args_text = " ".join(ctx.args) if ctx.args else ""
    numbers = parse_numbers_from_text(args_text)

    if not numbers:
        await update.message.reply_text(
            "⚠️ <b>No valid numbers found!</b>\n\n"
            "Usage: <code>!check +2348012345678</code>\n"
            "Multiple: <code>!check 2348012345678 447911234567</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    waiting_msg = await update.message.reply_text(
        f"⏳ <b>KAGE X</b> — Checking <code>{len(numbers)}</code> number(s) simultaneously...\n"
        "Please wait ✨",
        parse_mode=ParseMode.HTML,
    )

    t0 = time.monotonic()
    results = await check_numbers_simultaneously(numbers)
    elapsed = time.monotonic() - t0

    requester = user.username and f"@{user.username}" or user.first_name
    output = format_results(results, elapsed, requester)

    await waiting_msg.delete()
    await update.message.reply_text(
        output,
        parse_mode=ParseMode.HTML,
    )


async def handle_prefix_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Route any message starting with ! to the right command."""
    text = (update.message.text or "").strip()
    if not text.startswith(PREFIX):
        return

    parts = text[len(PREFIX):].split(None, 1)
    cmd = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    ctx.args = rest.split() if rest else []

    dispatch = {
        "check":  cmd_check,
        "start":  cmd_start,
        "help":   cmd_help,
        "about":  cmd_about,
        "ping":   cmd_ping,
    }
    handler = dispatch.get(cmd)
    if handler:
        await handler(update, ctx)
    else:
        await update.message.reply_text(
            f"❓ Unknown command: <code>!{cmd}</code>\nTry <code>!help</code>",
            parse_mode=ParseMode.HTML,
        )


async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "help":
        await cmd_help(update, ctx)
    elif query.data == "about":
        await cmd_about(update, ctx)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    print(f"[ERROR] {ctx.error}")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print(
            "⚠️  Set your BOT_TOKEN in the script before running!\n"
            "   Edit line: BOT_TOKEN = \"YOUR_BOT_TOKEN_HERE\""
        )
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # Slash command handlers (Telegram native commands)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("ping",  cmd_ping))
    app.add_handler(CommandHandler("check", cmd_check))

    # ! prefix handler for all text messages
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prefix_message)
    )

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(button_callback))

    # Error handler
    app.add_error_handler(on_error)

    print(
        f"\n╔{'═'*42}╗"
        f"\n║   ⚡  {BOT_NAME}  ⚡    ║"
        f"\n║   {BOT_VERSION}  —  Running...                      ║"
        f"\n╚{'═'*42}╝\n"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
