"""Offline force-join tests; Telegram transport is mocked."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop

from admin_panel import AdminPanel
from test_admin_panel import OWNER, ADMIN, USER, update


def chat(chat_id=-1001234567890, username="requiredchat", chat_type="channel"):
    return NS(id=chat_id, username=username, type=chat_type,
              title="Required <Chat>")


class ForceJoinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.reply = AsyncMock()
        self.tasks = []
        def create_task(coro, **kwargs):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task
        self.app = NS(create_task=create_task)
        self.panel = AdminPanel(self.app, OWNER, self.reply, self.path)
        self.bot = NS(id=777, username="kage_test_bot",
                      send_message=AsyncMock(), copy_message=AsyncMock())
        self.ctx = NS(bot=self.bot)
        self.panel.store.execute(
            "INSERT INTO admins VALUES(?,?,?)", (ADMIN,OWNER,time.time()))

    async def asyncSetUp(self):
        self.required = chat()
        self.bot.get_chat = self._async(self.required)
        self.bot.get_chat_member = self._async(NS(status="administrator"))

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.temp.cleanup()

    async def route(self, event, stopped=True):
        if stopped:
            with self.assertRaises(ApplicationHandlerStop):
                await self.panel.middleware(event, self.ctx)
        else:
            await self.panel.middleware(event, self.ctx)

    @staticmethod
    def _async(value=None, side_effect=None):
        from unittest.mock import AsyncMock
        return AsyncMock(return_value=value, side_effect=side_effect)

    def add_record(self, obj=None, link=None):
        obj = obj or self.required
        link = link or f"https://t.me/{obj.username}"
        self.panel.store.execute("""
            INSERT INTO force_join_chats
            (chat_id,title,username,invite_link,chat_type,added_by,added_at)
            VALUES(?,?,?,?,?,?,?)
        """, (obj.id,obj.title,obj.username,link,obj.type,OWNER,time.time()))

    async def test_owner_adds_public_force_join_chat(self):
        await self.route(update(OWNER, "!addforcejoin @requiredchat"))
        row = self.panel.store.force_join_chats()[0]
        self.assertEqual(row["chat_id"], self.required.id)
        self.assertEqual(row["invite_link"], "https://t.me/requiredchat")
        self.bot.get_chat.assert_awaited_once_with("@requiredchat")
        self.bot.get_chat_member.assert_awaited_once_with(self.required.id, self.bot.id)

    async def test_owner_adds_private_chat_with_invite(self):
        private = chat(username=None, chat_type="supergroup")
        self.bot.get_chat.return_value = private
        await self.route(update(
            OWNER, f"!addforcejoin {private.id} https://t.me/+Secret_invite-1"))
        row = self.panel.store.force_join_chats()[0]
        self.assertEqual(row["invite_link"], "https://t.me/+Secret_invite-1")

    async def test_private_chat_requires_invite_link(self):
        self.bot.get_chat.return_value = chat(username=None)
        await self.route(update(OWNER, f"!addforcejoin {self.required.id}"))
        self.assertFalse(self.panel.store.force_join_chats())

    async def test_bot_must_be_admin_before_chat_is_added(self):
        self.bot.get_chat_member.return_value = NS(status="member")
        await self.route(update(OWNER, "!addforcejoin @requiredchat"))
        self.assertFalse(self.panel.store.force_join_chats())

    async def test_unverifiable_chat_is_not_added(self):
        self.bot.get_chat.side_effect = BadRequest("not found")
        await self.route(update(OWNER, "!addforcejoin @requiredchat"))
        self.assertFalse(self.panel.store.force_join_chats())

    async def test_only_owner_can_manage_force_join(self):
        for uid in (ADMIN, USER):
            await self.route(update(uid, "!addforcejoin @requiredchat"))
        self.assertFalse(self.panel.store.force_join_chats())

    async def test_existing_and_new_users_are_blocked_until_joined(self):
        self.add_record()
        self.bot.get_chat_member.return_value = NS(status="left")
        for uid in (USER, 8888):
            event = update(uid, "!check +2348012345678")
            await self.route(event)
            markup = self.reply.call_args.kwargs["reply_markup"]
            self.assertEqual(markup.inline_keyboard[-1][0].callback_data, "fj:check")
            self.assertEqual(markup.inline_keyboard[0][0].url,
                             "https://t.me/requiredchat")

    async def test_joined_user_continues_to_existing_handler(self):
        self.add_record()
        self.bot.get_chat_member.return_value = NS(status="member")
        await self.route(update(USER, "!check +2348012345678"), False)

    async def test_owner_and_admin_bypass_force_join(self):
        self.add_record()
        self.bot.get_chat_member.return_value = NS(status="left")
        await self.route(update(OWNER, "!stats"))
        await self.route(update(ADMIN, "!stats"))

    async def test_verify_button_rechecks_and_then_allows_use(self):
        self.add_record()
        self.bot.get_chat_member.return_value = NS(status="member")
        event = update(USER, callback="fj:check")
        await self.route(event)
        event.callback_query.answer.assert_awaited_once_with("Membership verified.")
        self.assertIn("Membership verified", self.reply.call_args.args[1])

    async def test_verify_button_still_blocks_missing_user(self):
        self.add_record()
        self.bot.get_chat_member.return_value = NS(status="left")
        event = update(USER, callback="fj:check")
        await self.route(event)
        self.assertTrue(event.callback_query.answer.call_args.kwargs["show_alert"])

    async def test_owner_removes_by_username_and_enforcement_stops(self):
        self.add_record()
        await self.route(update(OWNER, "!removeforcejoin @requiredchat"))
        self.assertFalse(self.panel.store.force_join_chats())
        await self.route(update(USER, "!check +2348012345678"), False)

    async def test_force_join_records_survive_restart(self):
        self.add_record()
        other = type(self.panel)(self.app, OWNER, self.reply, self.path)
        self.assertEqual(other.store.force_join_chats()[0]["chat_id"],
                         self.required.id)
