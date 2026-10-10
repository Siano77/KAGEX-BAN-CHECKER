#!/usr/bin/env python3
# ╔════════════════════════════════════════════════════════════╗
# ║         KAGE X Ban Checker  —  Telegram Bot v1.1          ║
# ║     WhatsApp Account Ban Status Checker (Fixed)           ║
# ║  Prefix: !  |  Simultaneous  |  Personal/Regional/Admin   ║
# ╚════════════════════════════════════════════════════════════╝

import os
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
#  CONFIG
# ─────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
BOT_NAME    = "KAGE X Ban Checker"
BOT_VERSION = "v1.1"
PREFIX      = "!"
MAX_NUMBERS = 20

# ─────────────────────────────────────────────
#  BAN CHECK ENGINE
# ─────────────────────────────────────────────

WA_CHECK_URL  = "https://v.whatsapp.net/v2/exist"
WA_FALLBACK   = "https://wa.me/"

WA_HEADERS = {
    "User-Agent": "WhatsApp/2.24.6.77 A",
    "Accept": "*/*",
}

BAN_SIGNATURES = {
    "Admin":    ["blocked", "abuse", "spam", "enforcement", "violation", "403", "401"],
    "Regional": ["country", "region", "geo", "restricted", "unavailable", "429"],
    "Personal": ["banned", "disabled", "suspended"],
}


def clean_number(raw: str) -> str:
    """Strip everything except digits."""
    return re.sub(r"\D", "", raw.strip())


def infer_ban_type(body: str, code: int) -> str:
    body_lower = body.lower()
    for btype, keywords in BAN_SIGNATURES.items():
        for kw in keywords:
            if kw in body_lower:
                return btype
    # fallback by HTTP code
    if code == 403:
        return "Admin"
    if code == 429:
        return "Regional"
    return "Personal"


async def check_single(session: aiohttp.ClientSession, raw: str) -> dict:
    number = clean_number(raw)

    if not number or len(number) < 7 or len(number) > 15:
        return {
            "display": raw, "number": raw,
            "status": "invalid", "ban_type": None,
            "ping_ms": 0, "note": "Not a valid number",
        }

    display = "+" + number
    t0 = time.monotonic()
    status   = "unknown"
    ban_type = None
    note     = ""
    http_code = 0

    # ── Primary check ──────────────────────────────────────────
    params = {"cc": number[:2], "phone": number[2:], "id": "kagex"}
    try:
        async with session.get(
            WA_CHECK_URL,
            params=params,
            headers=WA_HEADERS,
            timeout=aiohttp.ClientTimeout(total=10),
            ssl=False,
        ) as resp:
            http_code = resp.status
            body      = await resp.text(errors="ignore")

            if http_code == 200:
                try:
                    data   = json.loads(body)
                    exists = str(data.get("exists", data.get("status", ""))).lower()
                    if exists in ("1", "true", "exist", "yes"):
                        status = "active"
                    else:
                        status   = "banned"
                        ban_type = infer_ban_type(body, http_code)
                except (json.JSONDecodeError, Exception):
                    if "exist" in body.lower():
                        status = "active"
                    else:
                        status   = "banned"
                        ban_type = infer_ban_type(body, http_code)

            elif http_code == 404:
                status = "not_registered"

            elif http_code in (401, 403):
                status   = "banned"
                ban_type = "Admin"

            elif http_code == 429:
                status   = "banned"
                ban_type = "Regional"

            elif http_code == 503:
                status = "service_unavailable"
                note   = "WhatsApp servers unavailable"

            else:
                status   = "banned"
                ban_type = infer_ban_type(body, http_code)

    except asyncio.TimeoutError:
        status = "timeout"
        note   = "Request timed out"

    except aiohttp.ClientConnectorError:
        # ── Fallback: probe wa.me redirect ────────────────────
        try:
            fallback_url = WA_FALLBACK + number
            async with session.get(
                fallback_url,
                timeout=aiohttp.ClientTimeout(total=8),
                allow_redirects=True,
                ssl=False,
            ) as r2:
                http_code = r2.status
                final_url = str(r2.url)
                if r2.status == 200 and "send" in final_url:
                    status = "active"
                else:
                    status   = "banned"
                    ban_type = "Personal"
        except Exception as e:
            status = "error"
            note   = str(e)[:80]

    except Exception as e:
        status = "error"
        note   = str(e)[:80]

    ping_ms = round((time.monotonic() - t0) * 1000)
    return {
        "display":  display,
        "number":   number,
        "status":   status,
        "ban_type": ban_type,
        "ping_ms":  ping_ms,
        "code":     http_code,
        "note":     note,
    }


async def check_all(numbers: list) -> list:
    """Run all checks concurrently."""
    connector = aiohttp.TCPConnector(limit=50, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks   = [check_single(session, n) for n in numbers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            out.append({
                "display": numbers[i], "number": numbers[i],
                "status": "error", "ban_type": None,
                "ping_ms": 0, "code": 0, "note": str(r)[:80],
            })
        else:
            out.append(r)
    return out


# ─────────────────────────────────────────────
#  FORMATTERS
# ─────────────────────────────────────────────

STATUS_MAP = {
    "active":              ("🟢", "ACTIVE — Not Banned"),
    "banned":              ("🔴", "BANNED"),
    "not_registered":      ("⚫", "NOT REGISTERED"),
    "invalid":             ("⚠️", "INVALID NUMBER"),
    "timeout":             ("🟡", "TIMEOUT"),
    "service_unavailable": ("🟠", "SERVICE DOWN"),
    "error":               ("❌", "ERROR"),
    "unknown":             ("❓", "UNKNOWN"),
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

    div = "─" * 34

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
        ban_label   = BAN_ICON.get(r["ban_type"], "") if r["ban_type"] else ""

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
        f"👤 By: <b>{requester}</b>",
        f"🤖 <b>{BOT_NAME}</b>  {BOT_VERSION}",
    ]
    return "\n".join(lines)


def extract_numbers(text: str) -> list:
    tokens  = re.split(r"[\s,;|\n]+", text.strip())
    numbers = []
    for t in tokens:
        t = t.strip().lstrip("+")
        t = re.sub(r"\D", "", t)
        if 7 <= len(t) <= 15:
            numbers.append("+" + t)
    return numbers[:MAX_NUMBERS]


# ─────────────────────────────────────────────
#  TELEGRAM HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    text = (
        "╔" + "═" * 36 + "╗\n"
        "║  ⚡  <b>KAGE X Ban Checker</b>  ⚡        ║\n"
        "╚" + "═" * 36 + "╝\n"
        "\n"
        f"👋 Welcome, <b>{name}</b>!\n\n"
        "Check WhatsApp numbers for <b>ban status</b> and <b>ban type</b>:\n"
        "  🔒 <b>Personal</b>  — account-level ban\n"
        "  🌍 <b>Regional</b>  — country/region restriction\n"
        "  👮 <b>Admin</b>    — platform enforcement\n"
        "\n"
        "━" * 24 + "\n"
        "📌 <b>Commands (prefix <code>!</code>)</b>\n"
        "  <code>!check +2348012345678</code>\n"
        "  <code>!check 2348012345678 447911234567</code>\n"
        "  <code>!ping</code>  — latency\n"
        "  <code>!help</code>  — all commands\n"
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
        "╔" + "═" * 34 + "╗\n"
        "║      📖 <b>KAGE X — Help</b>           ║\n"
        "╚" + "═" * 34 + "╝\n\n"
        "<b>Check a number</b>\n"
        "  <code>!check +CountryCodeNumber</code>\n\n"
        "<b>Check multiple (simultaneous)</b>\n"
        "  <code>!check 2348012345678 447911234567 15551234567</code>\n\n"
        "<b>Other Commands</b>\n"
        "  <code>!start</code>   — welcome\n"
        "  <code>!ping</code>    — bot latency\n"
        "  <code>!help</code>    — this screen\n"
        "  <code>!about</code>   — bot info\n\n"
        f"⚡ Max <b>{MAX_NUMBERS}</b> numbers per request."
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "╔" + "═" * 34 + "╗\n"
        f"║   ⚡ <b>{BOT_NAME}</b>  ⚡   ║\n"
        f"║          {BOT_VERSION:<25}║\n"
        "╚" + "═" * 34 + "╝\n\n"
        "🔍 Checks WhatsApp ban status in real-time\n"
        "⚡ Simultaneous multi-number checking\n"
        "🎯 Detects Personal, Regional & Admin bans\n"
        "🌐 Global prefix: <code>!</code>\n\n"
        "━" * 22 + "\n"
        "💻 Built with Python + python-telegram-bot"
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
    """Route messages starting with ! to the correct handler."""
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


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    token = BOT_TOKEN.strip()
    if not token:
        print(
            "\n⚠️  BOT_TOKEN is not set!\n"
            "   Set it as an environment variable:\n"
            "   export BOT_TOKEN=your_token_here\n"
        )
        return

    app = Application.builder().token(token).build()

    # Native slash commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("ping",  cmd_ping))
    app.add_handler(CommandHandler("check", cmd_check))

    # ! prefix for all text messages
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prefix)
    )

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Error handler
    app.add_error_handler(on_error)

    print(
        "\n" +
        "╔" + "═" * 42 + "╗" +
        "\n║   ⚡  " + BOT_NAME + "  ⚡       ║" +
        "\n║   " + BOT_VERSION + "  —  Bot is now running...     ║" +
        "\n╚" + "═" * 42 + "╝\n"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
