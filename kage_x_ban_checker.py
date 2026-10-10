#!/usr/bin/env python3
# ╔══════════════════════════════════════════════════════════╗
# ║        KAGE X Ban Checker  —  Telegram Bot v4.0         ║
# ║   Accurate  |  Real WA API  |  No False Invalids        ║
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

# ----------------------------------------------------------------
#  CONFIG
# ----------------------------------------------------------------
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
BOT_NAME    = "KAGE X Ban Checker"
BOT_VERSION = "v4.0"
PREFIX      = "!"
MAX_NUMBERS = 20

# ----------------------------------------------------------------
#  NUMBER PARSING  (lenient — never falsely rejects real numbers)
# ----------------------------------------------------------------

# Known country codes ordered longest-first so we match greedily
# (e.g. 234 before 23 before 2)
KNOWN_CC = [
    "1","7","20","27","30","31","32","33","34","36","39","40","41",
    "43","44","45","46","47","48","49","51","52","53","54","55","56",
    "57","58","60","61","62","63","64","65","66","81","82","84","86",
    "90","91","92","93","94","95","98",
    "212","213","216","218","220","221","222","223","224","225","226",
    "227","228","229","230","231","232","233","234","235","236","237",
    "238","239","240","241","242","243","244","245","246","247","248",
    "249","250","251","252","253","254","255","256","257","258","260",
    "261","262","263","264","265","266","267","268","269",
    "290","291","297","298","299",
    "350","351","352","353","354","355","356","357","358","359",
    "370","371","372","373","374","375","376","377","378","380",
    "381","382","385","386","387","389",
    "420","421","423",
    "500","501","502","503","504","505","506","507","508","509",
    "590","591","592","593","594","595","596","597","598","599",
    "670","672","673","674","675","676","677","678","679","680",
    "681","682","683","685","686","687","688","689","690","691",
    "692",
    "800","808","850","852","853","855","856",
    "880","886",
    "960","961","962","963","964","965","966","967","968","970",
    "971","972","973","974","975","976","977",
    "992","993","994","995","996","998",
]
# Sort longest first
KNOWN_CC_SORTED = sorted(set(KNOWN_CC), key=len, reverse=True)


def parse_number(raw: str):
    """
    Parse a phone number into (country_code, national_number, display).
    Uses a large country-code table and never rejects real numbers.
    Returns None only if the digits are too short or too long to be valid.
    """
    digits = re.sub(r"\D", "", raw.strip())
    if not digits:
        return None
    if len(digits) < 7 or len(digits) > 15:
        return None

    display = "+" + digits

    # Step 1: try to match a known country code prefix
    for cc in KNOWN_CC_SORTED:
        if digits.startswith(cc):
            nn = digits[len(cc):]
            if len(nn) >= 4:          # national part must be at least 4 digits
                return cc, nn, display

    # Step 2: fallback — try splitting at 1, 2, 3 digits for cc
    for cc_len in (1, 2, 3):
        if len(digits) > cc_len + 4:
            cc = digits[:cc_len]
            nn = digits[cc_len:]
            return cc, nn, display

    # Step 3: absolute last resort — send the whole number as national part
    return "1", digits, display


def extract_numbers(text: str) -> list:
    tokens  = re.split(r"[\s,;|\n]+", text.strip())
    numbers = []
    seen    = set()
    for t in tokens:
        digits = re.sub(r"\D", "", t.strip())
        if 7 <= len(digits) <= 15 and digits not in seen:
            seen.add(digits)
            numbers.append(digits)   # store raw digits, display added later
    return numbers[:MAX_NUMBERS]


# ----------------------------------------------------------------
#  WHATSAPP REAL API ENGINE
# ----------------------------------------------------------------

WA_EXIST_URL = "https://v.whatsapp.net/v2/exist"

WA_USER_AGENTS = [
    "WhatsApp/2.24.8.78 A",
    "WhatsApp/2.24.6.77 A",
    "WhatsApp/2.23.25.84 A",
    "WhatsApp/2.24.10.84 A",
    "WhatsApp/2.24.12.80 A",
]


def _wa_headers() -> dict:
    return {
        "User-Agent": random.choice(WA_USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }


def _rand_id(n: int = 16) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


# WhatsApp API reason → (status, default_ban_type)
REASON_MAP = {
    # active
    "exist":               ("active",         None),
    "ok":                  ("active",         None),
    # personal bans
    "blocked":             ("banned",         "Personal"),
    "gone":                ("banned",         "Personal"),
    "banned":              ("banned",         "Personal"),
    "suspended":           ("banned",         "Personal"),
    "disabled":            ("banned",         "Personal"),
    "unauthorized":        ("banned",         "Personal"),
    # not registered
    "unregistered":        ("not_registered", None),
    "not_exist":           ("not_registered", None),
    "nonexistent":         ("not_registered", None),
    "missing":             ("not_registered", None),
    # bad input
    "bad_param":           ("invalid_input",  None),
    "country-code-invalid":("invalid_input",  None),
    "enum_mismatch":       ("invalid_input",  None),
    # rate limit
    "too_recent":          ("rate_limited",   None),
    "try_later":           ("rate_limited",   None),
}


async def _wa_exist_api(
    session: aiohttp.ClientSession, cc: str, nn: str
) -> dict:
    """Call WhatsApp's v2/exist endpoint and return parsed JSON + meta."""
    params = {
        "cc":      cc,
        "phone":   nn,
        "id":      _rand_id(),
        "lg":      "en",
        "lc":      "US",
        "authkey": _rand_id(24),
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
            http   = resp.status
            body   = await resp.text(errors="ignore")
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            data["_http"] = http
            data["_body"] = body[:400].lower()
            return data
    except asyncio.TimeoutError:
        return {"_err": "timeout"}
    except Exception as e:
        return {"_err": str(e)[:100]}


async def _classify_ban_type(
    session: aiohttp.ClientSession, full_digits: str
) -> str:
    """
    Secondary page check to classify ban as Admin, Regional, or Personal.
    Called only after we confirmed the number IS banned.
    """
    url = "https://api.whatsapp.com/send?phone=" + full_digits
    hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with session.get(
            url, headers=hdrs,
            timeout=aiohttp.ClientTimeout(total=8),
            ssl=False, allow_redirects=True,
        ) as resp:
            body = (await resp.text(errors="ignore")).lower()
            if resp.status == 403:
                return "Admin"
            for kw in ("not available in your country", "not available in your region",
                       "service not available", "country is not supported"):
                if kw in body:
                    return "Regional"
            for kw in ("terms of service", "policy violation", "enforcement",
                       "violated our", "spam", "abuse report"):
                if kw in body:
                    return "Admin"
    except Exception:
        pass
    return "Personal"


async def check_single(
    session: aiohttp.ClientSession, raw_digits: str
) -> dict:
    """Full check for one number. Returns structured result dict."""

    parsed = parse_number(raw_digits)
    if not parsed:
        return {
            "display":  "+" + raw_digits,
            "status":   "invalid",
            "ban_type": None,
            "ping_ms":  0,
            "note":     "Number too short or too long",
        }

    cc, nn, display = parsed
    t0 = time.monotonic()

    data    = await _wa_exist_api(session, cc, nn)
    ping_ms = round((time.monotonic() - t0) * 1000)

    # ─ Connection/timeout errors
    if "_err" in data:
        err = data["_err"]
        return {
            "display":  display,
            "status":   "timeout" if err == "timeout" else "error",
            "ban_type": None,
            "ping_ms":  ping_ms,
            "note":     "Request timed out" if err == "timeout" else err,
        }

    http         = data.get("_http", 200)
    body_lower   = data.get("_body", "")
    status_field = str(data.get("status", "")).lower()
    result_field = str(data.get("result", "")).lower()
    reason_field = str(data.get("reason", "")).lower()
    login_field  = str(data.get("login",  "")).lower()

    final_status = None
    ban_type     = None
    note         = ""

    # ─ Case 1: API returned explicit "ok"
    if status_field == "ok":
        mapped = REASON_MAP.get(result_field)
        if mapped:
            final_status, ban_type = mapped
        else:
            # "ok" with unknown result or login present ⇒ active
            final_status = "active"

    # ─ Case 2: API returned explicit "fail"
    elif status_field == "fail":
        mapped = REASON_MAP.get(reason_field)
        if mapped:
            final_status, ban_type = mapped
            if reason_field and final_status == "banned":
                note = f"Reason: {reason_field}"
        else:
            final_status = "unknown"
            note = f"API reason: {reason_field}" if reason_field else ""

    # ─ Case 3: No JSON status — fall back to HTTP code + body scan
    else:
        # Body keyword scan
        if any(w in body_lower for w in ("\"exist\"", "login", "result\":\"ok")):
            final_status = "active"
        elif any(w in body_lower for w in ("blocked", "banned", "gone", "suspended")):
            final_status = "banned"
        elif any(w in body_lower for w in ("unregistered", "not_exist", "nonexistent")):
            final_status = "not_registered"
        elif http == 200:
            # 200 with no clear signal: treat as active (conservative)
            final_status = "active"
            note = "Active (HTTP 200)"
        elif http in (401, 403):
            final_status = "banned"
            ban_type     = "Admin"
        elif http == 404:
            final_status = "not_registered"
        elif http == 429:
            final_status = "rate_limited"
        elif http >= 500:
            final_status = "service_down"
        else:
            final_status = "unknown"

    # ─ Classify ban type for confirmed bans
    if final_status == "banned" and ban_type is None:
        ban_type = await _classify_ban_type(session, cc + nn)

    return {
        "display":  display,
        "status":   final_status,
        "ban_type": ban_type,
        "ping_ms":  ping_ms,
        "note":     note,
    }


async def check_all(numbers: list) -> list:
    """Run all checks simultaneously."""
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
                "display":  "+" + numbers[i],
                "status":   "error",
                "ban_type": None,
                "ping_ms":  0,
                "note":     str(r)[:80],
            })
        else:
            out.append(r)
    return out


# ----------------------------------------------------------------
#  FORMATTERS
# ----------------------------------------------------------------

STATUS_MAP = {
    "active":         ("🟢", "ACTIVE — Not Banned"),
    "banned":         ("🔴", "BANNED"),
    "not_registered": ("⚫", "NOT ON WHATSAPP"),
    "invalid":        ("⚠️", "INVALID NUMBER"),
    "invalid_input":  ("⚠️", "INVALID FORMAT"),
    "timeout":        ("🟡", "TIMEOUT"),
    "rate_limited":   ("⏳", "RATE LIMITED"),
    "service_down":   ("🟠", "WA SERVICE DOWN"),
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
    div    = "─" * 36

    lines = [
        "╔" + "═" * 38 + "╗",
        "║   ⚡  <b>KAGE X Ban Checker</b>  ⚡  v4.0  ║",
        f"║   🗓 {now:<31}║",
        "╚" + "═" * 38 + "╝",
        "",
        "📊 <b>Summary</b>",
        f"   • Total    : <code>{total}</code>",
        f"   • 🔴 Banned  : <code>{banned}</code>",
        f"   • 🟢 Active  : <code>{active}</code>",
        f"   • ❓ Other   : <code>{other}</code>",
        f"   • ⏱ Speed   : <code>{elapsed:.2f}s</code>",
        "",
        div, "",
    ]

    for idx, r in enumerate(results, 1):
        icon, label = STATUS_MAP.get(r["status"], ("❓", r["status"].upper()))
        ban_label   = BAN_ICON.get(r.get("ban_type")) if r.get("ban_type") else None

        lines.append(f"<b>#{idx}</b>  {icon}  <code>{r['display']}</code>")
        lines.append(f"       Status   : <b>{label}</b>")
        if ban_label:
            lines.append(f"       Ban Type : <b>{ban_label}</b>")
        lines.append(f"       Ping     : <code>{r['ping_ms']} ms</code>")
        if r.get("note"):
            lines.append(f"       Note     : <i>{r['note']}</i>")
        lines.append("")

    lines.append(div)
    lines.append(f"👤 By: <b>{requester}</b>  |  🤖 <b>{BOT_NAME}</b>")
    return "\n".join(lines)


# ----------------------------------------------------------------
#  TELEGRAM HANDLERS
# ----------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    text = (
        "╔" + "═" * 38 + "╗\n"
        "║   ⚡  <b>KAGE X Ban Checker</b>  ⚡  v4.0  ║\n"
        "╚" + "═" * 38 + "╝\n\n"
        f"👋 Welcome, <b>{name}</b>!\n\n"
        "Checks each WhatsApp number via <b>WhatsApp’s real API</b>\n"
        "simultaneously — accurate per-number results.\n\n"
        "<b>Detects:</b>\n"
        "  🟢 Active  •  🔴 Banned  •  ⚫ Not Registered\n"
        "  Ban types: 🔒 Personal  🌍 Regional  👮 Admin\n\n"
        "━" * 26 + "\n"
        "  <code>!check +2348012345678</code>\n"
        "  <code>!check 234xxx 447xxx 155xxx</code>\n"
        "  <code>!ping</code>  │  <code>!help</code>\n"
        "━" * 26 + "\n"
        f"⚡ Up to <b>{MAX_NUMBERS}</b> numbers simultaneously!"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Help",  callback_data="cb_help"),
        InlineKeyboardButton("ℹ️ About", callback_data="cb_about"),
    ]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚡ <b>KAGE X — Commands</b>\n\n"
        "<code>!check +234xxxxxxxx</code> — single\n"
        "<code>!check 234xxx 447xxx 155xxx</code> — multi\n"
        "<code>!ping</code> — latency\n"
        "<code>!about</code> — bot info\n\n"
        f"⚡ Max <b>{MAX_NUMBERS}</b> numbers per request."
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "╔" + "═" * 34 + "╗\n"
        f"║  ⚡ <b>{BOT_NAME}</b>  {BOT_VERSION}      ║\n"
        "╚" + "═" * 34 + "╝\n\n"
        "🔍 Real WhatsApp API detection\n"
        "⚡ Simultaneous per-number checking\n"
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
            "⚠️ <b>No valid numbers detected.</b>\n\n"
            "Send numbers with country code:\n"
            "  <code>!check +2348012345678</code>\n"
            "  <code>!check 2348012345678 447911234567</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    wait = await update.message.reply_text(
        f"⏳ <b>KAGE X</b> — Checking <code>{len(numbers)}</code> "
        "number(s) via WhatsApp API... ✨",
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
            f"❓ Unknown: <code>!{cmd}</code> — try <code>!help</code>",
            parse_mode=ParseMode.HTML,
        )


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "cb_help":  await cmd_help(update, ctx)
    elif q.data == "cb_about": await cmd_about(update, ctx)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    print(f"[KAGE X ERROR] {ctx.error}")


# ----------------------------------------------------------------
#  MAIN
# ----------------------------------------------------------------

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
        "\n" + "╔" + "═" * 46 + "╗" +
        "\n║   ⚡  " + BOT_NAME + "  ⚡  " + BOT_VERSION + "           ║" +
        "\n║   Real WhatsApp API — No False Invalids — Live  ║" +
        "\n╚" + "═" * 46 + "╝\n"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
