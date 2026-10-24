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
from datetime import datetime, timezone

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
    "forcejoin": "forcejoin", "addforcejoin": "forcejoin_add",
    "removeforcejoin": "forcejoin_remove",
}


# Separate from the general/check banners. Only the owner can change these.
PHOTO_COMMANDS = {
    "setadminpic": "panel", "setstatspic": "stats", "setuptimepic": "uptime",
    "setuserspic": "users", "setgroupspic": "groups", "setadminspic": "admins",
    "setbanpic": "ban", "setunbanpic": "unban", "setaddadminpic": "add",
    "setremoveadminpic": "remove", "setbroadcastpic": "broadcast",
    "setforcejoinpic": "forcejoin",
}
PHOTO_LABELS = {
    "panel": "Admin Panel", "stats": "Bot Stats", "uptime": "Uptime",
    "users": "Users List", "groups": "Groups", "admins": "Admins List",
    "ban": "Ban User", "unban": "Unban User", "add": "Add Admin",
    "remove": "Remove Admin", "broadcast": "Broadcast",
    "forcejoin": "Force Join",
}
COMMANDS.update({name: "setpic:" + category for name, category in PHOTO_COMMANDS.items()})


class Store:
    """Short, parameterized local transactions; no open transaction spans an await."""
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS activity_totals (
                    day_utc TEXT NOT NULL, kind TEXT NOT NULL,
                    total INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day_utc, kind)
                );
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
                CREATE TABLE IF NOT EXISTS admin_photos (
                    category TEXT PRIMARY KEY, photo_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS groups_seen (
                    chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL,
                    username TEXT, active INTEGER NOT NULL, last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS force_join_chats (
                    chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL,
                    username TEXT, invite_link TEXT NOT NULL, chat_type TEXT NOT NULL,
                    added_by INTEGER NOT NULL, added_at REAL NOT NULL
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

    def get_photo(self, category):
        rows = self.rows("SELECT photo_id FROM admin_photos WHERE category=?", (category,))
        return rows[0]["photo_id"] if rows else None

    def set_photo(self, category, photo_id):
        if photo_id is None:
            self.execute("DELETE FROM admin_photos WHERE category=?", (category,))
        else:
            self.execute("""INSERT INTO admin_photos(category,photo_id) VALUES(?,?)
                ON CONFLICT(category) DO UPDATE SET photo_id=excluded.photo_id""",
                (category,photo_id))

    def recipients(self):
        return self.rows("""
            SELECT user_id,private_chat_id FROM users
            WHERE private_chat_id IS NOT NULL AND unreachable=0
                AND user_id NOT IN (SELECT user_id FROM bans)
            ORDER BY user_id
        """)

    def count_activity(self, kind):
        """Anonymous daily counts only: never store messages or checked numbers."""
        if kind not in ("interactions", "check_requests"):
            raise ValueError("Unknown activity counter")
        day = datetime.now(timezone.utc).date().isoformat()
        self.execute("""INSERT INTO activity_totals(day_utc,kind,total) VALUES(?,?,1)
            ON CONFLICT(day_utc,kind) DO UPDATE SET total=total+1""", (day, kind))

    def activity_total(self, kind, day=None):
        if day is None:
            return self.scalar("SELECT COALESCE(SUM(total),0) FROM activity_totals WHERE kind=?", (kind,))
        return self.scalar("SELECT COALESCE(SUM(total),0) FROM activity_totals WHERE kind=? AND day_utc=?", (kind,day))

    def force_join_chats(self):
        return self.rows("SELECT * FROM force_join_chats ORDER BY added_at, chat_id")


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

    async def say(self, msg, text, markup=BACK, *, category=None):
        photo_id = None
        if category:
            photo_id = self.store.get_photo(category)
            if photo_id is None and category != "panel":
                photo_id = self.store.get_photo("panel")
        if photo_id:
            # Preserve the same monospace look, including on photo captions.
            # Telegram captions are short; long lists keep their existing splitter.
            short = len(text.encode("utf-16-le")) // 2 <= 1024
            try:
                if short:
                    return await msg.reply_photo(
                        photo=photo_id, caption=f"<pre>{escape(text, quote=False)}</pre>",
                        parse_mode="HTML", reply_markup=markup)
                await msg.reply_photo(photo=photo_id)
            except TelegramError as exc:
                # A deleted/invalid image must never hide buttons or admin output.
                log.warning("Admin category photo failed (%s): %s", category, type(exc).__name__)
        # The shared formatter reads HTML; escape untrusted names/group titles first.
        return await self.reply(msg, escape(text), reply_markup=markup)

    async def set_picture(self, msg, uid, category, args):
        if not self.owner_id or uid != self.owner_id:
            await self.say(msg, "🚫 Only the owner can change admin-category pictures.")
            return
        if category not in PHOTO_LABELS:
            await self.say(msg, "Unknown admin picture category.")
            return
        if msg.chat.type != "private":
            await self.say(msg, "🔒 Send picture commands in a private chat with this bot.", None)
            return
        command = next(name for name, value in PHOTO_COMMANDS.items() if value == category)
        photos = getattr(msg, "photo", None)
        if args.lower() == "clear" and not photos:
            self.store.set_photo(category, None)
            fallback = "This category will use the main Admin Panel picture, if set." if category != "panel" else "Categories with their own pictures are unchanged."
            await self.say(msg, f"✅ {PHOTO_LABELS[category]} picture cleared.\n{fallback}")
            return
        if args or not photos:
            await self.say(msg, f"📸 SET {PHOTO_LABELS[category].upper()} PICTURE\n\n"
                           "Attach a photo (not a document).\n"
                           f"Set its caption to: !{command}\n"
                           "Send it here to save it.\n\n"
                           f"To remove this setting, send: !{command} clear\n"
                           "Your general and check banners are not changed.")
            return
        self.store.set_photo(category, photos[-1].file_id)
        await self.say(msg, f"✅ {PHOTO_LABELS[category]} picture saved.\n"
                       "Other category pictures and general/check banners are unchanged.",
                       category=category)

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
            if not user.is_bot:
                self.store.count_activity("interactions")

        if user.id != self.owner_id and self.store.banned(user.id):
            if query:
                await query.answer("You are banned from using this bot.", show_alert=True)
            elif interacting:
                await self.say(msg, "🚫 You are banned from using this bot.", None)
            raise ApplicationHandlerStop

        force_callback = bool(query and (query.data or "") == "fj:check")
        if not self.is_admin(user.id) and (interacting or force_callback):
            missing = await self.missing_force_joins(ctx.bot, user.id)
            if missing:
                if query:
                    await query.answer("Join every required chat first.", show_alert=True)
                await self.force_join_notice(msg, missing)
                raise ApplicationHandlerStop
            if force_callback:
                await query.answer("Membership verified.")
                await self.say(msg, "✅ Membership verified.\n\nYou can now use the bot.", None)
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

        # Count only text check commands that reach the existing bot handlers.
        # This is a request count, not completed checks or a count of numbers.
        if not query and msg.text:
            parts = msg.text.strip().split(None, 1)
            token = parts[0] if parts else ""
            slash_check = re.fullmatch(r"/check(?:@([a-z0-9_]+))?", token, re.I)
            addressed_here = slash_check and (not slash_check[1] or
                slash_check[1].lower() == (ctx.bot.username or "").lower())
            if token.lower() == "!check" or addressed_here:
                self.store.count_activity("check_requests")

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
                elif state["action"] == "forcejoin_add":
                    await self.add_force_join(msg, user.id, text, ctx)
                elif state["action"] == "forcejoin_remove":
                    await self.remove_force_join(msg, user.id, text)
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
            rows.append([("🔐 Force Join", "forcejoin")])
        rows.append([("📢 Broadcast", "broadcast")])
        await self.say(msg, "👑 KAGE X — ADMIN PANEL\n\nChoose a category below.\n"
                       "Changes and broadcasts require confirmation.", keyboard(rows), category="panel")

    async def command(self, msg, uid, cmd, args, ctx):
        # A new panel command invalidates older confirmation buttons.
        self.pending.pop((uid, msg.chat.id), None)
        if cmd.startswith("setpic:"):
            await self.set_picture(msg, uid, cmd.split(":", 1)[1], args)
        elif cmd == "cancel":
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
        elif cmd == "forcejoin":
            await self.show_force_join(msg, uid)
        elif cmd == "forcejoin_add":
            if args:
                await self.add_force_join(msg, uid, args, ctx)
            else:
                await self.prompt_force_join(msg, uid, "forcejoin_add")
        elif cmd == "forcejoin_remove":
            if args:
                await self.remove_force_join(msg, uid, args)
            else:
                await self.prompt_force_join(msg, uid, "forcejoin_remove")
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

    async def missing_force_joins(self, bot, user_id):
        missing = []
        for row in self.store.force_join_chats():
            try:
                member = await bot.get_chat_member(row["chat_id"], user_id)
                joined = member.status in ("member", "administrator", "creator") or (
                    member.status == "restricted" and getattr(member, "is_member", False))
            except TelegramError as exc:
                log.warning("Force-join membership check failed for %s: %s",
                            row["chat_id"], type(exc).__name__)
                joined = False
            if not joined:
                missing.append(row)
        return missing

    async def force_join_notice(self, msg, missing):
        rows = []
        for item in missing:
            kind = "Channel" if item["chat_type"] == "channel" else "Group"
            rows.append([InlineKeyboardButton(
                f"➕ Join {kind}: {item['title'][:35]}", url=item["invite_link"])])
        rows.append([InlineKeyboardButton("✅ I Joined — Verify", callback_data="fj:check")])
        await self.reply(
            msg,
            escape("🔐 FORCE JOIN REQUIRED\n\nJoin every channel/group below first, "
                   "then tap “I Joined — Verify”."),
            reply_markup=InlineKeyboardMarkup(rows))

    async def show_force_join(self, msg, uid):
        if uid != self.owner_id:
            await self.say(msg, "🚫 Only the owner can manage force-join chats.")
            return
        records = self.store.force_join_chats()
        lines = ["🔐 FORCE JOIN", "", f"Required chats: {len(records)}"]
        for item in records:
            username = f"@{item['username']}" if item["username"] else "Private invite"
            lines += ["", item["title"][:80], f"ID: {item['chat_id']}", username]
        if not records:
            lines += ["", "No force-join chat is configured."]
        rows = [
            [("➕ Add Chat", "forcejoin_add"), ("➖ Remove Chat", "forcejoin_remove")],
            [("⬅ Admin Panel", "panel")],
        ]
        await self.say(msg, "\n".join(lines), keyboard(rows), category="forcejoin")

    async def prompt_force_join(self, msg, uid, action):
        if uid != self.owner_id:
            await self.say(msg, "🚫 Only the owner can manage force-join chats.")
            return
        self.put_state(msg, uid, kind="input", action=action)
        if action == "forcejoin_add":
            text = (
                "➕ ADD FORCE-JOIN CHAT\n\n"
                "Public chat:\n!addforcejoin @username\n\n"
                "Private chat:\n!addforcejoin -1001234567890 https://t.me/+invite\n\n"
                "The bot must already be an administrator in that channel/group.\n"
                "!cancel stops this action."
            )
        else:
            text = (
                "➖ REMOVE FORCE-JOIN CHAT\n\n"
                "Send its numeric chat ID or public @username.\n"
                "Example: !removeforcejoin @username\n\n"
                "!cancel stops this action."
            )
        await self.say(msg, text, CANCEL, category="forcejoin")

    async def add_force_join(self, msg, uid, text, ctx):
        if uid != self.owner_id:
            await self.say(msg, "🚫 Only the owner can manage force-join chats.")
            return
        parts = text.split(maxsplit=1)
        target = parts[0]
        supplied_link = parts[1].strip() if len(parts) == 2 else ""
        if not (re.fullmatch(r"-100[0-9]{5,16}", target) or
                re.fullmatch(r"@[A-Za-z0-9_]{5,32}", target)):
            await self.say(msg, "Send a public @username, or a -100… chat ID followed by its invite link.", CANCEL)
            return
        try:
            chat = await ctx.bot.get_chat(int(target) if target.startswith("-100") else target)
            if chat.type not in ("group", "supergroup", "channel"):
                raise ValueError("unsupported chat type")
            bot_member = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
            if bot_member.status not in ("administrator", "creator"):
                await self.say(msg, "Make the bot an administrator in that channel/group first.", CANCEL)
                return
        except (TelegramError, ValueError) as exc:
            log.warning("Could not add force-join chat %s: %s", target, type(exc).__name__)
            await self.say(msg, "Telegram could not verify that chat. Check the ID/username and make the bot an administrator there.", CANCEL)
            return
        username = getattr(chat, "username", None)
        link = f"https://t.me/{username}" if username else supplied_link
        if not re.fullmatch(r"https://t\.me/(?:\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+)", link or "") and not username:
            await self.say(msg, "A private chat needs a valid https://t.me/+… invite link.", CANCEL)
            return
        self.store.execute("""
            INSERT INTO force_join_chats
                (chat_id,title,username,invite_link,chat_type,added_by,added_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
                username=excluded.username,invite_link=excluded.invite_link,
                chat_type=excluded.chat_type,added_by=excluded.added_by,
                added_at=excluded.added_at
        """, (chat.id, chat.title or username or str(chat.id), username, link,
              chat.type, uid, time.time()))
        self.pending.pop((uid, msg.chat.id), None)
        await self.say(msg, f"✅ Force join enabled for:\n{chat.title or username}\nID: {chat.id}",
                       category="forcejoin")

    async def remove_force_join(self, msg, uid, text):
        if uid != self.owner_id:
            await self.say(msg, "🚫 Only the owner can manage force-join chats.")
            return
        target = text.strip()
        if re.fullmatch(r"-100[0-9]{5,16}", target):
            where, value = "chat_id=?", int(target)
        elif re.fullmatch(r"@[A-Za-z0-9_]{5,32}", target):
            where, value = "LOWER(username)=?", target[1:].lower()
        else:
            await self.say(msg, "Send one configured -100… chat ID or public @username.", CANCEL)
            return
        found = self.store.scalar(f"SELECT COUNT(*) FROM force_join_chats WHERE {where}", (value,))
        if not found:
            await self.say(msg, "That chat is not in the force-join list.", category="forcejoin")
            return
        self.store.execute(f"DELETE FROM force_join_chats WHERE {where}", (value,))
        self.pending.pop((uid, msg.chat.id), None)
        await self.say(msg, f"✅ Force join removed for {target}.", category="forcejoin")

    async def show(self, msg, uid, action, page):
        self.pending.pop((uid, msg.chat.id), None)
        if action == "stats":
            s = self.store
            values = {
                "Received interactions": s.activity_total("interactions"),
                "Interactions today (UTC)": s.activity_total("interactions", datetime.now(timezone.utc).date().isoformat()),
                "Check requests": s.activity_total("check_requests"),
                "Users tracked": s.scalar("SELECT COUNT(*) FROM users"),
                "Active in last 24h": s.scalar("SELECT COUNT(*) FROM users WHERE last_seen>=?", (time.time()-86400,)),
                "Broadcast audience": len(s.recipients()),
                "Banned IDs": s.scalar("SELECT COUNT(*) FROM bans"),
                "Admins (incl. owner)": s.scalar("SELECT COUNT(*) FROM admins WHERE user_id!=?", (self.owner_id,))+1,
                "Tracked active groups": s.scalar("SELECT COUNT(*) FROM groups_seen WHERE active=1"),
                "Force-join chats": s.scalar("SELECT COUNT(*) FROM force_join_chats"),
            }
            await self.say(msg, "📊 BOT STATS\n\n" + "\n".join(f"{k}: {v}" for k,v in values.items()) +
                           "\n\nCounts begin when this module is installed.\n"
                           "Activity totals start with the database update.\n"
                           "Check requests include invalid inputs, not completed results.\n"
                           "Checked numbers and message text are not saved in this database.\n"
                           "These are local counts, not Telegram's monthly-user badge.", category="stats")
        elif action == "uptime":
            seconds = int(time.monotonic()-self.started)
            days, remainder = divmod(seconds, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, seconds = divmod(remainder, 60)
            await self.say(msg, f"⏱ UPTIME\n\n{days}d {hours}h {minutes}m {seconds}s\n\nSince this bot process started.", category="uptime")
        elif action == "admins":
            rows = self.store.rows("SELECT a.user_id,u.name,u.username FROM admins a LEFT JOIN users u USING(user_id) WHERE a.user_id!=? ORDER BY a.added_at", (self.owner_id,))
            lines = ["👑 ADMINS", f"Owner: {self.owner_id}"]
            for row in rows:
                lines.append(f"{row['user_id']} · {(row['name'] or 'Not yet seen')[:70]}")
            await self.say(msg, "\n".join(lines), category="admins")
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
        await self.say(msg, "\n".join(lines), keyboard(rows), category=action)

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
                       "Use Users List to find an ID.\n\n!cancel stops this action. Prompt expires in 10 minutes.", CANCEL, category=action)

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
                       keyboard([[("✅ Confirm", "confirm:"+nonce), ("✖ Cancel", "cancel")]]), category=action)

    def broadcasting(self):
        return self.broadcast_task is not None and not self.broadcast_task.done()

    async def prompt_broadcast(self, msg, uid):
        if self.broadcasting():
            await self.say(msg, "📢 A broadcast is already running.", keyboard([[("🛑 Stop broadcast", "stop")],[("⬅ Admin Panel", "panel")]]), category="broadcast")
            return
        self.put_state(msg, uid, kind="input", action="broadcast")
        await self.say(msg, "📢 BROADCAST\n\nSend the message or media to broadcast.\n"
                       "You will see a preview before anything is sent.\n\n"
                       "Recipients: tracked private-chat users who are not banned\n"
                       "or known to have blocked the bot. Groups are not included.\n"
                       "!cancel stops this action.", CANCEL, category="broadcast")

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
        await self.say(msg, f"📢 Broadcast preview sent separately above.\n"
                       f"Category pictures are not included in the broadcast.\n\nCurrent eligible recipients: {audience}\n"
                       "Nothing has been broadcast yet. Send it?\n"
                       "Confirmation expires in 10 minutes.",
                       keyboard([[("📢 Send broadcast", "confirm:"+nonce), ("✖ Cancel", "cancel")]]), category="broadcast")

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
                           keyboard([[("🛑 Stop broadcast", "stop")],[("⬅ Admin Panel", "panel")]]), category="broadcast")
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
        await self.say(msg, f"✅ {labels[action]} user {target}.", category=action)

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
                                   f"Skipped (banned): {skipped}\nNot attempted: {remaining}", category="broadcast")
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
