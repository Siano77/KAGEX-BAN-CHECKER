# Admin Panel — setup and usage

## What changed

The existing main bot file has **two added lines only**:

```python
from admin_panel import register_admin_panel
```

Immediately after `app = Application.builder().token(token).build()`:

```python
register_admin_panel(app, OWNER_ID, reply_monospace)
```

All checking functions, API credentials/configuration, existing commands, banner
logic and reply formatters are unchanged. `admin_panel.py` holds the new features.
No new third-party dependency is required.

## Deploy

1. Deploy the complete project, including **both** `admin_panel.py` and
   `kage_x_ban_checker.py`.
2. Keep your existing `BOT_TOKEN`, `BANCHECK_KEY` and banner settings unchanged.
   This update does not add, replace or repair your API key.
3. Set `OWNER_ID` to your positive numeric Telegram user ID. The admin panel
   remains locked if this is unset or zero.
4. Attach persistent storage and set `ADMIN_DB_PATH` to a path on that storage.
   Example: if your Railway volume is mounted at `/data`, set:

   ```text
   ADMIN_DB_PATH=/data/admin_panel.sqlite3
   ```

   The SQLite file is created automatically. **Without persistent storage, user
   records, bans and added admins can be lost when the container is replaced.**
   Mount paths must match your hosting configuration. This variable does not
   move the existing `banner.txt` or `check_banner.txt` files.
5. Run **one bot instance/replica** for this bot token. Existing polling conflicts
   from duplicate instances must be resolved separately.
6. Message the bot privately and send `!admin` or `/admin`.

Do not commit the live database to a public repository. It contains Telegram IDs,
names, usernames and group details. Back it up privately if needed.

## Panel buttons

```text
👑 ADMIN PANEL

📊 Bot Stats          ⏱ Uptime
👥 Users List         💬 Groups
🚫 Ban User           ✅ Unban User
👑 Admins List
➕ Add Admin          ➖ Remove Admin   (owner only)
📢 Broadcast
```

The panel uses private chat only so user IDs and administrative details are not
posted in groups. The same monospace reply helper is used for panel messages.
Stats/user/group pages include a button to return to the main panel.

## Direct commands

Every command below accepts `!` or `/`.

| Command | Purpose |
| --- | --- |
| `!admin` / `!panel` | Open main admin panel |
| `!stats` | Users tracked, users active in last 24 hours, broadcast audience, banned IDs, admins and tracked groups |
| `!uptime` | Time since this process started; resets on restart |
| `!users` | Paginated user list with numeric IDs and usernames |
| `!groups` | Paginated tracked active group list |
| `!admins` | Owner and added admins |
| `!ban 123456789` | Preview a bot-access ban; requires confirmation |
| `!unban 123456789` | Preview an unban; requires confirmation |
| `!addadmin 123456789` | Owner-only admin promotion; requires confirmation |
| `!removeadmin 123456789` | Owner-only admin removal; requires confirmation |
| `!broadcast` | Prompt for text/media, then preview and confirmation |
| `!broadcast Your announcement` | Preview a plain-text broadcast |
| `!cancel` | Cancel the current prompt or unconfirmed action |
| `!cancelbroadcast` | Stop a running broadcast (initiating admin or owner) |

Use Users List to obtain IDs. Targets must be **numeric Telegram user IDs**, not
phone numbers or @usernames. A target can be banned before their first visit.
The owner cannot be removed or banned. Existing admins must have their admin role
removed by the owner before they can be banned.

Added admins can see user/group records, ban/unban users and send broadcasts.
They cannot add or remove admins. Grant this access only to trusted people.
All permissions are rechecked on every command/button press.

Banning blocks new messages/commands/button actions from that person from reaching
the existing bot handlers; it does not suspend their Telegram account. A check
that was already running when the ban was applied may still finish.

## User and group tracking

- Tracking begins with this update. No historical users or groups are imported.
- Private-chat messages, commands and callback interactions register unique user
  IDs. Group command/callback users are tracked too; passive group members are
  not treated as bot users.
- Broadcasts only target recorded private-chat users. Group-only users cannot be
  privately messaged until they initiate a private chat with the bot.
- Active-in-last-24-hours counts recorded interactions, not Telegram's official
  public monthly-user statistic.
- Groups are discovered from messages the bot receives and bot-membership updates.
  Removal events mark groups inactive. Existing groups may need a command such as
  `!ping` after deployment to be recorded.
- This is not a complete Telegram directory. Privacy mode and missed updates
  can leave group records incomplete or stale. Telegram has no Bot API method to
  enumerate every historical bot user/group.
- If a user blocks the bot during broadcast, they are excluded from future
  broadcasts after Telegram reports Forbidden. A new private interaction marks
  them reachable again.

## Broadcast safety

1. Press Broadcast and send one text message, photo, video, document or other
   Telegram-copyable message.
2. A private preview appears. **No audience message is sent yet.**
3. Confirm with **Send broadcast**, or Cancel.
4. The bot sends at a conservative pace, respects Telegram retry-after responses,
   and reports delivered/failed/skipped/not-attempted totals.

Broadcasts exclude banned users, users known to have blocked the bot and groups.
The owner/admin is included if their private chat is an eligible tracked user.
For media broadcasts, Telegram's copy operation preserves the original message
content/caption; it does not force new monospace formatting onto that content.
Non-copyable service/protected messages may fail. Send a normal message instead.
Multiple-message media albums are treated as individual messages, not one album.

Only one broadcast can run at a time. The initiating admin or owner can press
Stop broadcast. Already delivered messages cannot be recalled. Removing the
initiating admin's role also stops further deliveries when the loop next checks.
A network request already in progress may complete before a stop takes effect.

Prompts and confirmation buttons expire after 10 minutes. Confirmation is bound
to the admin who created it and can be used once. Starting another admin command
invalidates an older pending confirmation. Using a regular bot command cancels a
pending input prompt and lets that existing command run normally.

Prompts, draft broadcasts and ongoing jobs are **not resumed after restart**.
Durable user records, bans, admin roles and group records remain in SQLite when
its storage persists. Do not redeploy during a broadcast if you want it to finish.

## Tests

With the existing dependencies installed:

```bash
python -m unittest discover -s tests -v
```

The suite uses the real `python-telegram-bot==21.6` library with mocked transport;
it does not log in or contact Telegram. It covers permissions, confirmation expiry
and replay, ban enforcement, persistence, tracking, pagination, broadcast preview,
copying, retries, blocked users, cancellation and handler registration. Live
Telegram/host deployment and the external BanCheck service still need testing.
