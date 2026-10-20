"""Offline tests for owner-only category pictures; no Telegram network calls."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import AsyncMock

from telegram.error import BadRequest, Forbidden
from admin_panel import AdminPanel, PHOTO_COMMANDS, PHOTO_LABELS
from test_admin_panel import OWNER, ADMIN, USER, person, message, update


def event(uid=OWNER, text='!admin', callback=None, photo_ids=(), private=True, caption=None):
    obj = update(uid, text, private, callback, caption)
    obj.effective_message.photo = [NS(file_id=fid) for fid in photo_ids]
    obj.effective_message.reply_photo = AsyncMock()
    return obj


class PhotoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'admin.sqlite3'
        self.reply = AsyncMock()
        self.app = NS(create_task=lambda *a, **k: None)
        self.panel = AdminPanel(self.app,OWNER,self.reply,self.path)
        self.panel.store.execute('INSERT INTO admins VALUES(?,?,?)',(ADMIN,OWNER,1))
        self.bot = NS(username='kage_test_bot',send_message=AsyncMock(),copy_message=AsyncMock())
        self.ctx = NS(bot=self.bot)

    def tearDown(self):
        self.temp.cleanup()

    async def route(self, obj, stopped=True):
        from telegram.ext import ApplicationHandlerStop
        if stopped:
            with self.assertRaises(ApplicationHandlerStop):
                await self.panel.middleware(obj,self.ctx)
        else:
            await self.panel.middleware(obj,self.ctx)
        return obj.effective_message

    async def test_all_eleven_photo_commands_work_from_photo_captions(self):
        self.assertEqual(len(PHOTO_COMMANDS),11)
        for command, category in PHOTO_COMMANDS.items():
            obj=event(text=None,caption='!'+command,photo_ids=('thumbnail','full-'+category))
            msg=await self.route(obj)
            self.assertEqual(self.panel.store.get_photo(category),'full-'+category)
            self.assertEqual(msg.reply_photo.call_args.kwargs['photo'],'full-'+category)
        self.assertEqual(self.panel.store.scalar('SELECT COUNT(*) FROM admin_photos'),11)

    async def test_owner_only_even_for_existing_admins(self):
        for uid in (USER,ADMIN):
            await self.route(event(uid,text=None,caption='!setstatspic',photo_ids=('bad',)))
        self.assertIsNone(self.panel.store.get_photo('stats'))
        await self.panel.set_picture(message(USER),USER,'stats','clear')
        self.assertIsNone(self.panel.store.get_photo('stats'))

    async def test_owner_must_be_configured(self):
        self.panel.owner_id=0
        await self.route(event(ADMIN,text=None,caption='!setadminpic',photo_ids=('bad',)))
        self.assertIsNone(self.panel.store.get_photo('panel'))

    async def test_no_picture_settings_in_group(self):
        await self.route(event(text=None,caption='!setadminpic',photo_ids=('bad',),private=False))
        self.assertIsNone(self.panel.store.get_photo('panel'))

    async def test_without_photo_shows_instructions(self):
        await self.route(event(text='!setadminpic'))
        text=self.reply.call_args.args[1]
        self.assertIn('!setadminpic',text)
        self.assertIn('Attach a photo',text)
        self.assertIsNone(self.panel.store.get_photo('panel'))

    async def test_image_document_does_not_set_photo(self):
        obj=event(text=None,caption='!setuserspic')
        obj.effective_message.document=NS(file_id='document-id')
        await self.route(obj)
        self.assertIsNone(self.panel.store.get_photo('users'))

    async def test_slash_caption_and_username_and_case(self):
        await self.route(event(text=None,caption='/SetStatsPic@kage_test_bot',photo_ids=('stats',)))
        self.assertEqual(self.panel.store.get_photo('stats'),'stats')
        await self.route(event(text=None,caption='/setstatspic@another_bot',photo_ids=('other',)),False)
        self.assertEqual(self.panel.store.get_photo('stats'),'stats')

    async def test_setting_one_category_does_not_overwrite_others(self):
        self.panel.store.set_photo('panel','main')
        self.panel.store.set_photo('stats','old-stats')
        self.panel.store.set_photo('users','users')
        await self.route(event(text=None,caption='!setstatspic',photo_ids=('new-stats',)))
        self.assertEqual(self.panel.store.get_photo('panel'),'main')
        self.assertEqual(self.panel.store.get_photo('stats'),'new-stats')
        self.assertEqual(self.panel.store.get_photo('users'),'users')

    async def test_main_panel_and_each_category_route_to_own_picture(self):
        for cat in PHOTO_LABELS:
            self.panel.store.set_photo(cat,'pic-'+cat)
        commands={'panel':'!admin','stats':'!stats','uptime':'!uptime','users':'!users',
                  'groups':'!groups','admins':'!admins','ban':'!ban','unban':'!unban',
                  'add':'!addadmin','remove':'!removeadmin','broadcast':'!broadcast'}
        for cat,command in commands.items():
            obj=event(text=command)
            msg=await self.route(obj)
            self.assertEqual(msg.reply_photo.call_args.kwargs['photo'],'pic-'+cat,cat)
            self.assertIsNotNone(msg.reply_photo.call_args.kwargs['reply_markup'])

    async def test_category_buttons_work_with_photos(self):
        self.panel.store.set_photo('stats','stats-photo')
        obj=event(callback='adm:stats')
        msg=await self.route(obj)
        self.assertEqual(msg.reply_photo.call_args.kwargs['photo'],'stats-photo')
        obj.callback_query.answer.assert_awaited_once()

    async def test_unset_category_inherits_main_picture(self):
        self.panel.store.set_photo('panel','main')
        obj=event(text='!stats')
        msg=await self.route(obj)
        self.assertEqual(msg.reply_photo.call_args.kwargs['photo'],'main')

    async def test_no_admin_photos_preserves_text_only(self):
        obj=event(text='!stats')
        msg=await self.route(obj)
        msg.reply_photo.assert_not_awaited()
        self.reply.assert_awaited_once()

    async def test_short_captions_use_escaped_monospace(self):
        self.panel.store.set_photo('stats','stats')
        msg=event().effective_message
        await self.panel.say(msg,'Person <a> & b',category='stats')
        kw=msg.reply_photo.call_args.kwargs
        self.assertEqual(kw['caption'],'<pre>Person &lt;a&gt; &amp; b</pre>')
        self.assertEqual(kw['parse_mode'],'HTML')
        self.reply.assert_not_awaited()

    async def test_long_output_uses_photo_then_original_text_helper(self):
        self.panel.store.set_photo('users','users')
        msg=event().effective_message
        text='👤 A <name>\n'*200
        await self.panel.say(msg,text,category='users')
        msg.reply_photo.assert_awaited_once_with(photo='users')
        self.assertIn('&lt;name&gt;',self.reply.call_args.args[1])
        self.assertIsNotNone(self.reply.call_args.kwargs['reply_markup'])

    async def test_emoji_caption_limit_is_respected(self):
        self.panel.store.set_photo('stats','stats')
        msg=event().effective_message
        await self.panel.say(msg,'🟢'*513,category='stats')
        self.assertNotIn('caption',msg.reply_photo.call_args.kwargs)
        self.reply.assert_awaited_once()

    async def test_invalid_photo_falls_back_to_text_and_keeps_buttons(self):
        self.panel.store.set_photo('stats','invalid-photo')
        obj=event(text='!stats')
        obj.effective_message.reply_photo.side_effect=BadRequest('invalid image')
        with self.assertLogs('admin_panel',level='WARNING'):
            await self.route(obj)
        self.reply.assert_awaited_once()
        self.assertIsNotNone(self.reply.call_args.kwargs['reply_markup'])
        self.assertIn('BOT STATS',self.reply.call_args.args[1])

    async def test_general_and_check_banner_commands_still_pass_through(self):
        self.panel.store.set_photo('panel','admin')
        for caption in ('!setbanner','!setcheckbanner'):
            await self.route(event(text=None,caption=caption,photo_ids=('original',)),False)
        self.assertEqual(self.panel.store.scalar('SELECT COUNT(*) FROM admin_photos'),1)

    async def test_photo_settings_are_persistent(self):
        self.panel.store.set_photo('stats','saved')
        self.panel.store.track_user(person(USER),USER)
        self.panel.store.execute('INSERT INTO bans VALUES(?,?,?)',(USER,OWNER,1))
        restarted=AdminPanel(self.app,OWNER,self.reply,self.path)
        self.assertEqual(restarted.store.get_photo('stats'),'saved')
        self.assertTrue(restarted.store.banned(USER))
        self.assertTrue(restarted.is_admin(ADMIN))
        self.assertEqual(restarted.store.scalar('SELECT COUNT(*) FROM users'),1)

    async def test_additive_migration_keeps_existing_records(self):
        self.panel.store.execute('DROP TABLE admin_photos')
        self.panel.store.track_user(person(USER),USER)
        restarted=AdminPanel(self.app,OWNER,self.reply,self.path)
        self.assertIsNone(restarted.store.get_photo('panel'))
        self.assertTrue(restarted.is_admin(ADMIN))
        self.assertEqual(restarted.store.scalar('SELECT COUNT(*) FROM users'),1)

    async def test_clear_category_returns_to_main_fallback(self):
        self.panel.store.set_photo('panel','main')
        self.panel.store.set_photo('stats','stats')
        await self.route(event(text='!setstatspic clear'))
        self.assertIsNone(self.panel.store.get_photo('stats'))
        obj=event(text='!stats')
        msg=await self.route(obj)
        self.assertEqual(msg.reply_photo.call_args.kwargs['photo'],'main')

    async def test_clearing_main_does_not_clear_category_pictures(self):
        self.panel.store.set_photo('panel','main')
        self.panel.store.set_photo('stats','stats')
        await self.route(event(text='!setadminpic clear'))
        self.assertIsNone(self.panel.store.get_photo('panel'))
        self.assertEqual(self.panel.store.get_photo('stats'),'stats')

    async def test_photo_commands_not_swallowed_by_broadcast_prompt(self):
        await self.route(event(text='!broadcast'))
        await self.route(event(text=None,caption='!setstatspic',photo_ids=('stats',)))
        self.assertEqual(self.panel.store.get_photo('stats'),'stats')
        self.assertFalse(self.panel.pending)
        self.bot.send_message.assert_not_awaited()
        self.bot.copy_message.assert_not_awaited()

    async def test_broadcast_category_photo_never_sent_to_audience(self):
        self.panel.store.set_photo('broadcast','admin-only')
        self.panel.store.track_user(person(USER),USER)
        obj=event(text='!broadcast News')
        msg=await self.route(obj)
        self.assertEqual(msg.reply_photo.call_args.kwargs['photo'],'admin-only')
        self.bot.send_message.assert_awaited_once_with(chat_id=OWNER,text='News',parse_mode=None)
        self.assertFalse(hasattr(self.bot,'send_photo'))
        self.assertIn('preview sent separately',msg.reply_photo.call_args.kwargs['caption'])

    async def test_target_confirmations_and_results_use_category_photo(self):
        self.panel.store.set_photo('ban','ban')
        obj=event(text='!ban '+str(USER))
        msg=await self.route(obj)
        self.assertEqual(msg.reply_photo.call_args.kwargs['photo'],'ban')
        nonce=self.panel.pending[(OWNER,OWNER)]['nonce']
        obj=event(callback='adm:confirm:'+nonce)
        msg=await self.route(obj)
        self.assertTrue(self.panel.store.banned(USER))
        self.assertEqual(msg.reply_photo.call_args.kwargs['photo'],'ban')

    async def test_unrelated_errors_do_not_display_admin_photos(self):
        self.panel.store.set_photo('panel','private-admin-art')
        obj=event(USER,text='!admin')
        msg=await self.route(obj)
        msg.reply_photo.assert_not_awaited()
        self.assertIn('Admin access only',self.reply.call_args.args[1])


if __name__=='__main__':
    unittest.main()
