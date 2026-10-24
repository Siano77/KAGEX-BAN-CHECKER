"""Persistence, additive migration and checked-number privacy regression tests."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from admin_panel import Store, AdminPanel
from telegram.ext import ApplicationHandlerStop
from test_admin_panel import update, OWNER, USER, person


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'nested' / 'admin_panel.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_database_created_automatically(self):
        self.assertTrue(self.path.is_file())
        self.assertEqual(self.store.scalar('PRAGMA integrity_check'), 'ok')

    def test_empty_counters_are_zero(self):
        self.assertEqual(self.store.activity_total('interactions'), 0)
        self.assertEqual(self.store.activity_total('check_requests'), 0)

    def test_counters_survive_restart(self):
        for _ in range(3):
            self.store.count_activity('interactions')
        restarted = Store(self.path)
        self.assertEqual(restarted.activity_total('interactions'), 3)
        day = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(restarted.activity_total('interactions', day), 3)
        self.assertEqual(len(restarted.rows('SELECT * FROM activity_totals')), 1)

    def test_unknown_counter_cannot_save_arbitrary_text(self):
        with self.assertRaises(ValueError):
            self.store.count_activity('!check +2348012345678')
        self.assertEqual(len(self.store.rows('SELECT * FROM activity_totals')), 0)

    def test_old_database_migrates_without_losing_records(self):
        # Dropping only the new table reproduces the uploaded schema.
        self.store.execute('DROP TABLE activity_totals')
        self.store.track_user(person(USER), USER)
        self.store.execute('INSERT INTO admins VALUES(?,?,?)', (USER, OWNER, 1))
        self.store.execute('INSERT INTO bans VALUES(?,?,?)', (44, OWNER, 1))
        self.store.set_photo('panel', 'saved-photo')
        self.store.track_group(NS(id=-10044,title='Saved group',username=None))
        self.store.execute('INSERT INTO force_join_chats VALUES(?,?,?,?,?,?,?)',
                           (-10055,'Required',None,'https://t.me/example','channel',OWNER,1))
        restarted = Store(self.path)
        self.assertTrue(restarted.admin(USER))
        self.assertTrue(restarted.banned(44))
        self.assertEqual(restarted.get_photo('panel'), 'saved-photo')
        self.assertEqual(restarted.scalar('SELECT COUNT(*) FROM users'), 1)
        self.assertEqual(restarted.scalar('SELECT COUNT(*) FROM groups_seen'), 1)
        self.assertEqual(len(restarted.force_join_chats()), 1)
        self.assertEqual(restarted.activity_total('interactions'), 0)


class RoutingActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.reply = AsyncMock()
        self.panel = AdminPanel(NS(), OWNER, self.reply, self.path)
        self.ctx = NS(bot=NS(username='kage_test_bot'))

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_check_requests_count_not_number_quantity(self):
        await self.panel.middleware(update(text='!check +2348012345678 447911234567'), self.ctx)
        self.assertEqual(self.panel.store.activity_total('check_requests'), 1)
        self.assertEqual(self.panel.store.activity_total('interactions'), 1)

    async def test_checked_numbers_not_in_database(self):
        await self.panel.middleware(update(text='!check +2348012345678 447911234567'), self.ctx)
        with sqlite3.connect(self.path) as db:
            contents = '\n'.join(db.iterdump())
        for secret in ('2348012345678', '447911234567', '!check'):
            self.assertNotIn(secret, contents)

    async def test_banned_requests_not_counted_as_allowed_checks(self):
        self.panel.store.execute('INSERT INTO bans VALUES(?,?,?)', (USER,OWNER,1))
        with self.assertRaises(ApplicationHandlerStop):
            await self.panel.middleware(update(text='!check +2348012345678'), self.ctx)
        self.assertEqual(self.panel.store.activity_total('check_requests'), 0)
        self.assertEqual(self.panel.store.activity_total('interactions'), 1)

    async def test_required_membership_blocks_check_count(self):
        self.panel.missing_force_joins = AsyncMock(return_value=[{'chat_id':-1}])
        self.panel.force_join_notice = AsyncMock()
        with self.assertRaises(ApplicationHandlerStop):
            await self.panel.middleware(update(text='!check +2348012345678'), self.ctx)
        self.assertEqual(self.panel.store.activity_total('check_requests'), 0)

    async def test_text_commands_and_bot_addressing(self):
        for command in ('/check 123', '/check@kage_test_bot 123', '!CHECK 123', '!check'):
            await self.panel.middleware(update(text=command), self.ctx)
        for command in ('/check@another_bot 123', '!checkother 123', 'hello'):
            await self.panel.middleware(update(text=command), self.ctx)
        await self.panel.middleware(update(text=None, caption='!check 123'), self.ctx)
        self.assertEqual(self.panel.store.activity_total('check_requests'), 4)

    async def test_passive_group_messages_do_not_inflate_activity(self):
        await self.panel.middleware(update(text='hello', private=False), self.ctx)
        self.assertEqual(self.panel.store.activity_total('interactions'), 0)
        self.assertEqual(self.panel.store.scalar('SELECT COUNT(*) FROM groups_seen'), 1)

    async def test_stats_show_persistent_totals(self):
        await self.panel.middleware(update(text='!check 123'), self.ctx)
        with self.assertRaises(ApplicationHandlerStop):
            await self.panel.middleware(update(OWNER, '!stats'), self.ctx)
        text = self.reply.call_args.args[1]
        self.assertIn('Check requests: 1', text)
        self.assertIn('Received interactions: 2', text)
        self.assertIn('not completed results', text)
        self.assertIn('not saved in this database', text)
