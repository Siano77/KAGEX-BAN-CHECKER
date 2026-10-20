# KAGE X Ban Checker — Telegram Bot

## Reply style and images

Bot replies now use Telegram's monospace `<pre>` code-block style, matching the
code-block text in your screenshot. Emoji still use Telegram's emoji rendering.
The exact font and Copy Code button appearance depend on the Telegram client.

There are two independent images:

- **General image:** for start, help, about, ping and API-status replies.
- **Check image:** for completed `!check` and `/check` results only.

### Set your images

1. Set `OWNER_ID` to your numeric Telegram user ID and restart the bot.
2. Send the bot your general photo with the caption `!setbanner`.
3. Send a different photo with the caption `!setcheckbanner`.
4. Send `!check +2348012345678` to see the separate check image.

Send photos as photos, not as document attachments. Both setter commands also
accept `/setbanner` and `/setcheckbanner`, including as photo captions.
Only the configured owner can change images. If `OWNER_ID` is missing or zero,
image-setting commands are disabled.

The screenshot is a reference only; no standalone banner image is bundled.
Upload the actual photos you want using the commands above.

Until a check image is configured, checks use the general image. If neither is
configured, replies are text-only. Long reports are sent as an image followed by
separate monospace messages because Telegram photo captions have a size limit.

### Banner storage

Photo file IDs are saved separately in `banner.txt` and `check_banner.txt`, in
the bot's working directory. They survive restarts only if that storage persists.
On hosts with ephemeral storage, use a persistent volume/working directory or set
`BOT_BANNER` and `CHECK_BANNER` to this bot's Telegram photo file IDs (or public
image URLs) as environment-variable defaults. Saved files take precedence over
environment-variable defaults.

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Configure environment variables on your host:

- `BOT_TOKEN`: your Telegram bot token.
- `OWNER_ID`: your numeric Telegram user ID (required to set images).
- `BANCHECK_KEY`: your BanCheck API key for the primary checking service.
- `BOT_BANNER`: optional general image file ID or public image URL.
- `CHECK_BANNER`: optional separate check image file ID or public image URL.

The original upload contained a hardcoded API key. It has been removed from this
copy. Rotate that key with your provider and put the replacement in `BANCHECK_KEY`;
do not commit credentials to source control.

Run:

```bash
python kage_x_ban_checker.py
```

## Commands

| Command | Purpose |
| --- | --- |
| `!start` | Welcome |
| `!check +2348012345678` | Single check |
| `!check 2348012345678 447911234567` | Batch check, up to 20 numbers |
| `!help` | Help |
| `!about` | About |
| `!ping` | Bot latency |
| `!testapi` | Test API connection |
| `!setbanner` | General image instructions (owner only) |
| `!setcheckbanner` | Check image instructions (owner only) |

Native slash equivalents are also supported.

## Validation

Python syntax compilation and 12 offline tests cover formatting, HTML escaping,
message splitting, separate image routing, banner persistence, owner restrictions
and photo-send fallback. Live Telegram rendering and the external checking API
have not been tested. The underlying checking engine is unchanged; this update
is not a verification of the accuracy of its ban-status results.

## New: owner/admin panel

Open `!admin` or `/admin` in a private chat. Includes stats, uptime, users, groups,
bans, owner-only admin management, and confirmed broadcasts.

See **ADMIN_PANEL.md** for setup, commands, persistent SQLite storage and limitations.
The main Python file only adds an import and registration call; its original
checking logic, API configuration, banners and formatters are unchanged.
