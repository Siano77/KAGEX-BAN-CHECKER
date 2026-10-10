# ⚡ KAGE X Ban Checker — Telegram Bot

WhatsApp account ban status checker via Telegram. Checks multiple numbers **simultaneously** and identifies ban type.

---

## 🚀 Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set your Bot Token
Open `kage_x_ban_checker.py` and replace:
```python
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
```
Get your token from [@BotFather](https://t.me/BotFather) on Telegram.

### 3. Run the bot
```bash
python kage_x_ban_checker.py
```

---

## 📋 Commands (Global prefix: `!`)

| Command | Description |
|---|---|
| `!check +234801234567` | Check one number |
| `!check 234801234567 447911234567` | Check multiple simultaneously |
| `!ping` | Check bot latency |
| `!help` | Show help |
| `!about` | About the bot |

Or use native slash commands: `/check`, `/help`, `/ping`, `/about`

---

## 🎯 Ban Types Detected

| Type | Meaning |
|---|---|
| 🔒 **Personal** | Account-level ban |
| 🌍 **Regional** | Country/region restriction |
| 👮 **Admin** | Platform enforcement ban |

---

## ⚡ Features
- Simultaneous checking (up to 20 numbers at once)
- Real-time ping display per number
- Beautiful aesthetic output
- Global `!` prefix for all users
- Inline keyboard buttons
