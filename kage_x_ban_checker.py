#!/usr/bin/env python3
"""
KAGE X Ban Checker  v8.0
Primary: baron0.com BanCheck API (POST /api/v2/check, Bearer auth)
Fallback: 5-method engine if API is down/rate-limited
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

# ════════════════════════════════════════════════
#  CONFIG  —  set BOT_TOKEN and optionally BANCHECK_KEY as Railway env vars
# ════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_NAME  = "KAGE X Ban Checker"
BOT_VER   = "v8.0"
PREFIX    = "!"
MAX_NUM   = 20

# BanCheck API — baron0.com
BANCHECK_KEY = os.getenv(
    "BANCHECK_KEY",
    "bk_v1_jLzHajfQ2_vMitdCmKPZh1b9Mg1iQmj3WTKs3fwEFwScIMnGaP1VtH3tnGEp"
    "d4znob40m0AYcHZV-TZZ2_RbHg2XQxNPaKXRSxYIZwpZagaF6DclHfbSNgY0ZPzl61"
    "r5XwvURqN9H0k0AWKP4miv9cnV4NTkVcagrcUGssscMEyo95BNWSDL-ima_7Y2kvN32f"
    "-_vVkIcwkv4hjzV3X79sgZoKYh7Y_cpsfTCMp9r1yEwdcQYyuimjwORVjTNdFOjRSxr"
    "U3oMMkYfl5ICt06bvi8wwGk3VHJGN7UntgqRAT_0AIUgKD4OhbP6IcU-EJyBraQPXPG"
    "oJtc2qt8XlbaCkZhUqc7NhdoqH0EY6F9stC3z0kOoztmoweatG5EdF6yJ-zGLKmqRF0"
    "ML2xS6Nkvs9_JZdAqLkCpAFICfPuyZ9b2hv0h8jmfIo0r2zTVbndu0gyHsFoXQ_zCh0"
    "AWZwe6LaRXsCz8rsmA_VpilvBegFx9iUsRrGrfcXFKPZ364qEpiU0R3p-a1-9zyGYdiv"
    "G6PkZN8gLZakTSt_wMNupOBg9n47FWeQ9SqYxrCz5KXO3TLlRZeSYP"
)
BARON_URL   = "https://baron0.com/api/v2/check"  # confirmed endpoint
OWNER_ID    = int(os.getenv("OWNER_ID", "0"))   # set to YOUR Telegram user ID in Railway
BOT_BANNER   = os.getenv("BOT_BANNER", "")  # optional env-var fallback
BANNER_FILE  = "banner.txt"                 # persists banner file_id across restarts

# ── Runtime banner state (updated by !setbanner without restart) ──────────────
def _load_banner() -> str:
    try:
        txt = open(BANNER_FILE).read().strip()
        if txt: return txt
    except FileNotFoundError:
        pass
    return BOT_BANNER

_banner: list = [_load_banner()]   # list so inner functions can mutate it

def get_banner() -> str:  return _banner[0]
def set_banner(fid: str) -> None:
    _banner[0] = fid
    try:
        open(BANNER_FILE, "w").write(fid)
    except Exception as e:
        print(f"[BANNER SAVE ERROR] {e}")


# ════════════════════════════════════════════════
#  NUMBER UTILITIES
# ════════════════════════════════════════════════

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


CAPTION_LIMIT = 950  # Telegram caption hard-limit is 1024; stay safe

async def send_with_banner(target, text: str,
                           parse_mode=ParseMode.HTML,
                           reply_markup=None) -> None:
    """
    Send image + text in ONE message bubble (photo+caption), exactly like Sanctum.
    Falls back gracefully: if no banner just sends text; if text too long sends
    photo first then text separately.
    """
    fid  = get_banner()
    kw   = {"parse_mode": parse_mode}
    if reply_markup:
        kw["reply_markup"] = reply_markup

    # Normalize: strip any existing blockquote tags, re-apply consistently
    clean = text.replace("<blockquote>", "").replace("</blockquote>", "").strip()
    quoted = f"<blockquote>{clean}</blockquote>"  # for plain-text sends

    if not fid:
        # No banner — send as quoted text
        await target.reply_text(quoted, **kw)
        return

    # Caption path: <blockquote> not supported, use clean text
    if len(clean) <= CAPTION_LIMIT:
        try:
            await target.reply_photo(photo=fid, caption=clean, **kw)
            return
        except Exception as e:
            print(f"[BANNER+CAPTION ERROR] {type(e).__name__}: {e}")

    # Caption too long or photo failed — photo then quoted text
    try:
        await target.reply_photo(fid)
    except Exception as e:
        print(f"[BANNER ERROR] {type(e).__name__}: {e}")
    await target.reply_text(quoted, **kw)


async def cmd_setbanner(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """!setbanner — owner-only: send as photo caption to set the banner image."""
    msg = u.message or u.effective_message
    # Block non-owners
    if OWNER_ID and u.effective_user.id != OWNER_ID:
        await msg.reply_text(
            "🚫 <b>Owner only.</b>",
            parse_mode=ParseMode.HTML,
        )
        return
    # Called with no photo (text-only !setbanner) → show instructions
    if not u.message or not u.message.photo:
        await (msg or u.effective_message).reply_text(
            "📸 <b>How to set your KAGE X banner:</b>\n\n"
            "1️⃣  Tap the 📎 attachment icon\n"
            "2️⃣  Pick your image\n"
            "3️⃣  <b>Type as caption:</b> <code>!setbanner</code>\n"
            "4️⃣  Send it to this bot\n\n"
            "✅ Done — banner will show on every command instantly.",
            parse_mode=ParseMode.HTML,
        )
        return
    # Photo received — grab highest-quality variant
    fid = msg.photo[-1].file_id
    set_banner(fid)
    print(f"[BANNER SET] file_id={fid}")
    await msg.reply_text(
        "✅ <b>Banner saved!</b>\n\n"
        "Your image will now appear before every command.\n"
        f"<i>file_id: <code>{fid[:40]}...</code></i>",
        parse_mode=ParseMode.HTML,
    )


def mask_number(display: str) -> str:
    """Mask middle digits for privacy: +2348078443988 → +234*****988"""
    digits = display.lstrip('+')
    if len(digits) <= 6:
        return display
    keep_start = max(4, len(digits) - 9)
    keep_end   = 3
    n_stars    = len(digits) - keep_start - keep_end
    if n_stars < 2:
        return display
    return f"+{digits[:keep_start]}{'*' * n_stars}{digits[-keep_end:]}"


# ════════════════════════════════════════════════
#  BARON0 BANCHECK API  —  PRIMARY
# ════════════════════════════════════════════════

def _ban_type_from_reason(reason: str) -> str:
    """
    Map baron0 reason string to a display ban type.
    The API returns things like 'spam', 'policy', 'regional', 'admin', etc.
    """
    r = (reason or "").lower()
    if any(w in r for w in ("admin", "platform", "enforcement", "operator")):
        return "Admin"
    if any(w in r for w in ("regional", "country", "region", "geo", "location")):
        return "Regional"
    # Default personal
    return "Personal"


async def baron0_check(session: aiohttp.ClientSession, digits: str) -> dict | None:
    """
    POST https://baron0.com/api/v2/check
    Body : {"number": "+{digits}"}
    Auth : Bearer {BANCHECK_KEY}

    Response shape (from bot.js reference):
      { "banned": bool, "reason": str | null, ... }

    Returns standardised dict or None on failure.
    """
    number = "+" + digits  # always send E.164 format with leading +
    headers = {
        "Authorization": f"Bearer {BANCHECK_KEY}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
        "User-Agent":    "KAGEX-BanChecker/8.0",
    }
    payload = {"number": number}

    try:
        async with session.post(
            BARON_URL,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=12),
            ssl=False,
        ) as r:
            http = r.status
            try:
                data = await r.json(content_type=None)
            except Exception:
                raw = await r.text(errors="ignore")
                try:
                    data = json.loads(raw)
                except Exception:
                    data = {"_raw": raw[:200]}

            # Log for debugging (Railway logs)
            print(f"[Baron0] {number} → HTTP {http} | {json.dumps(data)[:120]}")

            if http == 401:
                print("[Baron0] 401 — API key rejected.")
                return None
            if http == 429:
                return {"status": "rate_limited", "ban_type": None,
                        "note": "baron0 rate limit — retry shortly"}
            if http not in (200, 201, 202):
                return None  # let fallback engine handle it

            # ── Parse response ─────────────────────────────────────
            banned  = data.get("banned")
            reason  = str(data.get("reason")  or data.get("ban_reason")  or
                          data.get("type")     or "").strip()
            reg     = data.get("registered")  # may or may not be present
            status  = str(data.get("status")  or "").lower()
            exists  = data.get("exists")       # some APIs return this

            # Not on WhatsApp at all
            if reg is False or exists is False:
                return {"status": "not_registered", "ban_type": None,
                        "note": "Not registered on WhatsApp"}
            if status in ("not_registered", "unregistered", "not_exist",
                          "nonexistent", "not_found"):
                return {"status": "not_registered", "ban_type": None, "note": ""}

            # Banned
            if banned is True or banned == 1:
                bt = _ban_type_from_reason(reason)
                note = reason if reason and reason.lower() not in\
                       ("none", "null", "n/a", "unknown") else ""
                return {"status": "banned", "ban_type": bt, "note": note}

            # Explicitly not banned
            if banned is False or banned == 0:
                return {"status": "active", "ban_type": None, "note": ""}

            # Ambiguous — try status field
            if status in ("active", "clean", "not_banned", "ok", "safe"):
                return {"status": "active", "ban_type": None, "note": ""}
            if status in ("banned", "blocked", "suspended"):
                bt = _ban_type_from_reason(reason)
                return {"status": "banned", "ban_type": bt, "note": reason}

            # Response received but neither true nor false — log and fall through
            print(f"[Baron0] Unrecognised response shape: {data}")
            return None

    except asyncio.TimeoutError:
        print(f"[Baron0] Timeout for {number}")
        return None
    except Exception as e:
        print(f"[Baron0] Error for {number}: {e}")
        return None


# ════════════════════════════════════════════════
#  5-METHOD FALLBACK ENGINE
# ════════════════════════════════════════════════

WA_UAS = [
    "WhatsApp/2.24.10.84 A", "WhatsApp/2.24.8.78 A",
    "WhatsApp/2.24.6.77 A",  "WhatsApp/2.23.25.84 A",
]
BROWSER_UAS = [
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def _rid(n=16):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


async def _get_page(session, url, *, params=None, headers=None, timeout=9):
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


_ACT = ["send message","open whatsapp","continue to chat","click to chat",
        "whatsapp://send","\"phone\":","phonenumber","send_message"]
_BAN = ["this account has been banned","account has been banned",
        "number is banned","phone number is banned",
        "banned from whatsapp","banned for violating"]
_NRG = ["phone number shared via url is invalid","this link is not valid",
        "not on whatsapp","number is not registered","invalid phone number",
        "no whatsapp account","doesn't have whatsapp"]


def _scan(body):
    for kw in _ACT: 
        if kw in body: return "active"
    for kw in _BAN: 
        if kw in body: return "banned"
    for kw in _NRG: 
        if kw in body: return "not_registered"
    return None


async def _fb_exist(session, cc, nn):
    params  = {"cc": cc, "phone": nn, "id": _rid(), "lg": "en", "lc": "US"}
    headers = {"User-Agent": random.choice(WA_UAS), "Accept": "*/*"}
    code, body, _ = await _get_page(
        session, "https://v.whatsapp.net/v2/exist",
        params=params, headers=headers,
    )
    if code < 0: return None
    try:
        d  = json.loads(body)
        s  = str(d.get("status", "")).lower()
        rs = str(d.get("result", "")).lower()
        rn = str(d.get("reason", "")).lower()
        if s == "ok" or rs in ("exist", "ok"): return "active"
        if rn in ("blocked","banned","gone","suspended","disabled"): return "banned"
        if rn in ("unregistered","not_exist","nonexistent"): return "not_registered"
        if code in (401, 403): return "banned"
        if code == 404: return "not_registered"
    except Exception:
        pass
    return _scan(body)


async def _fb_wame(session, digits):
    h = {"User-Agent": random.choice(BROWSER_UAS[:2]),
         "Accept": "text/html,*/*", "Accept-Language": "en-US,en;q=0.9"}
    code, body, url = await _get_page(
        session, f"{{https://wa.me/{digits}}}", headers=h
    )
    if code < 0: return None
    if "whatsapp://" in url or "intent://" in url: return "active"
    return _scan(body)


async def _fb_api_send(session, digits):
    h = {"User-Agent": random.choice(BROWSER_UAS),
         "Accept": "text/html,*/*", "Accept-Language": "en-US,en;q=0.9"}
    code, body, _ = await _get_page(
        session, "https://api.whatsapp.com/send",
        params={"phone": digits}, headers=h,
    )
    if code < 0: return None
    if code == 403: return "banned"
    return _scan(body)


async def _fb_click(session, digits):
    h = {"User-Agent": random.choice(BROWSER_UAS[:2]),
         "Accept": "text/html,*/*"}
    code, body, url = await _get_page(
        session, "https://click.whatsapp.com/send",
        params={"phone": digits}, headers=h,
    )
    if code < 0: return None
    if "whatsapp://" in url: return "active"
    return _scan(body)


async def _fb_redirect(session, digits):
    h = {"User-Agent": random.choice(BROWSER_UAS[:2]),
         "Accept": "text/html,*/*"}
    try:
        async with session.get(
            f"{{https://wa.me/{digits}}}", headers=h,
            timeout=aiohttp.ClientTimeout(total=8),
            ssl=False, allow_redirects=False,
        ) as r:
            loc = r.headers.get("Location", "").lower()
            if "send" in loc or "whatsapp" in loc: return "active"
            if "invalid" in loc or "error" in loc: return "not_registered"
    except Exception:
        pass
    return None


async def _fb_ban_type(session, digits):
    h = {"User-Agent": random.choice(BROWSER_UAS),
         "Accept-Language": "en-US,en;q=0.9"}
    code, body, _ = await _get_page(
        session, "https://api.whatsapp.com/send",
        params={"phone": digits}, headers=h,
    )
    if code == 403: return "Admin"
    for kw in ["not available in your country","not available in your region",
                "country is not supported"]:
        if kw in body: return "Regional"
    for kw in ["terms of service","policy violation","violated our",
                "enforcement","spam","abuse"]:
        if kw in body: return "Admin"
    return "Personal"


async def fallback_check(session, digits: str) -> dict:
    cc, nn = split_cc(digits)
    sigs   = []
    results = await asyncio.gather(
        _fb_exist(session, cc, nn),
        _fb_wame(session, digits),
        _fb_api_send(session, digits),
        _fb_click(session, digits),
        _fb_redirect(session, digits),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, str): sigs.append(r)
    counts = {k: sigs.count(k) for k in ("active","banned","not_registered")}

    if counts["active"] >= 1:
        return {"status": "active",         "ban_type": None, "note": "(fallback)"}
    if counts["banned"] >= 1:
        bt = await _fb_ban_type(session, digits)
        return {"status": "banned",          "ban_type": bt,   "note": "(fallback)"}
    if counts["not_registered"] >= 1:
        return {"status": "not_registered", "ban_type": None, "note": "(fallback)"}
    if not sigs:
        return {"status": "server_blocked",  "ban_type": None,
                "note": "WA IP block — baron0 API is primary"}
    return {"status": "unknown", "ban_type": None,
            "note": f"{len(sigs)}/5 weak signals"}


# ════════════════════════════════════════════════
#  MASTER CHECK
# ════════════════════════════════════════════════

async def check_one(session: aiohttp.ClientSession, digits: str) -> dict:
    display = "+" + digits
    t0      = time.monotonic()

    # 1. Baron0 API (primary — real ban data)
    api = await baron0_check(session, digits)
    ping_ms = round((time.monotonic() - t0) * 1000)

    if api is not None:
        return {
            "display":  display,
            "status":   api["status"],
            "ban_type": api.get("ban_type"),
            "ping_ms":  ping_ms,
            "note":     api.get("note", ""),
            "source":   "⚡ KAGE X API",
        }

    # 2. Fallback engine
    fb = await fallback_check(session, digits)
    ping_ms = round((time.monotonic() - t0) * 1000)
    return {
        "display":  display,
        "status":   fb["status"],
        "ban_type": fb.get("ban_type"),
        "ping_ms":  ping_ms,
        "note":     fb.get("note", ""),
        "source":   "🔄 KAGE X Engine",
    }


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
            out.append({"display": "+" + numbers[i], "status": "error",
                        "ban_type": None, "ping_ms": 0,
                        "note": str(r)[:80], "source": "❌"})
        else:
            out.append(r)
    return out


# ════════════════════════════════════════════════
#  FORMATTERS
# ════════════════════════════════════════════════

# ── Status & ban display maps ────────────────────────────────────────────────
STATUS_DISPLAY = {
    "active":         ("🟢", "✦ ALIVE  —  NOT BANNED 🔥"),
    "banned":         ("🔴", "💀 BANNED  —  SHADOW CAUGHT"),
    "not_registered": ("👻", "⚫ GHOST  —  NOT ON WHATSAPP"),
    "rate_limited":   ("⏳", "⌛ RATE LIMITED — RETRY SOON"),
    "server_blocked": ("🌑", "🌑 SHADOW WALL — IP RESTRICTED"),
    "timeout":        ("🕐", "🕐 TIMEOUT — NO ECHO"),
    "error":          ("💢", "💢 SYSTEM ERROR"),
    "unknown":        ("🌀", "🌀 UNKNOWN — SIGNAL LOST"),
}
BAN_DISPLAY = {
    "Admin":    "👮 Admin Shadow",
    "Regional": "🌍 Regional Shadow",
    "Personal": "🔒 Personal Shadow",
}


def build_msg(results, elapsed, requester):
    total  = len(results)
    banned = sum(1 for r in results if r["status"] == "banned")
    active = sum(1 for r in results if r["status"] == "active")
    other  = total - banned - active
    now    = datetime.now().strftime("%d %b %Y · %H:%M:%S")
    div    = "━" * 30

    lines = [
        "👁  <b>K A G E  X  B A N  C H E C K E R</b>  👁",
        f"<i>🕐 {now}</i>",
        "",
        "<b>🌑 Shadow Report</b>",
        f"  ⚡ Checked  : <code>{total}</code>",
        f"  💀 Banned   : <code>{banned}</code>",
        f"  🟢 Alive    : <code>{active}</code>",
        f"  🌀 Other    : <code>{other}</code>",
        f"  🏎 Speed    : <code>{elapsed:.2f}s</code>",
        "",
        div,
        "",
    ]

    for i, r in enumerate(results, 1):
        icon, lbl = STATUS_DISPLAY.get(r["status"], ("🌀", r["status"].upper()))
        masked    = mask_number(r["display"])
        bt        = BAN_DISPLAY.get(r.get("ban_type"), "") if r.get("ban_type") else "—"
        src       = r.get("source", "KAGE X API")
        note      = r.get("note", "")

        lines.append(f"<b>[ #{i} ]</b>")
        lines.append(f"📱 <b>Number</b>   : <code>{masked}</code>")
        lines.append(f"{icon} <b>Status</b>    : <b>{lbl}</b>")
        lines.append(f"🛡 <b>Ban Type</b>  : {bt}")
        lines.append(f"⏱ <b>Ping</b>      : <code>{r['ping_ms']} ms</code>")
        lines.append(f"📡 <b>API</b>       : <i>{src}</i>")
        if note and note not in ("(fallback)", ""):
            lines.append(f"🩸 <b>Reason</b>    : <i>{note}</i>")
        lines.append("")

    lines += [
        div,
        f"  👤 <b>{requester}</b>  ·  💀 <b>K A G E  X</b> 💀",
    ]
    return "\n".join(lines)


# ════════════════════════════════════════════════
#  HANDLERS
# ══════════════════��═════════════════════════════

async def cmd_start(u: Update, _):
    name = u.effective_user.first_name
    welcome = (
        "👁  <b>K A G E  X  B A N  C H E C K E R</b>  👁\n"
        "<i>— We see through every shadow —</i>\n\n"
        f"⚡ Welcome back, <b>{name}</b>. The dark scanner is online.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌑 <b>What we expose:</b>\n"
        "   🟢 <b>Alive</b>   — Active &amp; roaming free\n"
        "   💀 <b>Banned</b>  — Caught in the shadow\n"
        "   👻 <b>Ghost</b>   — Never existed on WA\n\n"
        "🩸 <b>Shadow types detected:</b>\n"
        "   🔒 Personal  ·  🌍 Regional  ·  👮 Admin\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Send numbers like this:</b>\n"
        "  <code>!check +2348012345678</code>\n"
        "  <code>!check 234xxx 447xxx 155xxx</code>\n\n"
        "💀 <b>K A G E  X</b>  ·  <i>The shadow never lies.</i>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 How To Check", callback_data="help"),
            InlineKeyboardButton("📡 API Status",   callback_data="testapi"),
        ],
        [
            InlineKeyboardButton("💀 About KAGE X", callback_data="about"),
        ],
    ])
    await send_with_banner(u.message, welcome, reply_markup=kb)


async def cmd_help(u: Update, _):
    t   = u.message or u.callback_query.message
    msg = (
        "⚡ <b>KAGE X — Commands</b>\n\n"
        "<code>!check +234xxxxxxxx</code> — single check\n"
        "<code>!check 234xxx 447xxx</code> — multi check\n"
        "<code>!testapi</code> — test KAGE X API\n"
        "<code>!ping</code> — latency\n"
        "<code>!about</code> — about KAGE X\n\n"
        f"⚡ Max <b>{MAX_NUM}</b> numbers per request."
    )
    await send_with_banner(t, msg)


async def cmd_about(u: Update, _):
    t   = u.message or u.callback_query.message
    msg = (
        "<blockquote>"
        "👁  <b>K A G E  X  —  S H A D O W  E D I T I O N</b>  👁\n"
        "<i>Version 8.0  ·  The darkest scanner alive.</i>\n\n"
        "🌑 Every number holds a secret.\n"
        "    We pull it straight from the shadow.\n\n"
        "⚡ <b>Engine</b>      :  KAGE X Neural API\n"
        "🛡 <b>Detection</b>  :  Personal · Regional · Admin\n"
        "🌀 <b>Fallback</b>   :  5-Shadow Scan Engine\n"
        "🔒 <b>Privacy</b>    :  Numbers always masked\n"
        "🌐 <b>Prefix</b>     :  <code>!</code>\n\n"
        "💀  <i>We see what others can't.</i>\n"
        "    <i>The shadow never lies.</i>"
        "</blockquote>"
    )
    await send_with_banner(t, msg)


async def cmd_testapi(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = u.message or u.callback_query.message
    wait   = await target.reply_text("🔍 Testing KAGE X BanCheck API...",
                                     parse_mode=ParseMode.HTML)
    conn = aiohttp.TCPConnector(limit=5, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as sess:
        result = await baron0_check(sess, "447386195088")
    await wait.delete()

    if result is None:
        txt = (
            "❌ <b>KAGE X API: NO RESPONSE</b>\n\n"
            "Possible causes:\n"
            "• API key expired or invalid\n"
            "• Engine is temporarily down\n"
            "• Network issue from Railway\n\n"
            "Using shadow fallback engine until resolved."
        )
    elif result["status"] == "rate_limited":
        txt = ("⏳ <b>KAGE X API: RATE LIMITED</b>\n"
               "Wait 1 min and try again.")
    else:
        txt = (
            f"✅ <b>KAGE X API: CONNECTED &amp; WORKING</b>\n\n"
            f"Test number: <code>+44738****088</code>\n"
            f"Result: <b>{result['status'].upper()}</b>\n"
            f"💀 KAGE X is fully online 🔥"
        )
    await send_with_banner(target, txt)


async def cmd_ping(u: Update, _):
    t0  = time.monotonic()
    tmp = await u.message.reply_text("🏓 Pinging...")
    ms  = round((time.monotonic() - t0) * 1000)
    await tmp.delete()
    await send_with_banner(u.message,
                           f"🏓 <b>Pong!</b>\n⚡ Latency: <code>{ms} ms</code>")


async def cmd_check(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args  = " ".join(ctx.args) if ctx.args else ""
    nums  = parse_numbers(args)
    user  = u.effective_user

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
        f"🌑 <b>Scanning</b> <code>{len(nums)}</code> number(s) through the shadow... ⚡",
        parse_mode=ParseMode.HTML,
    )
    t0      = time.monotonic()
    results = await check_all(nums)
    elapsed = time.monotonic() - t0
    req     = f"@{user.username}" if user.username else user.first_name
    await wait.delete()

    result_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 CHECK ANOTHER", callback_data="check_another"),
        InlineKeyboardButton("📞 CONTACT",       url="https://t.me/"),
    ]])
    await send_with_banner(
        u.message,
        build_msg(results, elapsed, req),
        reply_markup=result_kb,
    )


async def on_prefix(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (u.message.text or "").strip()
    if not text.startswith(PREFIX): return
    parts    = text[len(PREFIX):].split(None, 1)
    cmd      = parts[0].lower() if parts else ""
    ctx.args = parts[1].split() if len(parts) > 1 else []
    fn = {"check": cmd_check, "start": cmd_start, "help": cmd_help,
          "about": cmd_about, "ping": cmd_ping, "testapi": cmd_testapi,
          "setbanner": cmd_setbanner}.get(cmd)
    if fn:
        await fn(u, ctx)
    else:
        await u.message.reply_text(
            f"❓ Unknown: <code>!{cmd}</code> — try <code>!help</code>",
            parse_mode=ParseMode.HTML,
        )


async def on_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    if q.data == "check_another":
        await q.message.reply_text(
            "⚡ <b>Send your next number(s):</b>\n\n"
            "  <code>!check +2348012345678</code>\n"
            "  <code>!check 234xxx 447xxx 155xxx</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    fn = {"help": cmd_help, "about": cmd_about, "testapi": cmd_testapi}.get(q.data)
    if fn: await fn(u, ctx)


async def on_err(update, ctx): print(f"[ERR] {ctx.error}")


# ════════════════════════════════════════════════
#  MAIN
# ══════════════════════════��═════════════════════

def main():
    token = BOT_TOKEN.strip()
    if not token:
        print("⚠️  BOT_TOKEN not set."); return

    app = Application.builder().token(token).build()
    for c, f in [("start", cmd_start), ("help", cmd_help), ("about", cmd_about),
                 ("ping", cmd_ping), ("check", cmd_check), ("testapi", cmd_testapi)]:
        app.add_handler(CommandHandler(c, f))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_prefix))
    # Photo with !setbanner caption → set the banner image
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.CaptionRegex(r"(?i)^!setbanner"),
        cmd_setbanner,
    ))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_error_handler(on_err)

    print(f"\n╔{'='*48}╗")
    print(f"║  ⚡  {BOT_NAME}  {BOT_VER}  ║")
    print(f"║  Primary   : baron0.com/api/v2/check           ║")
    print(f"║  Fallback  : 5-method WA engine                ║")
    print(f"╚{'='*48}╝\n")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
