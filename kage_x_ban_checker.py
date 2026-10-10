#!/usr/bin/env python3
"""
KAGE X Ban Checker  v7.0
Uses your BanCheck API key for accurate real-time WhatsApp ban detection.
Falls back to 5-method engine if API is unreachable.
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

# ═════════════════════════════════════════════════════
#  CONFIG  —  set these as Railway env vars
# ═════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_NAME  = "KAGE X Ban Checker"
BOT_VER   = "v7.0"
PREFIX    = "!"
MAX_NUM   = 20

# ── BanCheck API (your key + optional custom URL override) ──────────────────
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
    "G6PkZN8gLZakTSt_wMNupOBg9n47FWeQ9SqYxrCz5KXO3TLlRZeSYP",
)

# Override with BANCHECK_API_URL env var if you know your service URL.
# Leave blank = auto-probe from the list below.
BANCHECK_API_URL = os.getenv("BANCHECK_API_URL", "")

# Candidate endpoints to auto-probe (tried in order)
BANCHECK_CANDIDATES = [
    # Format: (url_template, method, key_field)
    # url_template gets .format(phone=digits, key=BANCHECK_KEY)
    "https://api.bancheck.io/v1/check?phone={phone}&key={key}",
    "https://api.bancheck.cc/v1/check?phone={phone}&key={key}",
    "https://bancheck.io/api/v1/check?number={phone}&apikey={key}",
    "https://api.wacheck.io/v1/check?phone={phone}&key={key}",
    "https://api.wachecker.io/check?number={phone}&apikey={key}",
    "https://api.wabubble.com/v1/check?phone={phone}&token={key}",
    "https://api.phonechecker.io/whatsapp?phone={phone}&key={key}",
    "https://waapi.app/api/v1/check?phone={phone}&api_key={key}",
    "https://api.check-wa.com/check?number={phone}&key={key}",
    "https://api.checkswa.io/v1/ban?phone={phone}&key={key}",
    "https://api.whatsapp-checker.com/check?number={phone}&key={key}",
    "https://api.watools.io/v1/check?phone={phone}&apikey={key}",
]

# Discovered working endpoint cached at runtime
_WORKING_ENDPOINT = None
_PROBE_DONE       = False


# ═════════════════════════════════════════════════════
#  NUMBER UTILITIES
# ═════════════════════════════════════════════════════

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


# ═════════════════════════════════════════════════════
#  BANCHECK API  —  PRIMARY METHOD
# ═════════════════════════════════════════════════════

def _parse_bancheck_response(data: dict, http: int) -> dict | None:
    """
    Parse any common bancheck API response format.
    Returns dict with keys: status, ban_type, note  OR  None if unclear.
    """
    if not data or not isinstance(data, dict):
        return None

    # Flatten all string values for keyword scanning
    flat = json.dumps(data).lower()

    # ── Active signals ────────────────────────────────────────────────
    def _is_active():
        # Explicit not-banned / active fields
        for k in ("is_banned", "banned", "isBanned"):
            v = data.get(k)
            if v is False or v == 0 or str(v).lower() in ("false", "0", "no"):
                return True
        for k in ("status", "result", "state"):
            v = str(data.get(k, "")).lower()
            if v in ("active", "not_banned", "clean", "ok", "safe",
                     "registered", "exist", "exists", "valid"):
                return True
        if "\"is_banned\": false" in flat or "\"banned\": false" in flat:
            return True
        return False

    # ── Banned signals ────────────────────────────────────────────────
    def _is_banned():
        for k in ("is_banned", "banned", "isBanned"):
            v = data.get(k)
            if v is True or v == 1 or str(v).lower() in ("true", "1", "yes"):
                return True
        for k in ("status", "result", "state"):
            v = str(data.get(k, "")).lower()
            if v in ("banned", "blocked", "suspended", "disabled",
                     "terminated", "violated"):
                return True
        if "\"is_banned\": true" in flat or "\"banned\": true" in flat:
            return True
        return False

    # ── Not registered signals ───────────────────────────────────────
    def _not_registered():
        for k in ("is_registered", "registered", "isRegistered",
                  "has_whatsapp", "hasWhatsApp", "on_whatsapp"):
            v = data.get(k)
            if v is False or v == 0 or str(v).lower() in ("false", "0", "no"):
                return True
        for k in ("status", "result", "state"):
            v = str(data.get(k, "")).lower()
            if v in ("not_registered", "unregistered", "not_exist",
                     "nonexistent", "not_found", "invalid"):
                return True
        return False

    # ── Ban type extraction ───────────────────────────────────────────
    def _ban_type():
        for k in ("ban_type", "banType", "type", "ban_reason",
                  "reason", "violation_type"):
            v = str(data.get(k, "")).lower()
            if "admin" in v or "platform" in v or "enforcement" in v:
                return "Admin"
            if "regional" in v or "country" in v or "region" in v:
                return "Regional"
            if "personal" in v or "account" in v or "spam" in v or "abuse" in v:
                return "Personal"
        return "Personal"  # default for confirmed bans

    if _not_registered():
        return {"status": "not_registered", "ban_type": None,
                "note": "Confirmed by BanCheck API"}
    if _is_active():
        return {"status": "active", "ban_type": None,
                "note": "Confirmed by BanCheck API"}
    if _is_banned():
        return {"status": "banned", "ban_type": _ban_type(),
                "note": "Confirmed by BanCheck API"}
    return None


async def _call_endpoint(session, url_tpl, digits) -> tuple[int, dict]:
    """Call a single API endpoint. Returns (http_code, parsed_json)."""
    url = url_tpl.format(phone=digits, key=BANCHECK_KEY)
    headers = {
        "Authorization": f"Bearer {BANCHECK_KEY}",
        "X-API-Key": BANCHECK_KEY,
        "Accept": "application/json",
        "User-Agent": "KAGEX-BanChecker/7.0",
    }
    try:
        # Try GET first
        async with session.get(
            url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
            ssl=False, allow_redirects=True,
        ) as r:
            code = r.status
            try:
                data = await r.json(content_type=None)
            except Exception:
                text = await r.text(errors="ignore")
                try:
                    data = json.loads(text)
                except Exception:
                    data = {}
            return code, data
    except asyncio.TimeoutError:
        return -2, {}
    except Exception:
        return -1, {}


async def _call_endpoint_post(session, base_url, digits) -> tuple[int, dict]:
    """Try POST with JSON body."""
    payload = {"phone": digits, "number": digits, "key": BANCHECK_KEY,
               "api_key": BANCHECK_KEY, "token": BANCHECK_KEY}
    headers = {
        "Authorization": f"Bearer {BANCHECK_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with session.post(
            base_url, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
            ssl=False,
        ) as r:
            code = r.status
            try:
                data = await r.json(content_type=None)
            except Exception:
                text = await r.text(errors="ignore")
                try:
                    data = json.loads(text)
                except Exception:
                    data = {}
            return code, data
    except Exception:
        return -1, {}


async def probe_bancheck_api(session) -> str | None:
    """
    Auto-detect the working BanCheck API endpoint.
    Returns the working URL template or None.
    """
    global _WORKING_ENDPOINT, _PROBE_DONE
    if _PROBE_DONE:
        return _WORKING_ENDPOINT

    # Use custom URL if set
    if BANCHECK_API_URL:
        _WORKING_ENDPOINT = BANCHECK_API_URL
        _PROBE_DONE = True
        print(f"[BanCheck] Using configured URL: {BANCHECK_API_URL}")
        return _WORKING_ENDPOINT

    print("[BanCheck] Auto-probing API endpoints...")
    test_num = "447386195088"  # UK test number

    for tpl in BANCHECK_CANDIDATES:
        code, data = await _call_endpoint(session, tpl, test_num)
        if code in (200, 201, 202):
            result = _parse_bancheck_response(data, code)
            if result is not None:
                _WORKING_ENDPOINT = tpl
                _PROBE_DONE = True
                print(f"[BanCheck] Working endpoint found: {tpl}")
                return tpl
        elif code not in (-1, -2, 404, 422):  # reachable but auth issue
            # Endpoint is reachable even if auth fails — this is our service
            _WORKING_ENDPOINT = tpl
            _PROBE_DONE = True
            print(f"[BanCheck] Reachable endpoint: {tpl} (HTTP {code})")
            return tpl

    _PROBE_DONE = True
    _WORKING_ENDPOINT = None
    print("[BanCheck] No working endpoint found — using fallback engine.")
    return None


async def bancheck_api_check(session, digits: str) -> dict | None:
    """
    Check a number via BanCheck API.
    Returns result dict or None if API unavailable.
    """
    endpoint = await probe_bancheck_api(session)
    if not endpoint:
        return None

    code, data = await _call_endpoint(session, endpoint, digits)

    if code in (-1, -2):
        return None

    result = _parse_bancheck_response(data, code)
    if result:
        return result

    # Try POST if GET gave no clear result
    base = endpoint.split("?")[0]
    code2, data2 = await _call_endpoint_post(session, base, digits)
    if code2 in (200, 201):
        return _parse_bancheck_response(data2, code2)

    return None


# ═════════════════════════════════════════════════════
#  FALLBACK 5-METHOD ENGINE
# ═════════════════════════════════════════════════════

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


_ACTIVE_KW   = ["send message","open whatsapp","continue to chat",
                "click to chat","start chat","whatsapp://send",
                "\"phone\":","phonenumber","send_message"]
_BANNED_KW   = ["this account has been banned","account has been banned",
                "number is banned","phone number is banned",
                "banned from whatsapp","banned for violating"]
_NOREG_KW    = ["phone number shared via url is invalid",
                "this link is not valid","not on whatsapp",
                "number is not registered","invalid phone number",
                "no whatsapp account","doesn't have whatsapp"]


def _scan(body):
    for kw in _ACTIVE_KW:
        if kw in body: return "active"
    for kw in _BANNED_KW:
        if kw in body: return "banned"
    for kw in _NOREG_KW:
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
        s  = str(d.get("status",  "")).lower()
        rs = str(d.get("result",  "")).lower()
        rn = str(d.get("reason",  "")).lower()
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
    code, body, url = await _get_page(session, f"https://wa.me/{digits}", headers=h)
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
            f"https://wa.me/{digits}", headers=h,
            timeout=aiohttp.ClientTimeout(total=8),
            ssl=False, allow_redirects=False,
        ) as r:
            loc = r.headers.get("Location", "").lower()
            if "send" in loc or "whatsapp" in loc: return "active"
            if "invalid" in loc or "error" in loc: return "not_registered"
    except Exception:
        pass
    return None


async def _fallback_classify_ban(session, digits):
    h = {"User-Agent": random.choice(BROWSER_UAS),
         "Accept-Language": "en-US,en;q=0.9"}
    code, body, _ = await _get_page(
        session, "https://api.whatsapp.com/send",
        params={"phone": digits}, headers=h,
    )
    if code == 403: return "Admin"
    regional_kw = ["not available in your country","not available in your region",
                   "country is not supported"]
    admin_kw    = ["terms of service","policy violation","violated our terms",
                   "enforcement","spam","abuse"]
    for kw in regional_kw:
        if kw in body: return "Regional"
    for kw in admin_kw:
        if kw in body: return "Admin"
    return "Personal"


async def fallback_check(session, digits: str) -> dict:
    """5-method fallback check when BanCheck API is unavailable."""
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

    counts = {k: sigs.count(k) for k in
              ("active", "banned", "not_registered", "rate_limited")}

    if counts["active"] >= 1:
        return {"status": "active", "ban_type": None, "note": "(fallback engine)"}
    if counts["banned"] >= 1:
        bt = await _fallback_classify_ban(session, digits)
        return {"status": "banned", "ban_type": bt, "note": "(fallback engine)"}
    if counts["not_registered"] >= 1:
        return {"status": "not_registered", "ban_type": None, "note": "(fallback engine)"}
    if not sigs:
        return {"status": "server_blocked", "ban_type": None,
                "note": "WA blocked server IP — add BANCHECK_API_URL env var"}
    return {"status": "unknown", "ban_type": None, "note": f"{len(sigs)}/5 weak signals"}


# ═════════════════════════════════════════════════════
#  MASTER CHECK
# ═════════════════════════════════════════════════════

async def check_one(session, digits: str) -> dict:
    display = "+" + digits
    t0      = time.monotonic()

    # 1. Try BanCheck API first (most accurate)
    api_result = await bancheck_api_check(session, digits)
    ping_ms    = round((time.monotonic() - t0) * 1000)

    if api_result:
        return {
            "display":  display,
            "status":   api_result["status"],
            "ban_type": api_result.get("ban_type"),
            "ping_ms":  ping_ms,
            "note":     api_result.get("note", "BanCheck API ✓"),
            "source":   "🟣 BanCheck API",
        }

    # 2. Fallback to 5-method engine
    fb = await fallback_check(session, digits)
    ping_ms = round((time.monotonic() - t0) * 1000)
    return {
        "display":  display,
        "status":   fb["status"],
        "ban_type": fb.get("ban_type"),
        "ping_ms":  ping_ms,
        "note":     fb.get("note", ""),
        "source":   "🟡 Fallback Engine",
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


# ═════════════════════════════════════════════════════
#  FORMATTERS
# ═════════════════════════════════════════════════════

STATUS_LBL = {
    "active":         ("🟢", "ACTIVE — Not Banned"),
    "banned":         ("🔴", "BANNED"),
    "not_registered": ("⚫", "NOT ON WHATSAPP"),
    "rate_limited":   ("⏳", "RATE LIMITED"),
    "server_blocked": ("🟠", "IP BLOCKED BY WA"),
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
        "║   ⚡  <b>KAGE X Ban Checker</b>  ⚡  v7.0  ║",
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
        bt  = BAN_LBL.get(r.get("ban_type"), "") if r.get("ban_type") else ""
        src = r.get("source", "")
        lines.append(f"<b>#{i}</b>  {icon}  <code>{r['display']}</code>")
        lines.append(f"      Status   : <b>{lbl}</b>")
        if bt:
            lines.append(f"      Ban Type : <b>{bt}</b>")
        lines.append(f"      Ping     : <code>{r['ping_ms']} ms</code>")
        if src:
            lines.append(f"      Source   : <i>{src}</i>")
        if r.get("note") and r["note"] not in ("Confirmed by BanCheck API", "BanCheck API ✓"):
            lines.append(f"      Note     : <i>{r['note']}</i>")
        lines.append("")

    lines += [div, f"👤 By: <b>{requester}</b>  |  🤖 <b>{BOT_NAME}</b>"]
    return "\n".join(lines)


# ═════════════════════════════════════════════════════
#  HANDLERS
# ═════════════════════════════════════════════════════

async def cmd_start(u: Update, _):
    name = u.effective_user.first_name
    msg  = (
        "╔" + "═" * 38 + "╗\n"
        "║   ⚡  <b>KAGE X Ban Checker</b>  ⚡  v7.0  ║\n"
        "╚" + "═" * 38 + "╝\n\n"
        f"👋 Welcome, <b>{name}</b>!\n\n"
        "🔑 <b>Powered by BanCheck API + 5-Method Fallback</b>\n"
        "Real-time, accurate WhatsApp ban detection.\n\n"
        "<b>Results:</b>\n"
        "  🟢 Active  •  🔴 Banned  •  ⚫ Not Registered\n"
        "  Ban types: 🔒 Personal  🌍 Regional  👮 Admin\n\n"
        "━" * 26 + "\n"
        "  <code>!check +2348012345678</code>\n"
        "  <code>!check 234xxx 447xxx 155xxx</code>\n"
        "  <code>!testapi</code>  │  <code>!ping</code>\n"
        "━" * 26
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Help",    callback_data="help"),
        InlineKeyboardButton("ℹ️ About",   callback_data="about"),
        InlineKeyboardButton("🔌 Test API", callback_data="testapi"),
    ]])
    await u.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_help(u: Update, _):
    msg = (
        "⚡ <b>KAGE X — Commands</b>\n\n"
        "<code>!check +234xxxxxxxx</code> — single\n"
        "<code>!check 234xxx 447xxx</code> — multi\n"
        "<code>!testapi</code> — test BanCheck API\n"
        "<code>!ping</code> — latency\n"
        "<code>!about</code> — bot info\n\n"
        f"⚡ Max <b>{MAX_NUM}</b> numbers per request."
    )
    t = u.message or u.callback_query.message
    await t.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_about(u: Update, _):
    msg = (
        "╔" + "═" * 34 + "╗\n"
        f"║  ⚡ <b>{BOT_NAME}</b>  {BOT_VER}     ║\n"
        "╚" + "═" * 34 + "╝\n\n"
        "🔑 Primary: BanCheck API (your key)\n"
        "🔄 Fallback: 5-method engine\n"
        "🎯 Detects Personal / Regional / Admin bans\n"
        "🌐 Global prefix: <code>!</code>"
    )
    t = u.message or u.callback_query.message
    await t.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_testapi(u: Update, _):
    """Test the BanCheck API connection and display status."""
    target = u.message or u.callback_query.message
    wait   = await target.reply_text(
        "🔍 Probing BanCheck API endpoints...", parse_mode=ParseMode.HTML
    )

    conn = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as sess:
        endpoint = await probe_bancheck_api(sess)

        if endpoint:
            # Try a real check on a known active number
            test_result = await bancheck_api_check(sess, "447386195088")
            if test_result:
                status_txt = (
                    f"✅ <b>BanCheck API: CONNECTED</b>\n\n"
                    f"Endpoint: <code>{endpoint[:60]}...</code>\n"
                    f"Test check: {test_result['status'].upper()}\n\n"
                    f"Your API key is working! 🔥"
                )
            else:
                status_txt = (
                    f"⚠️ <b>BanCheck API: REACHABLE but no data</b>\n\n"
                    f"Endpoint found: <code>{endpoint[:60]}...</code>\n"
                    f"But the response couldn’t be parsed.\n"
                    f"Set <code>BANCHECK_API_URL</code> env var with\n"
                    f"the correct URL from your API provider."
                )
        else:
            candidates_tried = "\n".join(
                f"  • {c.split('?')[0][:50]}" for c in BANCHECK_CANDIDATES[:5]
            )
            status_txt = (
                f"❌ <b>BanCheck API: NOT REACHABLE</b>\n\n"
                f"Tried {len(BANCHECK_CANDIDATES)} endpoints — none responded.\n\n"
                f"<b>To fix:</b>\n"
                f"1. Find out which website your key is from\n"
                f"2. Set <code>BANCHECK_API_URL</code> in Railway env vars\n"
                f"   e.g. <code>https://api.yourservice.com/v1/check?phone={{{{phone}}}}&key={{{{key}}}}</code>\n\n"
                f"Using 5-method fallback engine for now."
            )

    await wait.edit_text(status_txt, parse_mode=ParseMode.HTML)


async def cmd_ping(u: Update, _):
    t0  = time.monotonic()
    msg = await u.message.reply_text("🏓 Pinging...")
    ms  = round((time.monotonic() - t0) * 1000)
    await msg.edit_text(f"🏓 <b>Pong!</b>\n⚡ Latency: <code>{ms} ms</code>",
                        parse_mode=ParseMode.HTML)


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
        f"⏳ Checking <code>{len(nums)}</code> number(s)... ✨",
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
    if not text.startswith(PREFIX): return
    parts    = text[len(PREFIX):].split(None, 1)
    cmd      = parts[0].lower() if parts else ""
    ctx.args = parts[1].split() if len(parts) > 1 else []
    fn = {"check": cmd_check, "start": cmd_start, "help":  cmd_help,
          "about": cmd_about, "ping":  cmd_ping,  "testapi": cmd_testapi}.get(cmd)
    if fn:
        await fn(u, ctx)
    else:
        await u.message.reply_text(
            f"❓ Unknown: <code>!{cmd}</code> — try <code>!help</code>",
            parse_mode=ParseMode.HTML,
        )


async def on_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    dispatch = {"help": cmd_help, "about": cmd_about, "testapi": cmd_testapi}
    fn = dispatch.get(q.data)
    if fn: await fn(u, ctx)


async def on_err(update, ctx): print(f"[ERR] {ctx.error}")


# ═════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════

def main():
    token = BOT_TOKEN.strip()
    if not token:
        print("⚠️  BOT_TOKEN not set."); return
    app = Application.builder().token(token).build()
    for c, f in [("start", cmd_start),("help", cmd_help),("about", cmd_about),
                 ("ping", cmd_ping),("check", cmd_check),("testapi", cmd_testapi)]:
        app.add_handler(CommandHandler(c, f))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_prefix))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_error_handler(on_err)
    print(f"\n╔{'='*46}╗")
    print(f"║  ⚡  {BOT_NAME}  {BOT_VER}  ║")
    print(f"║  BanCheck API + 5-Method Fallback Engine     ║")
    print(f"╚{'='*46}╝\n")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
