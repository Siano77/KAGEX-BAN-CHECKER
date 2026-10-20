"""Isolated admin controls for KAGE X. Existing checking code is not modified.

SQLite state lives at ADMIN_DB_PATH (default: admin_panel.sqlite3).
All administration is restricted to private chat, including callback buttons.
"""
import asyncio
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import ApplicationHandlerStop, TypeHandler

log = logging.getLogger(__name__)
PAGE_SIZE = 8
STATE_TTL = 600
COMMANDS = {
    "admin": "panel", "panel": "panel", "stats": "stats", "uptime": "uptime",
    "users": "users", "groups": "groups", "admins": "admins", "ban": "ban",
    "unban": "unban", "addadmin": "add", "removeadmin": "remove",
    "broadcast": "broadcast", "cancel": "cancel", "cancelbroadcast": "stop",
}


class Store:
    """Short, parameterized local transactions; no open transaction spans an await."""
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    username TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                    private_chat_id INTEGER, unreachable INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY, added_by INTEGER NOT NULL,
                    added_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bans (
                    user_id INTEGER PRIMARY KEY, banned_by INTEGER NOT NULL,
                    banned_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS groups_seen (
                    chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL,
                    username TEXT, active INTEGER NOT NULL, last_seen REAL NOT NULL
                );
            """)

    def connect(self):
        return _Connection(self.path)

    def execute(self, sql, params=()):
        with self.connect() as db:
            db.execute(sql, params)

    def rows(self, sql, params=()):
        with self.connect() as db:
            return db.execute(sql, params).fetchall()

    def scalar(self, sql, params=()):
        return self.rows(sql, params)[0][0]

    def track_user(self, user, private_id=None):
        if user.is_bot:
            return
        now = time.time()
        self.execute("""
            INSERT INTO users(user_id,name,username,first_seen,last_seen,private_chat_id)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,
                username=excluded.username, last_seen=excluded.last_seen,
                private_chat_id=COALESCE(excluded.private_chat_id,users.private_chat_id),
                unreachable=CASE WHEN excluded.private_chat_id IS NOT NULL
                    THEN 0 ELSE users.unreachable END
        """, (user.id, user.full_name, user.username, now, now, private_id))

    def track_group(self, chat, active=True):
        self.execute("""
            INSERT INTO groups_seen(chat_id,title,username,active,last_seen)
            VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,username=excluded.username,
                active=excluded.active,last_seen=excluded.last_seen
        """, (chat.id, chat.title or "Untitled group", chat.username, int(active), time.time()))

    def banned(self, uid):
        return bool(self.scalar("SELECT COUNT(*) FROM bans WHERE user_id=?", (uid,)))

    def admin(self, uid):
        return bool(self.scalar("SELECT COUNT(*) FROM admins WHERE user_id=?", (uid,)))

    def recipients(self):
        return self.rows("""
            SELECT user_id,private_chat_id FROM users
            WHERE private_chat_id IS NOT NULL AND unreachable=0
                AND user_id NOT IN (SELECT user_id FROM bans)
            ORDER BY user_id
        """)


class _Connection:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row

    def __enter__(self):
        return self.db

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                self.db.commit()
            else:
                self.db.rollback()
        finally:
            self.db.close()


def keyboard(rows):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="adm:" + data) for label, data in row]
        for row in rows
    ])


BACK = keyboard([[("⬅ Admin Panel", "panel")]])
CANCEL = keyboard([[("✖ Cancel", "cancel")]])


class AdminPanel:
    def __init__(self, app, owner_id, reply, db_path):
        self.app = app
        self.owner_id = owner_id
        self.reply = reply
        self.store = Store(db_path)
        self.started = time.monotonic()
        self.pending = {}  # (admin ID, private chat ID) -> expiring one-shot action
        self.broadcast_task = None
        self.broadcast_owner = None
        self.stop_broadcast = asyncio.Event()

    def is_admin(self, uid):
        # No hidden access when owner configuration is absent.
        return bool(self.owner_id and (uid == self.owner_id or self.store.admin(uid)))

    async def say(self, msg, text, markup=BACK):
        # The shared formatter reads HTML; escape untrusted names/group titles first.
        return await self.reply(msg, escape(text), reply_markup=markup)

    def parse_command(self, text, bot_username):
        match = re.match(r"^[!/]([a-z]+)(?:@([a-z0-9_]+))?(?:\s+([\s\S]*))?$", text.strip(), re.I)
        if not match:
            return None, ""
        command, addressed, args = match.groups()
        if addressed and addressed.lower() != (bot_username or "").lower():
            return None, ""
        return COMMANDS.get(command.lower()), (args or "").strip()

    async def middleware(self, update, ctx):
        try:
            await self._route(update, ctx)
        except sqlite3.Error:
            log.exception("Admin storage error; update blocked to preserve access controls")
            query = update.callback_query
            try:
                if query:
                    await query.answer("Bot storage is temporarily unavailable.", show_alert=True)
                elif update.effective_message:
                    await self.say(update.effective_message,
                                   "Bot storage is temporarily unavailable. Please retry later.", None)
            except TelegramError:
                pass
            raise ApplicationHandlerStop

    async def _route(self, update, ctx):
        member = update.my_chat_member
        if member and member.chat.type in ("group", "supergroup"):
            new = member.new_chat_member
            active = new.status in ("member", "administrator", "creator") or (
                new.status == "restricted" and getattr(new, "is_member", False))
            self.store.track_group(member.chat, active)
            return

        msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if not msg or not user or not chat:
            return
        private = chat.type == "private"
        query = update.callback_query
        text = (msg.text or msg.caption or "") if not query else ""
        cmd, args = self.parse_command(text, ctx.bot.username)
        if chat.type in ("group", "supergroup"):
            # Migrate persisted group IDs if Telegram upgrades a group.
            if getattr(msg, "migrate_to_chat_id", None):
                self.store.track_group(chat, False)
            else:
                self.store.track_group(chat)
        interacting = private or query is not None or text.startswith(("!", "/"))
        if interacting:
            self.store.track_user(user, chat.id if private else None)

        if user.id != self.owner_id and self.store.banned(user.id):
            if query:
                await query.answer("You are banned from using this bot.", show_alert=True)
            elif interacting:
                await self.say(msg, "🚫 You are banned from using this bot.", None)
            raise ApplicationHandlerStop

        admin_callback = bool(query and (query.data or "").startswith("adm:"))
        state_key = (user.id, chat.id)
        state = self.pending.get(state_key)
        if state and state["expires"] <= time.monotonic():
            self.pending.pop(state_key, None)
            state = None

        if admin_callback or cmd:
            if not self.is_admin(user.id):
                if query:
                    await query.answer("Admin access only.", show_alert=True)
                else:
                    await self.say(msg, "🚫 Admin access only. The owner must set OWNER_ID.", None)
                raise ApplicationHandlerStop
            if not private:
                if query:
                    await query.answer("Open the admin panel in my private chat.", show_alert=True)
                else:
                    await self.say(msg, "🔒 Open !admin in a private chat with this bot.", None)
                raise ApplicationHandlerStop
            if query:
                await query.answer()
                await self.callback(msg, user.id, query.data[4:], ctx)
            else:
                await self.command(msg, user.id, cmd, args, ctx)
            raise ApplicationHandlerStop

        if state and private:
            if not self.is_admin(user.id):
                self.pending.pop(state_key, None)
                return
            # Existing bot commands are never mistaken for an admin prompt answer.
            if text.startswith(("!", "/")):
                self.pending.pop(state_key, None)
                return
            if state["kind"] == "input":
                if state["action"] == "broadcast":
                    await self.prepare_broadcast(msg, user.id, ctx)
                else:
                    await self.prepare_target(msg, user.id, state["action"], text)
                raise ApplicationHandlerStop

    def put_state(self, msg, uid, **state):
        state["expires"] = time.monotonic() + STATE_TTL
        self.pending[(uid, msg.chat.id)] = state
        return state

    async def panel(self, msg, uid):
        self.pending.pop((uid, msg.chat.id), None)
        rows = [
            [("📊 Bot Stats", "stats"), ("⏱ Uptime", "uptime")],
            [("👥 Users List", "users:0"), ("💬 Groups", "groups:0")],
            [("🚫 Ban User", "ban"), ("✅ Unban User", "unban")],
            [("👑 Admins List", "admins")],
        ]
        if uid == self.owner_id:
            rows.append([("➕ Add Admin", "add"), ("➖ Remove Admin", "remove")])
        rows.append([("📢 Broadcast", "broadcast")])
        await self.say(msg, "👑 KAGE X — ADMIN PANEL\n\nChoose a category below.\n"
                       "Changes and broadcasts require confirmation.", keyboard(rows))

    async def command(self, msg, uid, cmd, args, ctx):
        # A new panel command invalidates older confirmation buttons.
        self.pending.pop((uid, msg.chat.id), None)
        if cmd == "cancel":
            self.pending.pop((uid, msg.chat.id), None)
            await self.say(msg, "✖ Pending action cancelled.")
        elif cmd == "panel":
            await self.panel(msg, uid)
        elif cmd in ("ban", "unban", "add", "remove"):
            if args:
                await self.prepare_target(msg, uid, cmd, args)
            else:
                await self.prompt_target(msg, uid, cmd)
        elif cmd == "broadcast":
            if args:
                await self.prepare_broadcast(msg, uid, ctx, text=args)
            else:
                await self.prompt_broadcast(msg, uid)
        elif cmd == "stop":
            await self.cancel_broadcast(msg, uid)
        else:
            await self.show(msg, uid, cmd, 0)

    async def callback(self, msg, uid, data, ctx):
        action, _, value = data.partition(":")
        if action == "confirm":
            await self.confirm(msg, uid, value, ctx)
        elif action in ("users", "groups"):
            if not value.isdigit() or len(value) > 8:
                await self.say(msg, "Invalid page. Open !admin again.")
                return
            await self.show(msg, uid, action, int(value))
        elif action in set(COMMANDS.values()):
            await self.command(msg, uid, action, "", ctx)
        else:
            await self.say(msg, "This button is no longer valid. Open !admin again.")

    async def show(self, msg, uid, action, page):
        self.pending.pop((uid, msg.chat.id), None)
        if action == "stats":
            s = self.store
            values = {
                "Users tracked": s.scalar("SELECT COUNT(*) FROM users"),
                "Active in last 24h": s.scalar("SELECT COUNT(*) FROM users WHERE last_seen>=?", (time.time()-86400,)),
                "Broadcast audience": len(s.recipients()),
                "Banned IDs": s.scalar("SELECT COUNT(*) FROM bans"),
                "Admins (incl. owner)": s.scalar("SELECT COUNT(*) FROM admins WHERE user_id!=?", (self.owner_id,))+1,
                "Tracked active groups": s.scalar("SELECT COUNT(*) FROM groups_seen WHERE active=1"),
            }
            await self.say(msg, "📊 BOT STATS\n\n" + "\n".join(f"{k}: {v}" for k,v in values.items()) +
                           "\n\nCounts begin when this module is installed.\n"
                           "These are local counts, not Telegram's monthly-user badge.")
        elif action == "uptime":
            seconds = int(time.monotonic()-self.started)
            days, remainder = divmod(seconds, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, seconds = divmod(remainder, 60)
            await self.say(msg, f"⏱ UPTIME\n\n{days}d {hours}h {minutes}m {seconds}s\n\nSince this bot process started.")
        elif action == "admins":
            rows = self.store.rows("SELECT a.user_id,u.name,u.username FROM admins a LEFT JOIN users u USING(user_id) WHERE a.user_id!=? ORDER BY a.added_at", (self.owner_id,))
            lines = ["👑 ADMINS", f"Owner: {self.owner_id}"]
            for row in rows:
                lines.append(f"{row['user_id']} · {(row['name'] or 'Not yet seen')[:70]}")
            await self.say(msg, "\n".join(lines))
        elif action in ("users", "groups"):
            await self.show_page(msg, action, page)

    async def show_page(self, msg, action, page):
        group = action == "groups"
        table = "groups_seen" if group else "users"
        where = " WHERE active=1" if group else ""
        total = self.store.scalar(f"SELECT COUNT(*) FROM {table}{where}")
        pages = max(1, (total+PAGE_SIZE-1)//PAGE_SIZE)
        page = min(page, pages-1)
        if group:
            records = self.store.rows("SELECT * FROM groups_seen WHERE active=1 ORDER BY last_seen DESC LIMIT ? OFFSET ?", (PAGE_SIZE,page*PAGE_SIZE))
        else:
            records = self.store.rows("SELECT u.*,b.user_id IS NOT NULL AS banned FROM users u LEFT JOIN bans b USING(user_id) ORDER BY u.first_seen DESC LIMIT ? OFFSET ?", (PAGE_SIZE,page*PAGE_SIZE))
        lines = ["💬 TRACKED ACTIVE GROUPS" if group else "👥 USERS LIST", f"Page {page+1}/{pages} · Total {total}", ""]
        for row in records:
            if group:
                lines += [row['title'][:80], f"ID: {row['chat_id']}"]
            else:
                lines += [row['name'][:80], f"ID: {row['user_id']}" + (" · BANNED" if row['banned'] else "")]
            if row['username']:
                lines.append("@" + row['username'])
            lines.append("")
        if not records:
            lines.append("None tracked yet.")
        if group:
            lines.append("Only groups observed since tracking began.\nNot a complete historical Telegram list.")
        nav = []
        if page > 0:
            nav.append(("⬅ Previous", f"{action}:{page-1}"))
        if page+1 < pages:
            nav.append(("Next ➡", f"{action}:{page+1}"))
        rows = [nav] if nav else []
        rows.append([("⬅ Admin Panel", "panel")])
        await self.say(msg, "\n".join(lines), keyboard(rows))

    def target_error(self, actor, action, target):
        if action in ("add", "remove") and actor != self.owner_id:
            return "🚫 Only the owner can add or remove admins."
        if target == self.owner_id:
            return "🔒 The owner cannot be banned, removed or changed here."
        if action == "ban" and self.is_admin(target):
            return "🔒 Admins cannot be banned. The owner must remove their admin role first."
        if action == "add" and self.store.banned(target):
            return "Unban this user before adding them as an admin."
        return None

    async def prompt_target(self, msg, uid, action):
        if action in ("add", "remove") and uid != self.owner_id:
            await self.say(msg, "🚫 Only the owner can manage admins.")
            return
        labels = {"ban":"BAN USER", "unban":"UNBAN USER", "add":"ADD ADMIN", "remove":"REMOVE ADMIN"}
        self.put_state(msg, uid, kind="input", action=action)
        await self.say(msg, f"{labels[action]}\n\nSend the person's numeric Telegram user ID.\n"
                       "Use Users List to find an ID.\n\n!cancel stops this action. Prompt expires in 10 minutes.", CANCEL)

    async def prepare_target(self, msg, uid, action, text):
        if not re.fullmatch(r"[1-9][0-9]{0,18}", text) or int(text) > 2**63-1:
            await self.say(msg, "Send one positive numeric Telegram user ID—not a phone number or @username.", CANCEL)
            return
        target = int(text)
        error = self.target_error(uid, action, target)
        if error:
            self.pending.pop((uid, msg.chat.id), None)
            await self.say(msg, error)
            return
        nonce = secrets.token_hex(8)
        self.put_state(msg, uid, kind="confirm", action=action, target=target, nonce=nonce)
        label = {"ban":"Ban user", "unban":"Unban user", "add":"Add admin", "remove":"Remove admin"}[action]
        note = "\nThis blocks access to this bot only." if action == "ban" else ""
        if action == "add":
            note = "\nThis grants access to user/group lists, bans and broadcasts."
        await self.say(msg, f"Confirm: {label}\nUser ID: {target}{note}",
                       keyboard([[("✅ Confirm", "confirm:"+nonce), ("✖ Cancel", "cancel")]]))

    def broadcasting(self):
        return self.broadcast_task is not None and not self.broadcast_task.done()

    async def prompt_broadcast(self, msg, uid):
        if self.broadcasting():
            await self.say(msg, "📢 A broadcast is already running.", keyboard([[("🛑 Stop broadcast", "stop")],[("⬅ Admin Panel", "panel")]]))
            return
        self.put_state(msg, uid, kind="input", action="broadcast")
        await self.say(msg, "📢 BROADCAST\n\nSend the message or media to broadcast.\n"
                       "You will see a preview before anything is sent.\n\n"
                       "Recipients: tracked private-chat users who are not banned\n"
                       "or known to have blocked the bot. Groups are not included.\n"
                       "!cancel stops this action.", CANCEL)

    async def prepare_broadcast(self, msg, uid, ctx, text=None):
        if self.broadcasting():
            await self.say(msg, "A broadcast is already running.")
            return
        audience = len(self.store.recipients())
        if not audience:
            self.pending.pop((uid, msg.chat.id), None)
            await self.say(msg, "No eligible private-chat users have been tracked yet.")
            return
        draft = {"text": text} if text is not None else {"source_chat_id":msg.chat.id,"message_id":msg.message_id}
        try:
            await self.deliver(ctx.bot, msg.chat.id, draft)
        except TelegramError:
            await self.say(msg, "❌ Telegram could not copy this message. Send plain text, a photo, video or document instead.", CANCEL)
            return
        nonce = secrets.token_hex(8)
        self.put_state(msg, uid, kind="confirm", action="broadcast", draft=draft, nonce=nonce)
        await self.say(msg, f"📢 Preview above.\n\nCurrent eligible recipients: {audience}\n"
                       "Nothing has been broadcast yet. Send it?\n"
                       "Confirmation expires in 10 minutes.",
                       keyboard([[("📢 Send broadcast", "confirm:"+nonce), ("✖ Cancel", "cancel")]]))

    async def deliver(self, bot, chat_id, draft):
        if "text" in draft:
            await bot.send_message(chat_id=chat_id, text=draft["text"], parse_mode=None)
        else:
            await bot.copy_message(chat_id=chat_id, from_chat_id=draft["source_chat_id"],
                                   message_id=draft["message_id"], reply_markup=InlineKeyboardMarkup([]))

    async def confirm(self, msg, uid, nonce, ctx):
        # Validate on every click; old, reused and cross-user buttons cannot act.
        state = self.pending.get((uid, msg.chat.id))
        if not state or state["kind"] != "confirm" or state["expires"] <= time.monotonic() or state["nonce"] != nonce:
            await self.say(msg, "This confirmation expired or was already used. Open !admin again.")
            return
        if not self.is_admin(uid):
            self.pending.pop((uid, msg.chat.id), None)
            await self.say(msg, "🚫 Admin access was removed.", None)
            return
        self.pending.pop((uid, msg.chat.id), None)
        action = state["action"]
        if action == "broadcast":
            if self.broadcasting():
                await self.say(msg, "A broadcast is already running. This draft was not sent.")
                return
            targets = self.store.recipients()
            self.stop_broadcast.clear()
            self.broadcast_owner = uid
            self.broadcast_task = self.app.create_task(
                self.run_broadcast(ctx.bot, msg, uid, state["draft"], targets), name="admin-broadcast")
            await self.say(msg, f"📢 Broadcast started. Recipients: {len(targets)}\n"
                           "You will receive a delivery summary.",
                           keyboard([[("🛑 Stop broadcast", "stop")],[("⬅ Admin Panel", "panel")]]))
            return
        target = state["target"]
        error = self.target_error(uid, action, target)
        if error:
            await self.say(msg, error)
            return
        if action == "ban":
            self.store.execute("INSERT OR IGNORE INTO bans VALUES(?,?,?)", (target,uid,time.time()))
        elif action == "unban":
            self.store.execute("DELETE FROM bans WHERE user_id=?", (target,))
        elif action == "add":
            self.store.execute("INSERT OR IGNORE INTO admins VALUES(?,?,?)", (target,uid,time.time()))
        elif action == "remove":
            self.store.execute("DELETE FROM admins WHERE user_id=?", (target,))
            for key in list(self.pending):
                if key[0] == target:
                    self.pending.pop(key, None)
        labels = {"ban":"Banned", "unban":"Unbanned", "add":"Admin access granted to", "remove":"Admin access removed from"}
        await self.say(msg, f"✅ {labels[action]} user {target}.")

    async def cancel_broadcast(self, msg, uid):
        if not self.broadcasting():
            await self.say(msg, "No broadcast is running.")
        elif uid not in (self.owner_id, self.broadcast_owner):
            await self.say(msg, "Only the initiating admin or the owner can stop this broadcast.")
        else:
            self.stop_broadcast.set()
            await self.say(msg, "🛑 Stop requested. Messages already delivered cannot be recalled.")

    async def run_broadcast(self, bot, msg, uid, draft, targets):
        sent = failed = skipped = 0
        try:
            for target in targets:
                if self.stop_broadcast.is_set() or not self.is_admin(uid):
                    break
                target_id, private_id = target['user_id'], target['private_chat_id']
                if self.store.banned(target_id):
                    skipped += 1
                    continue
                for attempt in range(3):
                    if self.stop_broadcast.is_set() or not self.is_admin(uid):
                        break
                    # A ban can be added while waiting out Telegram rate limits.
                    if self.store.banned(target_id):
                        skipped += 1
                        break
                    try:
                        await self.deliver(bot, private_id, draft)
                    except RetryAfter as exc:
                        delay = exc.retry_after
                        delay = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
                        if attempt == 2:
                            failed += 1
                        else:
                            try:
                                await asyncio.wait_for(self.stop_broadcast.wait(), timeout=delay+0.25)
                            except asyncio.TimeoutError:
                                pass
                            continue
                    except Forbidden:
                        self.store.execute("UPDATE users SET unreachable=1 WHERE user_id=?", (target_id,))
                        failed += 1
                    except TelegramError as exc:
                        log.warning("Broadcast delivery failed: %s", type(exc).__name__)
                        failed += 1
                    else:
                        sent += 1
                    break
                # Conservative pacing; Telegram RetryAfter above remains authoritative.
                try:
                    await asyncio.wait_for(self.stop_broadcast.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Broadcast interrupted")
        finally:
            remaining = len(targets)-sent-failed-skipped
            label = "STOPPED" if self.stop_broadcast.is_set() or remaining else "FINISHED"
            # Do not expose an ongoing admin report after access has been revoked.
            if self.is_admin(uid):
                try:
                    await self.say(msg, f"📢 BROADCAST {label}\n\nDelivered: {sent}\nFailed: {failed}\n"
                                   f"Skipped (banned): {skipped}\nNot attempted: {remaining}")
                except TelegramError:
                    log.warning("Could not send broadcast summary to admin")


def register_admin_panel(app, owner_id, reply):
    panel = AdminPanel(app, owner_id, reply,
                       os.getenv("ADMIN_DB_PATH", "admin_panel.sqlite3"))
    app.bot_data["admin_panel"] = panel
    # Runs before every existing handler. Unhandled updates continue unchanged.
    app.add_handler(TypeHandler(Update, panel.middleware), group=-100)
    if not owner_id:
        log.warning("Admin panel disabled: set OWNER_ID to your numeric Telegram user ID")
    return panel
