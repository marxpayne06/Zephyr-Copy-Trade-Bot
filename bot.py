import logging
import os
import sqlite3
import asyncio
import base58
import httpx
import re
import base64
import time
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG (Paste your keys here)
# ─────────────────────────────────────────────────────────────────────────────

BOT_TOKEN         = "8544776626:AAFtqbjhQbC3vtw-ECW4np75J8iDeAJ28Ls"
SOLANA_RPC_URL    = "https://solana-mainnet.g.alchemy.com/v2/HKpG0b8eDkPcgqUadfkYw"
WELCOME_PHOTO_URL = "https://i.ibb.co/YBfYSqTw"
BOT_NAME          = "Zephyr Copy Trade Bot"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# RENDER HEALTH SERVER & CLEANUP
# ─────────────────────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Zephyr is alive")
    def log_message(self, *args): pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"✅ Health server active on port {port}")
    server.serve_forever()

def maintenance_loop():
    """Runs every 6 hours to prevent database bloat and RAM spikes on Render."""
    while True:
        try:
            conn = sqlite3.connect("zephyr.db")
            conn.execute("VACUUM") # Compresses DB file size
            conn.commit()
            conn.close()
            logger.info("🧹 System maintenance: Database vacuumed.")
        except Exception as e:
            logger.error(f"Maintenance error: {e}")
        time.sleep(21600) 

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

DB_PATH = "zephyr.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        referrer_id INTEGER, slippage REAL DEFAULT 1.0, gas_fee TEXT DEFAULT 'medium')""")
    c.execute("CREATE TABLE IF NOT EXISTS wallets (user_id INTEGER PRIMARY KEY, public_key TEXT NOT NULL, private_key TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS monitors (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, token_ca TEXT, token_name TEXT, target_price REAL, direction TEXT, active INTEGER DEFAULT 1)")
    conn.commit(); conn.close()

def upsert_user(uid, username, fname):
    conn = get_conn(); conn.execute("INSERT INTO users (user_id, username, first_name) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username", (uid, username, fname)); conn.commit(); conn.close()

def save_wallet(uid, pub, priv):
    conn = get_conn(); conn.execute("INSERT INTO wallets (user_id, public_key, private_key) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET public_key=excluded.public_key, private_key=excluded.private_key", (uid, pub, priv)); conn.commit(); conn.close()

def get_wallet(uid):
    conn = get_conn(); row = conn.execute("SELECT * FROM wallets WHERE user_id=?", (uid,)).fetchone(); conn.close(); return dict(row) if row else None

# ─────────────────────────────────────────────────────────────────────────────
# SOLANA & DEX UTILS
# ─────────────────────────────────────────────────────────────────────────────

from solders.keypair import Keypair
SOL_MINT = "So11111111111111111111111111111111111111112"

async def fetch_token_data(ca):
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}")
            pairs = r.json().get("pairs", [])
            return pairs[0] if pairs else None
        except: return None

async def get_sol_balance(pub):
    async with httpx.AsyncClient() as c:
        try:
            r = await c.post(SOLANA_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[pub]})
            return r.json().get("result", {}).get("value", 0) / 1e9
        except: return 0.0

# ─────────────────────────────────────────────────────────────────────────────
# UI & KEYBOARDS
# ─────────────────────────────────────────────────────────────────────────────

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👛 Wallet", callback_data="w_m"), InlineKeyboardButton("📊 Portfolio", callback_data="p_v")],
        [InlineKeyboardButton("🛒 Buy", callback_data="b_s"), InlineKeyboardButton("💸 Sell", callback_data="s_s")],
        [InlineKeyboardButton("📉 Trending", callback_data="t_m"), InlineKeyboardButton("⚙️ Settings", callback_data="set")],
        [InlineKeyboardButton("🤝 Referral", callback_data="ref")]
    ])

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="home")]])

# ─────────────────────────────────────────────────────────────────────────────
# HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)
    welcome_text = (f"👋 *Welcome to {BOT_NAME}*\n\n"
                    f"Fastest trading on Solana. Paste any token CA below to get started.")
    await update.message.reply_photo(photo=WELCOME_PHOTO_URL, caption=welcome_text, reply_markup=main_kb(), parse_mode=ParseMode.MARKDOWN)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = query.from_user.id
    
    if query.data == "home":
        await query.edit_message_caption(caption="Main Menu", reply_markup=main_kb())

    elif query.data == "w_m":
        w = get_wallet(uid)
        bal = await get_sol_balance(w['public_key']) if w else 0.0
        txt = f"👛 *Wallet Management*\n\nAddr: `{w['public_key'] if w else 'None'}`\nBal: `{bal:.4f} SOL`"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🆕 Create Wallet", callback_data="w_c")], [InlineKeyboardButton("⬅️ Back", callback_data="home")]])
        await query.edit_message_caption(caption=txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

    elif query.data == "w_c":
        kp = Keypair(); pub = str(kp.pubkey()); priv = base58.b58encode(bytes(kp)).decode()
        save_wallet(uid, pub, priv)
        await query.edit_message_caption(caption=f"✅ *Wallet Created!*\n\nAddress: `{pub}`\nPrivate Key: `{priv}`\n\n*Save this private key immediately!*", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())

    elif query.data == "t_m":
        await query.edit_message_caption(caption="📈 *Fetching Trending Tokens...*", parse_mode=ParseMode.MARKDOWN)
        # Simplified Trending
        await query.edit_message_caption(caption="📉 *DEXScreener Trending (Top 5)*\n\n1. $PEPE (SOL)\n2. $WIF (SOL)\n3. $BONK (SOL)\n\n_More tokens appearing as they trend!_", reply_markup=back_kb(), parse_mode=ParseMode.MARKDOWN)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    # CA detection
    if re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", text):
        msg = await update.message.reply_text("🔍 *Searching DEXScreener...*", parse_mode=ParseMode.MARKDOWN)
        data = await fetch_token_data(text)
        if data:
            price = data.get("priceUsd", "0.00")
            name = data.get("baseToken", {}).get("name", "Unknown")
            sym = data.get("baseToken", {}).get("symbol", "???")
            liq = data.get("liquidity", {}).get("usd", 0)
            
            txt = (f"🪙 *{name}* ({sym})\n\n"
                   f"💰 Price: `${price}`\n"
                   f"💧 Liquidity: `${liq:,.0f}`\n\n"
                   f"`{text}`")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy 0.1 SOL", callback_data=f"buy_0.1_{text}")],
                                       [InlineKeyboardButton("🛒 Buy 0.5 SOL", callback_data=f"buy_0.5_{text}")],
                                       [InlineKeyboardButton("⬅️ Menu", callback_data="home")]])
            await msg.edit_text(txt, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        else:
            await msg.edit_text("❌ Token not found or no liquidity.")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def main():
    init_db()
    # Start Keep-Alive Server
    Thread(target=run_health_server, daemon=True).start()
    # Start System Maintenance
    Thread(target=maintenance_loop, daemon=True).start()
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    logger.info("🌬️ Zephyr Bot is live!")
    app.run_polling()

if __name__ == "__main__":
    main()
