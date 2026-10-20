"""Offline admin tests using the real PTB package and mocked Telegram transport.
Run from project root: python -m unittest discover -s tests -v
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden, RetryAfter
from telegram.ext import ApplicationHandlerStop
from admin_panel import AdminPanel

OWNER, ADMIN, USER = 1001, 1002, 1003


def person(uid=USER):
    return NS(id=uid, full_name=f"Person <{uid}> & name", username=f"user{uid}", is_bot=False)


def message(uid=USER, text="hello", private=True, caption=None):
    chat = NS(id=uid if private else -900, type="private" if private else "supergroup",
              title="Group <test>", username=None)
    return NS(chat=chat, message_id=44, text=text, caption=caption, migrate_to_chat_id=None)


def update(uid=USER, text="hello", private=True, callback=None, caption=None):
    msg = message(uid, text, private, caption)
    query = NS(data=callback, answer=AsyncMock()) if callback else None
    return NS(effective_user=person(uid), effective_chat=msg.chat,
              effective_message=msg, callback_query=query, my_chat_member=None)


class AdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'state.sqlite3'
        self.reply = AsyncMock()
        self.tasks = []
        def create_task(coro, **kwargs):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task
        self.app = NS(create_task=create_task)
        self.panel = AdminPanel(self.app, OWNER, self.reply, self.path)
        self.bot = NS(username="kage_test_bot", send_message=AsyncMock(), copy_message=AsyncMock())
        self.ctx = NS(bot=self.bot)
        self.panel.store.execute("INSERT INTO admins VALUES(?,?,?)", (ADMIN,OWNER,time.time()))

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

    def state(self, uid=OWNER):
        return self.panel.pending[(uid,uid)]

    def track(self, uid, private=True):
        self.panel.store.track_user(person(uid), uid if private else None)

    async def prepare(self, action, target=USER, actor=OWNER):
        await self.panel.prepare_target(message(actor), actor, action, str(target))
        return self.state(actor)['nonce']

    async def test_private_user_is_tracked_once(self):
        await self.route(update(), False)
        await self.route(update(), False)
        self.assertEqual(self.panel.store.scalar('SELECT COUNT(*) FROM users'), 1)
        self.assertEqual(self.panel.store.recipients()[0]['private_chat_id'], USER)

    async def test_passive_group_members_are_not_counted(self):
        await self.route(update(private=False), False)
        self.assertEqual(self.panel.store.scalar('SELECT COUNT(*) FROM users'), 0)
        self.assertEqual(self.panel.store.scalar('SELECT COUNT(*) FROM groups_seen'), 1)

    async def test_group_command_user_not_broadcast_target(self):
        await self.route(update(text='!check +2348012345678', private=False), False)
        self.assertEqual(self.panel.store.scalar('SELECT COUNT(*) FROM users'), 1)
        self.assertEqual(len(self.panel.store.recipients()), 0)

    async def test_owner_panel_buttons(self):
        await self.route(update(OWNER, '!admin'))
        markup = self.reply.call_args.kwargs['reply_markup']
        data = {b.callback_data for row in markup.inline_keyboard for b in row}
        self.assertTrue({'adm:stats','adm:uptime','adm:users:0','adm:groups:0','adm:ban',
                         'adm:unban','adm:add','adm:remove','adm:broadcast'} <= data)

    async def test_admin_cannot_see_owner_buttons(self):
        await self.route(update(ADMIN, '/admin'))
        markup = self.reply.call_args.kwargs['reply_markup']
        data = {b.callback_data for row in markup.inline_keyboard for b in row}
        self.assertNotIn('adm:add',data)
        self.assertNotIn('adm:remove',data)

    async def test_nonadmin_commands_and_forged_buttons_denied(self):
        for event in [update(USER, '!stats'), update(USER, callback='adm:users:0')]:
            await self.route(event)
            if event.callback_query:
                event.callback_query.answer.assert_awaited_once()
        self.assertFalse(self.panel.pending)

    async def test_no_admin_panel_in_groups(self):
        await self.route(update(OWNER, '!users', False))
        self.assertIn('private chat', self.reply.call_args.args[1])
        self.assertNotIn('USERS LIST', self.reply.call_args.args[1])

    async def test_missing_owner_fails_closed(self):
        self.panel.owner_id = 0
        await self.route(update(ADMIN, '!admin'))
        self.assertFalse(self.panel.is_admin(ADMIN))

    async def test_existing_commands_continue(self):
        for cmd in ['!start', '!help', '!check +2348012345678', '/check +1234567890', '!ping']:
            await self.route(update(USER,cmd), False)
        self.reply.assert_not_awaited()

    async def test_other_bot_commands_not_intercepted(self):
        await self.route(update(OWNER,'/admin@another_bot'),False)
        self.reply.assert_not_awaited()
        await self.route(update(OWNER,'/admin@kage_test_bot'))

    async def test_ban_confirm_and_global_enforcement(self):
        nonce = await self.prepare('ban')
        self.assertFalse(self.panel.store.banned(USER))
        await self.panel.confirm(message(OWNER),OWNER,nonce,self.ctx)
        self.assertTrue(self.panel.store.banned(USER))
        await self.route(update(USER,'!check +2348012345678'))
        self.assertIn('banned',self.reply.call_args.args[1])
        event = update(USER,callback='help')
        await self.route(event)
        self.assertTrue(event.callback_query.answer.call_args.kwargs['show_alert'])

    async def test_unban_restores_normal_commands(self):
        self.panel.store.execute('INSERT INTO bans VALUES(?,?,?)',(USER,OWNER,time.time()))
        nonce = await self.prepare('unban')
        await self.panel.confirm(message(OWNER),OWNER,nonce,self.ctx)
        await self.route(update(USER,'!check +2348012345678'),False)

    async def test_owner_and_admin_cannot_be_banned(self):
        for target in (OWNER,ADMIN):
            await self.panel.prepare_target(message(OWNER),OWNER,'ban',str(target))
            self.assertNotIn((OWNER,OWNER),self.panel.pending)
            self.assertFalse(self.panel.store.banned(target))

    async def test_regular_admin_cannot_manage_roles(self):
        for action in ('add','remove'):
            await self.panel.prepare_target(message(ADMIN),ADMIN,action,str(USER))
            self.assertNotIn((ADMIN,ADMIN),self.panel.pending)
        self.assertFalse(self.panel.is_admin(USER))

    async def test_owner_adds_and_removes_admin(self):
        nonce = await self.prepare('add')
        await self.panel.confirm(message(OWNER),OWNER,nonce,self.ctx)
        self.assertTrue(self.panel.is_admin(USER))
        nonce = await self.prepare('remove')
        await self.panel.confirm(message(OWNER),OWNER,nonce,self.ctx)
        self.assertFalse(self.panel.is_admin(USER))

    async def test_cannot_remove_owner(self):
        await self.panel.prepare_target(message(OWNER),OWNER,'remove',str(OWNER))
        self.assertTrue(self.panel.is_admin(OWNER))
        self.assertNotIn((OWNER,OWNER),self.panel.pending)

    async def test_cannot_promote_banned_user(self):
        self.panel.store.execute('INSERT INTO bans VALUES(?,?,?)',(USER,OWNER,time.time()))
        await self.panel.prepare_target(message(OWNER),OWNER,'add',str(USER))
        self.assertFalse(self.panel.is_admin(USER))
        self.assertNotIn((OWNER,OWNER),self.panel.pending)

    async def test_confirmation_expires_and_replay_is_rejected(self):
        nonce = await self.prepare('ban')
        self.state()['expires'] = time.monotonic()-1
        await self.panel.confirm(message(OWNER),OWNER,nonce,self.ctx)
        self.assertFalse(self.panel.store.banned(USER))
        nonce = await self.prepare('ban')
        await self.panel.confirm(message(OWNER),OWNER,nonce,self.ctx)
        self.panel.store.execute('DELETE FROM bans WHERE user_id=?',(USER,))
        await self.panel.confirm(message(OWNER),OWNER,nonce,self.ctx)
        self.assertFalse(self.panel.store.banned(USER))

    async def test_confirmation_bound_to_actor(self):
        nonce = await self.prepare('ban')
        await self.panel.confirm(message(ADMIN),ADMIN,nonce,self.ctx)
        self.assertFalse(self.panel.store.banned(USER))

    async def test_revoked_admin_cannot_use_old_confirm_button(self):
        nonce = await self.prepare('ban',actor=ADMIN)
        self.panel.store.execute('DELETE FROM admins WHERE user_id=?',(ADMIN,))
        await self.route(update(ADMIN,callback='adm:confirm:'+nonce))
        self.assertFalse(self.panel.store.banned(USER))

    async def test_bad_target_input_no_mutation(self):
        for text in ['-4', 'abc','@test','0','1 OR 1=1','999999999999999999999999999']:
            await self.panel.prepare_target(message(OWNER),OWNER,'ban',text)
        self.assertEqual(self.panel.store.scalar('SELECT COUNT(*) FROM bans'),0)

    async def test_prompt_accepts_id_and_cancel_clears_it(self):
        await self.route(update(OWNER,'!ban'))
        self.assertEqual(self.state()['kind'],'input')
        await self.route(update(OWNER,str(USER)))
        self.assertEqual(self.state()['kind'],'confirm')
        await self.route(update(OWNER,'!cancel'))
        self.assertFalse(self.panel.pending)

    async def test_existing_command_cancels_admin_prompt(self):
        await self.route(update(OWNER,'!ban'))
        await self.route(update(OWNER,'!check +2348012345678'),False)
        self.assertFalse(self.panel.pending)

    async def test_caption_banner_command_not_captured_as_broadcast(self):
        await self.route(update(OWNER,'!broadcast'))
        await self.route(update(OWNER,None,caption='!setcheckbanner'),False)
        self.assertFalse(self.panel.pending)

    async def test_stats_uptime_and_paginated_users(self):
        for uid in range(2000,2020):
            self.track(uid)
        for command in ['!stats','!uptime','!users','!groups','!admins']:
            await self.route(update(OWNER,command))
        await self.route(update(OWNER,callback='adm:users:1'))
        self.assertIn('Page 2/',self.reply.call_args.args[1])
        await self.route(update(OWNER,callback='adm:users:99999'))
        self.assertIn('Page 3/',self.reply.call_args.args[1])
        await self.route(update(OWNER,callback='adm:users:not_a_page'))

    async def test_user_and_group_names_are_escaped(self):
        self.track(USER)
        await self.panel.show_page(message(OWNER),'users',0)
        self.assertIn('&lt;',self.reply.call_args.args[1])
        self.assertIn('&amp;',self.reply.call_args.args[1])

    async def test_group_membership_join_and_leave(self):
        event = update(private=False)
        event.my_chat_member = NS(chat=event.effective_chat,new_chat_member=NS(status='administrator'))
        await self.route(event,False)
        self.assertEqual(self.panel.store.scalar('SELECT active FROM groups_seen'),1)
        event.my_chat_member.new_chat_member.status='left'
        await self.route(event,False)
        self.assertEqual(self.panel.store.scalar('SELECT active FROM groups_seen'),0)

    async def test_persistent_users_bans_and_admins(self):
        self.track(USER)
        self.panel.store.execute('INSERT INTO bans VALUES(?,?,?)',(USER,OWNER,time.time()))
        other = AdminPanel(self.app,OWNER,self.reply,self.path)
        self.assertTrue(other.store.banned(USER))
        self.assertTrue(other.is_admin(ADMIN))
        self.assertEqual(other.store.scalar('SELECT COUNT(*) FROM users'),1)
        self.assertFalse(other.pending)

    async def test_broadcast_preview_does_not_send_to_audience(self):
        self.track(USER)
        await self.panel.prepare_broadcast(message(OWNER),OWNER,self.ctx,text='Announcement')
        self.bot.send_message.assert_awaited_once_with(chat_id=OWNER,text='Announcement',parse_mode=None)
        self.assertEqual(self.state()['action'],'broadcast')
        self.assertIsNone(self.panel.broadcast_task)

    async def test_broadcast_recipients_exclude_banned_groups_and_blocked(self):
        for uid in (USER,2001,2002):
            self.track(uid)
        self.track(2003,False)
        self.panel.store.execute('INSERT INTO bans VALUES(?,?,?)',(2001,OWNER,time.time()))
        self.panel.store.execute('UPDATE users SET unreachable=1 WHERE user_id=?',(2002,))
        self.assertEqual([r['user_id'] for r in self.panel.store.recipients()],[USER])

    async def test_confirmed_broadcast_and_summary(self):
        self.track(USER)
        await self.panel.prepare_broadcast(message(OWNER),OWNER,self.ctx,text='News')
        nonce=self.state()['nonce']
        self.bot.send_message.reset_mock()
        await self.panel.confirm(message(OWNER),OWNER,nonce,self.ctx)
        await self.panel.broadcast_task
        self.bot.send_message.assert_awaited_once_with(chat_id=USER,text='News',parse_mode=None)
        self.assertIn('Delivered: 1',self.reply.call_args.args[1])
        await self.panel.confirm(message(OWNER),OWNER,nonce,self.ctx)
        self.assertEqual(self.bot.send_message.await_count,1)

    async def test_media_broadcast_copies_content(self):
        self.track(USER)
        await self.panel.prepare_broadcast(message(OWNER),OWNER,self.ctx)
        self.bot.copy_message.assert_awaited_once()
        await self.panel.confirm(message(OWNER),OWNER,self.state()['nonce'],self.ctx)
        await self.panel.broadcast_task
        self.assertEqual(self.bot.copy_message.await_count,2)
        self.assertEqual(self.bot.copy_message.call_args.kwargs['chat_id'],USER)

    async def test_forbidden_recipient_marked_and_reset_on_return(self):
        self.track(USER)
        self.bot.send_message.side_effect=Forbidden('blocked')
        await self.panel.run_broadcast(self.bot,message(OWNER),OWNER,{'text':'News'},self.panel.store.recipients())
        self.assertEqual(self.panel.store.scalar('SELECT unreachable FROM users WHERE user_id=?',(USER,)),1)
        self.track(USER)
        self.assertEqual(self.panel.store.scalar('SELECT unreachable FROM users WHERE user_id=?',(USER,)),0)

    async def test_retry_after_retries_safely(self):
        self.track(USER)
        self.bot.send_message.side_effect=[RetryAfter(0),None]
        await self.panel.run_broadcast(self.bot,message(OWNER),OWNER,{'text':'News'},self.panel.store.recipients())
        self.assertEqual(self.bot.send_message.await_count,2)
        self.assertIn('Delivered: 1',self.reply.call_args.args[1])

    async def test_broadcast_stop_and_permission_revocation(self):
        self.track(USER)
        self.panel.stop_broadcast.set()
        await self.panel.run_broadcast(self.bot,message(OWNER),OWNER,{'text':'News'},self.panel.store.recipients())
        self.bot.send_message.assert_not_awaited()
        self.panel.stop_broadcast.clear()
        self.panel.store.execute('DELETE FROM admins WHERE user_id=?',(ADMIN,))
        await self.panel.run_broadcast(self.bot,message(ADMIN),ADMIN,{'text':'News'},self.panel.store.recipients())
        self.bot.send_message.assert_not_awaited()

    async def test_new_command_invalidates_old_confirmation(self):
        nonce = await self.prepare('ban')
        await self.route(update(OWNER,'!unban invalid_id'))
        await self.panel.confirm(message(OWNER),OWNER,nonce,self.ctx)
        self.assertFalse(self.panel.store.banned(USER))

    async def test_prompt_timeout_restores_normal_messages(self):
        await self.route(update(OWNER,'!ban'))
        self.state()['expires'] = time.monotonic()-1
        await self.route(update(OWNER,str(USER)),False)
        self.assertFalse(self.panel.pending)

    async def test_broadcast_cancel_before_confirmation_sends_nothing(self):
        self.track(USER)
        await self.panel.prepare_broadcast(message(OWNER),OWNER,self.ctx,text='News')
        nonce = self.state()['nonce']
        self.bot.send_message.reset_mock()
        await self.route(update(OWNER,'!cancel'))
        await self.panel.confirm(message(OWNER),OWNER,nonce,self.ctx)
        self.bot.send_message.assert_not_awaited()
        self.assertIsNone(self.panel.broadcast_task)

    async def test_real_telegram_handler_registration(self):
        from telegram.ext import Application, TypeHandler
        from admin_panel import register_admin_panel
        app = Application.builder().token('123456:testtoken').build()
        with patch.dict('os.environ',{'ADMIN_DB_PATH':str(self.path)}):
            panel = register_admin_panel(app,OWNER,self.reply)
        self.assertIs(app.bot_data['admin_panel'],panel)
        self.assertIsInstance(app.handlers[-100][0],TypeHandler)
        self.assertEqual(app.handlers[-100][0].callback,panel.middleware)

    async def test_storage_error_blocks_update(self):
        import sqlite3
        with self.assertLogs('admin_panel', level='ERROR'):
            with patch.object(self.panel.store,'track_user',side_effect=sqlite3.OperationalError('test unavailable')):
                await self.route(update(USER,'!check 2348012345678'))
        self.assertIn('unavailable',self.reply.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
