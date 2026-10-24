# Bot database — activity storage update

## What changed

Only `admin_panel.py` changed at runtime. The main `kage_x_ban_checker.py` and every
other existing project file are byte-for-byte identical to the uploaded ZIP.
One test file and this setup guide are new. No new dependency is required.

The bot already had SQLite storage. This update keeps the same database and
adds one table (`activity_totals`) without deleting or replacing existing tables.

## What is stored

- Users: Telegram user ID, name, username, first/last activity, private-chat ID,
  and delivery availability.
- Groups: ID, title, username, last activity and last known membership state.
- Admins: added admin IDs, who added them and when. The owner remains configured
  through OWNER_ID, not a required row in the admins table.
- Bans, admin photo file IDs and required force-join chats.
- Anonymous daily totals: received interactions and allowed check command requests.
  These totals contain only UTC date, counter kind and count, with no user link.

Received interactions include private messages, callback button presses and
command-like group messages received by this module, including blocked attempts.
Passive group messages do not count as interactions. Check requests count text
`!check` and `/check` commands that pass the admin ban/force-join gates; one command
counts once even if it has multiple numbers. Invalid/empty requests count too.
The counter does NOT measure successful checks, individual numbers or API results.
Telegram redelivery/retries can count an update again; this is not a billing ledger.

Checked phone numbers, check results and message bodies are NOT saved in this
SQLite database. This update does not change the original checking code's logs
or Telegram messages; it is not a promise of log/message deletion. User-provided
profile/group names are still saved as before.

## Railway activation

1. Back up your existing database privately before updating.
2. Replace only `admin_panel.py` in your existing deployed project, or deploy the
   full updated ZIP if it matches your current project.
3. Keep BOT_TOKEN, BANCHECK_KEY, OWNER_ID and existing banners unchanged.
4. Attach a persistent Railway Volume to the bot service, mounted at `/data`.
5. Set the bot service variable:

   ADMIN_DB_PATH=/data/admin_panel.sqlite3

6. Apply the changes and redeploy one bot instance.
7. Send `!admin`, then select Bot Stats, Users List, Groups or Admins List.

The bot creates the database file automatically at startup. No live or blank
SQLite file is bundled, to avoid overwriting your real records. Keep databases
and backups out of public Git repositories.

IMPORTANT: If you already have records in a database at a different path, keep
that path if it is persistent, or stop the bot and copy the existing file onto
the Volume before changing ADMIN_DB_PATH. Changing a path does not move data;
a new empty database would otherwise be created. Keep a private backup and
redeploy only after the copy is complete.

Old user/admin/group/settings records remain intact when the same database is
used. New activity totals start at zero; historical counts cannot be reconstructed.
Lists reflect observed users/groups, not an exhaustive Telegram history. Uptime
still shows time since the current process started and resets on restart.

## Verification

90 offline tests passed, including the original 78 tests plus 12 database tests.
Coverage includes additive migration, restart persistence, privacy of checked
numbers in SQL contents, activity counting and ban/force-join enforcement.
These are offline tests, not a live Telegram or Railway deployment test.
