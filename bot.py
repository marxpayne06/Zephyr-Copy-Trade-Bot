import logging
import os
import sqlite3
import asyncio
import base58
import httpx
import re
import time
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8544776626:AAFtqbjhQbC3vtw-ECW4np75J8iDeAJ28Ls")
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
WELCOME_PHOTO_URL = "https://i.ibb.co/YBfYSqTw"
BOT_NAME = "Zephyr Copy Trade Bot"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# RENDER HEALTH SERVER (Stabilized)
# ─────────────────────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Zephyr Bot is alive!")
    
    def log_message(self, format, *args):
        return 

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"✅ Health server active on port {port}")
    server.serve_forever()

def maintenance_loop():
    while True:
        try:
            conn = sqlite3.connect("zephyr.db")
            conn.execute("VACUUM")
            conn.commit()
            conn.close()
            logger.info("🧹 Database maintenance completed.")
        except Exception as e:
            logger.error(f"Maintenance error: {e}")
        time.sleep(21600)  # 6 hours

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

DB_PATH = "zephyr.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, 
        username TEXT, 
        first_name TEXT,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        referrer_id INTEGER, 
        slippage REAL DEFAULT 1.0, 
        gas_fee TEXT DEFAULT 'medium',
        auto_buy BOOLEAN DEFAULT 0,
        buy_amount REAL DEFAULT 0.1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS wallets (
        user_id INTEGER PRIMARY KEY, 
        public_key TEXT NOT NULL, 
        private_key TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized")

def upsert_user(uid, username, fname, referrer_id=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO users (user_id, username, first_name, referrer_id) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
        (uid, username, fname, referrer_id)
    )
    conn.commit()
    conn.close()

def save_wallet(uid, pub, priv):
    conn = get_conn()
    conn.execute(
        "INSERT INTO wallets (user_id, public_key, private_key) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET public_key=excluded.public_key, private_key=excluded.private_key",
        (uid, pub, priv)
    )
    conn.commit()
    conn.close()

def get_wallet(uid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM wallets WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_user(uid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def update_user_settings(uid, **kwargs):
    conn = get_conn()
    for key, value in kwargs.items():
        conn.execute(f"UPDATE users SET {key}=? WHERE user_id=?", (value, uid))
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# SOLANA & DEX UTILS
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_token_data(ca):
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}")
            data = r.json()
            pairs = data.get("pairs", [])
            return pairs[0] if pairs else None
        except Exception as e:
            logger.error(f"Error fetching token data: {e}")
            return None

async def get_sol_balance(pub):
    if not pub: return 0.0
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(SOLANA_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[pub]})
            return r.json().get("result", {}).get("value", 0) / 1e9
        except Exception:
            return 0.0

# ─────────────────────────────────────────────────────────────────────────────
# UI & KEYBOARDS (Restored Full versions)
# ─────────────────────────────────────────────────────────────────────────────

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👛 Wallet", callback_data="w_m"), InlineKeyboardButton("📊 Portfolio", callback_data="p_v")],
        [InlineKeyboardButton("🛒 Buy", callback_data="b_s"), InlineKeyboardButton("💸 Sell", callback_data="s_s")],
        [InlineKeyboardButton("📈 Trending", callback_data="t_m"), InlineKeyboardButton("⚙️ Settings", callback_data="set")],
        [InlineKeyboardButton("🤝 Referral", callback_data="ref"), InlineKeyboardButton("🔔 Alerts", callback_data="alerts")]
    ])

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="home")]])

def wallet_kb(has_wallet=False):
    buttons = []
    if not has_wallet:
        buttons.append([InlineKeyboardButton("🆕 Create Wallet", callback_data="w_c"), InlineKeyboardButton("📥 Import", callback_data="w_i")])
    else:
        buttons.append([InlineKeyboardButton("💰 Balance", callback_data="w_b"), InlineKeyboardButton("📤 Withdraw", callback_data="w_w")])
        buttons.append([InlineKeyboardButton("🗑️ Delete Wallet", callback_data="w_d")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(buttons)

def buy_kb(ca):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 0.1 SOL", callback_data=f"buy_0.1_{ca}"), InlineKeyboardButton("🛒 0.5 SOL", callback_data=f"buy_0.5_{ca}")],
        [InlineKeyboardButton("🛒 1.0 SOL", callback_data=f"buy_1.0_{ca}"), InlineKeyboardButton("🛒 2.0 SOL", callback_data=f"buy_2.0_{ca}")],
        [InlineKeyboardButton("⚙️ Custom Amount", callback_data=f"buy_custom_{ca}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")]
    ])

def settings_kb(user_data):
    slippage = user_data.get('slippage', 1.0)
    gas = user_data.get('gas_fee', 'medium')
    auto_buy = "✅" if user_data.get('auto_buy', 0) else "❌"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Slippage: {slippage}%", callback_data="set_slippage")],
        [InlineKeyboardButton(f"Gas Fee: {gas.upper()}", callback_data="set_gas")],
        [InlineKeyboardButton(f"Auto-Buy: {auto_buy}", callback_data="set_autobuy")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")]
    ])

# ─────────────────────────────────────────────────────────────────────────────
# HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referrer_id = int(context.args[0]) if context.args and context.args[0].isdigit() else None
    upsert_user(user.id, user.username, user.first_name, referrer_id)
    
    welcome_text = (
        f"👋 *Welcome to {BOT_NAME}*\n\n"
        f"Your high-speed Solana trading companion.\n\n"
        f"Paste any token CA to start trading!"
    )
    
    try:
        await update.message.reply_photo(photo=WELCOME_PHOTO_URL, caption=welcome_text, reply_markup=main_kb(), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text(welcome_text, reply_markup=main_kb(), parse_mode=ParseMode.MARKDOWN)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    try:
        if data == "home":
            await query.edit_message_text("🏠 *Main Menu*", reply_markup=main_kb(), parse_mode=ParseMode.MARKDOWN)
        
        elif data == "w_m":
            w = get_wallet(uid)
            bal = await get_sol_balance(w['public_key']) if w else 0.0
            txt = f"👛 *Wallet Management*\n\nAddress: `{w['public_key'] if w else 'Not created'}`\nBalance: `{bal:.4f} SOL`"
            await query.edit_message_text(txt, reply_markup=wallet_kb(bool(w)), parse_mode=ParseMode.MARKDOWN)

        elif data == "w_c":
            from solders.keypair import Keypair
            kp = Keypair()
            pub, priv = str(kp.pubkey()), base58.b58encode(bytes(kp)).decode()
            save_wallet(uid, pub, priv)
            await query.edit_message_text(f"✅ *Wallet Created*\n\nAddress: `{pub}`\n\nPK: `{priv}`\n\n⚠️ SAVE THIS NOW!", reply_markup=back_kb(), parse_mode=ParseMode.MARKDOWN)

        elif data == "w_i":
            await query.edit_message_text("📥 *Import Wallet*\n\nSend your private key in the next message.", reply_markup=back_kb(), parse_mode=ParseMode.MARKDOWN)
            context.user_data['awaiting_import'] = True

        elif data == "t_m":
            await query.edit_message_text("🔥 *Trending on Solana*\n\n1. $SOL\n2. $WIF\n3. $BONK\n4. $JUP\n5. $RAY", reply_markup=back_kb(), parse_mode=ParseMode.MARKDOWN)

        elif data == "ref":
            link = f"https://t.me/{context.bot.username}?start={uid}"
            await query.edit_message_text(f"🤝 *Referral Program*\n\nLink: `{link}`\n\nEarn rewards when friends trade!", reply_markup=back_kb(), parse_mode=ParseMode.MARKDOWN)

        elif data == "set":
            u = get_user(uid) or {}
            await query.edit_message_text("⚙️ *Settings*", reply_markup=settings_kb(u), parse_mode=ParseMode.MARKDOWN)

        elif data.startswith("buy_"):
            parts = data.split("_")
            amount, ca = parts[1], parts[2] if len(parts) > 2 else None
            if amount == "custom":
                await query.edit_message_text("💰 *Enter custom SOL amount:*", reply_markup=back_kb())
                context.user_data['awaiting_buy_amount'] = ca
            else:
                await query.edit_message_text(f"🛒 *Confirm Buy {amount} SOL* for `{ca}`?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data=f"exec_buy_{amount}_{ca}"), InlineKeyboardButton("❌ Cancel", callback_data="home")]]), parse_mode=ParseMode.MARKDOWN)

        elif data.startswith("exec_buy_"):
            await query.edit_message_text("⏳ *Processing Transaction...*", parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(2)
            await query.edit_message_text("✅ *Order Placed!* (Simulation Mode)", reply_markup=main_kb())

    except Exception as e:
        logger.error(f"Callback error: {e}")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = update.effective_user.id
    
    if context.user_data.get('awaiting_import'):
        await update.message.reply_text("✅ Private key received (Import logic pending implementation).", reply_markup=main_kb())
        context.user_data['awaiting_import'] = False
        return

    if context.user_data.get('awaiting_buy_amount'):
        ca = context.user_data['awaiting_buy_amount']
        await update.message.reply_text(f"🛒 Confirm buy {text} SOL?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data=f"exec_buy_{text}_{ca}")]]))
        context.user_data['awaiting_buy_amount'] = False
        return

    if re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", text):
        msg = await update.message.reply_text("🔍 *Searching DEXScreener...*", parse_mode=ParseMode.MARKDOWN)
        data = await fetch_token_data(text)
        if data:
            txt = (f"🪙 *{data['baseToken']['name']}* (${data['baseToken']['symbol']})\n"
                   f"Price: `${data['priceUsd']}`\nLiq: `${data['liquidity']['usd']:,.0f}`\n\nCA: `{text}`")
            await msg.edit_text(txt, reply_markup=buy_kb(text), parse_mode=ParseMode.MARKDOWN)
        else:
            await msg.edit_text("❌ Token not found.", reply_markup=back_kb())
    else:
        await update.message.reply_text("❓ Please paste a valid Solana CA.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    init_db()
    Thread(target=run_health_server, daemon=True).start()
    Thread(target=maintenance_loop, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_error_handler(error_handler)

    logger.info("🚀 Zephyr is live!")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
