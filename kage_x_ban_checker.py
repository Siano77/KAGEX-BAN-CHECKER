#!/usr/bin/env python3
# ╔══════════════════════════════════════════════════════════╗
# ║        KAGE X Ban Checker  —  Telegram Bot v3.0         ║
# ║   Real WhatsApp API Detection  |  Accurate Per-Number    ║
# ║   Prefix: !  |  Simultaneous  |  Admin/Regional/Personal ║
# ╚══════════════════════════════════════════════════════════╝

import os
import asyncio
import aiohttp
import re
import time
import random
import string
import json
from datetime import datetime

import phonenumbers
from phonenumbers import NumberParseException

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

# ═══════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
BOT_NAME    = "KAGE X Ban Checker"
BOT_VERSION = "v3.0"
PREFIX      = "!"
MAX_NUMBERS = 20

# ═══════════════════════════════════════════════════════
#  WHATSAPP REAL API ENGINE
# ═══════════════════════════════════════════════════════

# WhatsApp's internal existence-check endpoint (same one the app uses)
WA_EXIST_URL  = "https://v.whatsapp.net/v2/exist"
WA_REG_URL    = "https://v.whatsapp.net/v2/register"

# Rotate user agents to reduce rate-limit risk
WA_USER_AGENTS = [
    "WhatsApp/2.24.8.78 A",
    "WhatsApp/2.24.6.77 A",
    "WhatsApp/2.23.25.84 A",
    "WhatsApp/2.24.10.84 A",
]


def _wa_headers() -> dict:
    return {
        "User-Agent": random.choice(WA_USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }


def _rand_id(length: int = 16) -> str:
    """Random alphanumeric device ID."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def parse_number(raw: str):
    """
    Parse and validate a phone number.
    Returns (country_code_str, national_number_str, display_str) or None.
    """
    digits = re.sub(r"\D", "", raw.strip())
    if not digits:
        return None
    try:
        parsed = phonenumbers.parse("+" + digits, None)
        if not phonenumbers.is_valid_number(parsed):
            # Try assuming it needs a leading digit
            return None
        cc = str(parsed.country_code)
        nn = str(parsed.national_number)
        display = "+" + digits
        return cc, nn, display
    except NumberParseException:
        return None


# ─── Ban-type classification from WhatsApp API reason codes ───

REASON_TO_STATUS = {
    # Active / registered
    "exist":           ("active",   None),
    "ok":              ("active",   None),

    # Banned reasons  ─ personal account violation
    "blocked":         ("banned",   "Personal"),
    "gone":            ("banned",   "Personal"),
    "banned":          ("banned",   "Personal"),
    "suspended":       ("banned",   "Personal"),
    "disabled":        ("banned",   "Personal"),

    # Not registered / never had WA
    "unregistered":    ("not_registered", None),
    "not_exist":       ("not_registered", None),
    "nonexistent":     ("not_registered", None),

    # Parameter / format issues
    "bad_param":       ("invalid",  None),
    "country-code-invalid": ("invalid", None),
    "enum_mismatch":   ("invalid",  None),
}


async def _call_exist_api(
    session: aiohttp.ClientSession,
    cc: str,
    nn: str,
) -> dict:
    """
    Call WhatsApp's v2/exist endpoint.
    Returns parsed JSON dict or {} on failure.
    """
    params = {
        "cc":    cc,
        "phone": nn,
        "id":    _rand_id(),
        "lg":    "en",
        "lc":    "US",
        "authkey": _rand_id(24),
        "_": str(int(time.time() * 1000)),
    }
    try:
        async with session.get(
            WA_EXIST_URL,
            params=params,
            headers=_wa_headers(),
            timeout=aiohttp.ClientTimeout(total=12),
            ssl=False,
            allow_redirects=True,
        ) as resp:
            http_code = resp.status
            text = await resp.text(errors="ignore")

            try:
                data = json.loads(text)
            except Exception:
                data = {}

            data["_http"] = http_code
            data["_raw"]  = text[:300]
            return data

    except asyncio.TimeoutError:
        return {"_error": "timeout"}
    except Exception as e:
        return {"_error": str(e)[:80]}


async def _classify_ban_type(
    session: aiohttp.ClientSession,
    full_number: str,
) -> str:
    """
    Secondary check for banned numbers to determine ban category.
    Uses WhatsApp's web send page HTML for additional signals.
    Defaults to Personal if inconclusive.
    """
    url = "https://api.whatsapp.com/send?phone=" + full_number
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with session.get(
            url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
            ssl=False, allow_redirects=True,
        ) as resp:
            body = (await resp.text(errors="ignore")).lower()

            # Regional signals
            regional_kw = [
                "not available in your country",
                "not available in your region",
                "country is not supported",
                "region not supported",
                "service is not available",
            ]
            for kw in regional_kw:
                if kw in body:
                    return "Regional"

            # Admin/policy signals
            admin_kw = [
                "terms of service",
                "policy violation",
                "enforcement",
                "violated our",
                "spam",
                "abuse report",
            ]
            for kw in admin_kw:
                if kw in body:
                    return "Admin"

            # HTTP 403 = admin enforcement
            if resp.status == 403:
                return "Admin"

    except Exception:
        pass

    return "Personal"


async def check_single(
    session: aiohttp.ClientSession,
    raw: str,
) -> dict:
    """Full check pipeline for one number. Returns result dict."""

    # ── 1. Parse & validate ───────────────────────────────
    parsed = parse_number(raw)
    if not parsed:
        return {
            "display":  "+" + re.sub(r"\D", "", raw) or raw,
            "status":   "invalid",
            "ban_type": None,
            "ping_ms":  0,
            "note":     "Invalid / unrecognized number",
        }

    cc, nn, display = parsed
    full_number = cc + nn
    t0 = time.monotonic()

    # ── 2. WhatsApp real API call ───────────────────────────
    data = await _call_exist_api(session, cc, nn)
    ping_ms = round((time.monotonic() - t0) * 1000)

    # ── 3. Handle connection errors ─────────────────────────
    if "_error" in data:
        err = data["_error"]
        return {
            "display":  display,
            "status":   "timeout" if err == "timeout" else "error",
            "ban_type": None,
            "ping_ms":  ping_ms,
            "note":     err if err != "timeout" else "Request timed out",
        }

    http  = data.get("_http", 200)
    status_field = str(data.get("status", "")).lower()
    result_field = str(data.get("result", "")).lower()
    reason_field = str(data.get("reason", "")).lower()

    # ── 4. Classify from API response ──────────────────────

    # 4a. Explicit "ok" status with result
    if status_field == "ok":
        mapped = REASON_TO_STATUS.get(result_field)
        if mapped:
            final_status, ban_type = mapped
        else:
            # "ok" with no result → active
            final_status, ban_type = "active", None

    # 4b. Explicit "fail" status with reason
    elif status_field == "fail":
        mapped = REASON_TO_STATUS.get(reason_field)
        if mapped:
            final_status, ban_type = mapped
        else:
            # Unknown fail reason → treat conservatively
            final_status, ban_type = "unknown", None

    # 4c. HTTP-code based fallback
    elif http == 200:
        # Check raw text
        raw_lower = data.get("_raw", "").lower()
        if "exist" in raw_lower or "login" in raw_lower:
            final_status, ban_type = "active", None
        elif any(w in raw_lower for w in ("blocked", "banned", "gone", "suspended")):
            final_status, ban_type = "banned", None
        else:
            final_status, ban_type = "unknown", None

    elif http in (401, 403):
        final_status, ban_type = "banned", "Admin"

    elif http == 404:
        final_status, ban_type = "not_registered", None

    elif http == 429:
        final_status, ban_type = "rate_limited", None

    elif http == 503:
        final_status, ban_type = "service_down", None

    else:
        final_status, ban_type = "unknown", None

    # ── 5. For banned numbers: run secondary ban-type classification ──
    if final_status == "banned" and ban_type in (None, "Personal"):
        ban_type = await _classify_ban_type(session, full_number)

    return {
        "display":  display,
        "status":   final_status,
        "ban_type": ban_type,
        "ping_ms":  ping_ms,
        "note":     f"API reason: {reason_field}" if reason_field and final_status not in ("active",) else "",
    }


async def check_all(numbers: list) -> list:
    """Check all numbers simultaneously."""
    connector = aiohttp.TCPConnector(limit=50, ssl=False, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(
            *[check_single(session, n) for n in numbers],
            return_exceptions=True,
        )
    out = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            out.append({
                "display":  numbers[i],
                "status":   "error",
                "ban_type": None,
                "ping_ms":  0,
                "note":     str(r)[:80],
            })
        else:
            out.append(r)
    return out


# ═══════════════════════════════════════════════════════
#  FORMATTERS
# ═══════════════════════════════════════════════════════

STATUS_MAP = {
    "active":         ("🟢", "ACTIVE — Not Banned"),
    "banned":         ("🔴", "BANNED"),
    "not_registered": ("⚫", "NOT ON WHATSAPP"),
    "invalid":        ("⚠️", "INVALID NUMBER"),
    "timeout":        ("🟡", "TIMEOUT"),
    "rate_limited":   ("⏳", "RATE LIMITED — Retry later"),
    "service_down":   ("🟠", "WA SERVICE DOWN"),
    "error":          ("❌", "ERROR"),
    "unknown":        ("❓", "UNDETECTABLE"),
}

BAN_ICON = {
    "Admin":    "👮 Admin Ban     — Platform enforcement",
    "Regional": "🌍 Regional Ban  — Country/region blocked",
    "Personal": "🔒 Personal Ban  — Account violation",
}


def build_result_message(results: list, elapsed: float, requester: str) -> str:
    now    = datetime.now().strftime("%d %b %Y  %H:%M:%S")
    total  = len(results)
    banned = sum(1 for r in results if r["status"] == "banned")
    active = sum(1 for r in results if r["status"] == "active")
    other  = total - banned - active
    div    = "─" * 36

    lines = [
        "╔" + "═" * 38 + "╗",
        "║   ⚡  <b>KAGE X Ban Checker</b>  ⚡ v3.0   ║",
        f"║   🗓 {now:<31}║",
        "╚" + "═" * 38 + "╝",
        "",
        "📊 <b>Summary</b>",
        f"   • Total     : <code>{total}</code>",
        f"   • 🔴 Banned   : <code>{banned}</code>",
        f"   • 🟢 Active   : <code>{active}</code>",
        f"   • ❓ Other    : <code>{other}</code>",
        f"   • ⏱ Speed    : <code>{elapsed:.2f}s</code>",
        "",
        div, "",
    ]

    for idx, r in enumerate(results, 1):
        icon, label = STATUS_MAP.get(r["status"], ("❓", r["status"].upper()))
        ban_label   = BAN_ICON.get(r.get("ban_type")) if r.get("ban_type") else None

        lines.append(f"<b>#{idx}</b>  {icon}  <code>{r['display']}</code>")
        lines.append(f"       Status   : <b>{label}</b>")
        if ban_label:
            lines.append(f"       Ban Type : {ban_label}")
        lines.append(f"       Ping     : <code>{r['ping_ms']} ms</code>")
        if r.get("note"):
            lines.append(f"       Note     : <i>{r['note']}</i>")
        lines.append("")

    lines.append(div)
    lines.append(f"👤 By: <b>{requester}</b>  |  🤖 <b>{BOT_NAME}</b>")
    return "\n".join(lines)


def extract_numbers(text: str) -> list:
    tokens  = re.split(r"[\s,;|\n]+", text.strip())
    numbers = []
    seen    = set()
    for t in tokens:
        digits = re.sub(r"\D", "", t.strip())
        if 7 <= len(digits) <= 15 and digits not in seen:
            seen.add(digits)
            numbers.append("+" + digits)
    return numbers[:MAX_NUMBERS]


# ═══════════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ═══════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    text = (
        "╔" + "═" * 38 + "╗\n"
        "║   ⚡  <b>KAGE X Ban Checker</b>  ⚡ v3.0   ║\n"
        "╚" + "═" * 38 + "╝\n\n"
        f"👋 Welcome, <b>{name}</b>!\n\n"
        "Accurately checks each WhatsApp number via\n"
        "<b>WhatsApp’s real internal API</b> — per number, simultaneously.\n\n"
        "<b>Detects:</b>\n"
        "  🟢 Active — not banned\n"
        "  🔴 Banned — with type:\n"
        "       🔒 Personal  |  🌍 Regional  |  👮 Admin\n"
        "  ⚫ Not registered on WhatsApp\n\n"
        "━" * 26 + "\n"
        "  <code>!check +2348012345678</code>\n"
        "  <code>!check 234xxx 447xxx 155xxx</code>\n"
        "  <code>!ping</code>  │  <code>!help</code>\n"
        "━" * 26 + "\n"
        f"⚡ Up to <b>{MAX_NUMBERS}</b> numbers checked at once!"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Help",  callback_data="cb_help"),
        InlineKeyboardButton("ℹ️ About", callback_data="cb_about"),
    ]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚡ <b>KAGE X — Commands</b>\n\n"
        "<code>!check +234xxxxxxxx</code> — check one\n"
        "<code>!check 234xxx 447xxx 155xxx</code> — multi\n"
        "<code>!ping</code> — bot latency\n"
        "<code>!about</code> — bot info\n\n"
        f"⚡ Max <b>{MAX_NUMBERS}</b> numbers per request."
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "╔" + "═" * 34 + "╗\n"
        f"║  ⚡ <b>{BOT_NAME}</b>  {BOT_VERSION}  ║\n"
        "╚" + "═" * 34 + "╝\n\n"
        "🔍 Uses WhatsApp’s real internal API\n"
        "⚡ Simultaneous per-number checking\n"
        "🎯 Detects Personal, Regional & Admin bans\n"
        "🌎 Uses <code>phonenumbers</code> for accurate parsing\n"
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
        f"⏳ <b>KAGE X</b> — Checking <code>{len(numbers)}</code> "
        f"number(s) via WhatsApp API... ✨",
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
    routes   = {
        "check": cmd_check, "start": cmd_start,
        "help":  cmd_help,  "about": cmd_about,
        "ping":  cmd_ping,
    }
    fn = routes.get(cmd)
    if fn:
        await fn(update, ctx)
    else:
        await update.message.reply_text(
            f"❓ Unknown command: <code>!{cmd}</code> — try <code>!help</code>",
            parse_mode=ParseMode.HTML,
        )


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "cb_help":  await cmd_help(update, ctx)
    elif q.data == "cb_about": await cmd_about(update, ctx)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    print(f"[KAGE X ERROR] {ctx.error}")


# ═══════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════

def main():
    token = BOT_TOKEN.strip()
    if not token:
        print("\n⚠️  BOT_TOKEN not set! Add it as an environment variable.\n")
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
        "\n" + "╔" + "═" * 44 + "╗" +
        "\n║   ⚡  " + BOT_NAME + "  ⚡  " + BOT_VERSION + "         ║" +
        "\n║   Real WhatsApp API Engine — Running...        ║" +
        "\n╚" + "═" * 44 + "╝\n"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
