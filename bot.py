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
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG (Use Environment Variables for Security)
# ─────────────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8544776626:AAFtqbjhQbC3vtw-ECW4np75J8iDeAJ28Ls")
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
WELCOME_PHOTO_URL = os.environ.get("WELCOME_PHOTO_URL", "https://i.ibb.co/YBfYSqTw")
BOT_NAME = "Zephyr Copy Trade Bot"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# RENDER HEALTH SERVER (Required for 24/7 uptime)
# ─────────────────────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Zephyr Bot is alive and running!")
    
    def log_message(self, format, *args):
        # Suppress health check logs to reduce noise
        if self.path != '/health':
            logger.info(f"Health check: {args[0]}")

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"✅ Health server active on port {port}")
    server.serve_forever()

def maintenance_loop():
    """Runs every 6 hours to prevent database bloat."""
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
    
    # Users table
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
    
    # Wallets table
    c.execute("""CREATE TABLE IF NOT EXISTS wallets (
        user_id INTEGER PRIMARY KEY, 
        public_key TEXT NOT NULL, 
        private_key TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Monitors/Alerts table
    c.execute("""CREATE TABLE IF NOT EXISTS monitors (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        user_id INTEGER, 
        token_ca TEXT, 
        token_name TEXT, 
        target_price REAL, 
        direction TEXT, 
        active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Transactions table
    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        token_ca TEXT,
        token_name TEXT,
        type TEXT,
        amount_sol REAL,
        price_usd REAL,
        tx_hash TEXT,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized")

def upsert_user(uid, username, fname, referrer_id=None):
    conn = get_conn()
    conn.execute(
        """INSERT INTO users (user_id, username, first_name, referrer_id) 
           VALUES (?,?,?,?) 
           ON CONFLICT(user_id) DO UPDATE SET 
           username=excluded.username, 
           first_name=excluded.first_name""",
        (uid, username, fname, referrer_id)
    )
    conn.commit()
    conn.close()

def save_wallet(uid, pub, priv):
    conn = get_conn()
    conn.execute(
        """INSERT INTO wallets (user_id, public_key, private_key) 
           VALUES (?,?,?) 
           ON CONFLICT(user_id) DO UPDATE SET 
           public_key=excluded.public_key, 
           private_key=excluded.private_key""",
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

SOL_MINT = "So11111111111111111111111111111111111111112"

async def fetch_token_data(ca):
    """Fetch token data from DEXScreener"""
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
    """Get SOL balance for a public key"""
    if not pub:
        return 0.0
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(
                SOLANA_RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [pub]
                }
            )
            result = r.json().get("result", {})
            return result.get("value", 0) / 1e9
        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
            return 0.0

async def get_token_price(ca):
    """Get current token price"""
    data = await fetch_token_data(ca)
    return float(data.get("priceUsd", 0)) if data else 0

# ─────────────────────────────────────────────────────────────────────────────
# UI & KEYBOARDS
# ─────────────────────────────────────────────────────────────────────────────

def main_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👛 Wallet", callback_data="w_m"),
            InlineKeyboardButton("📊 Portfolio", callback_data="p_v")
        ],
        [
            InlineKeyboardButton("🛒 Buy", callback_data="b_s"),
            InlineKeyboardButton("💸 Sell", callback_data="s_s")
        ],
        [
            InlineKeyboardButton("📈 Trending", callback_data="t_m"),
            InlineKeyboardButton("⚙️ Settings", callback_data="set")
        ],
        [
            InlineKeyboardButton("🤝 Referral", callback_data="ref"),
            InlineKeyboardButton("🔔 Alerts", callback_data="alerts")
        ]
    ])

def back_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="home")]
    ])

def wallet_kb(has_wallet=False):
    buttons = []
    if not has_wallet:
        buttons.append([InlineKeyboardButton("🆕 Create New Wallet", callback_data="w_c")])
        buttons.append([InlineKeyboardButton("📥 Import Wallet", callback_data="w_i")])
    else:
        buttons.append([InlineKeyboardButton("💰 Check Balance", callback_data="w_b")])
        buttons.append([InlineKeyboardButton("📤 Withdraw SOL", callback_data="w_w")])
        buttons.append([InlineKeyboardButton("🗑️ Delete Wallet", callback_data="w_d")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(buttons)

def buy_kb(ca):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛒 Buy 0.1 SOL", callback_data=f"buy_0.1_{ca}"),
            InlineKeyboardButton("🛒 Buy 0.5 SOL", callback_data=f"buy_0.5_{ca}")
        ],
        [
            InlineKeyboardButton("🛒 Buy 1.0 SOL", callback_data=f"buy_1.0_{ca}"),
            InlineKeyboardButton("🛒 Buy 2.0 SOL", callback_data=f"buy_2.0_{ca}")
        ],
        [
            InlineKeyboardButton("⚙️ Custom Amount", callback_data=f"buy_custom_{ca}")
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")]
    ])

def sell_kb(ca):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 Sell 25%", callback_data=f"sell_25_{ca}"),
            InlineKeyboardButton("💸 Sell 50%", callback_data=f"sell_50_{ca}")
        ],
        [
            InlineKeyboardButton("💸 Sell 75%", callback_data=f"sell_75_{ca}"),
            InlineKeyboardButton("💸 Sell 100%", callback_data=f"sell_100_{ca}")
        ],
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
    """Handle /start command"""
    user = update.effective_user
    
    # Check for referral
    referrer_id = None
    if context.args and context.args[0].isdigit():
        referrer_id = int(context.args[0])
    
    upsert_user(user.id, user.username, user.first_name, referrer_id)
    
    welcome_text = (
        f"👋 *Welcome to {BOT_NAME}*\n\n"
        f"Your high-speed Solana trading companion.\n\n"
        f"🚀 *Features:*\n"
        f"• Instant token swaps\n"
        f"• Real-time price monitoring\n"
        f"• Wallet management\n"
        f"• Trending tokens\n\n"
        f"Paste any token Contract Address (CA) to start trading!"
    )
    
    # Send photo if URL is valid, otherwise send text
    try:
        if WELCOME_PHOTO_URL and WELCOME_PHOTO_URL.startswith('http'):
            await update.message.reply_photo(
                photo=WELCOME_PHOTO_URL,
                caption=welcome_text,
                reply_markup=main_kb(),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                welcome_text,
                reply_markup=main_kb(),
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error(f"Error sending welcome: {e}")
        await update.message.reply_text(
            welcome_text,
            reply_markup=main_kb(),
            parse_mode=ParseMode.MARKDOWN
        )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries"""
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    
    data = query.data
    
    try:
        # Main Navigation
        if data == "home":
            await query.edit_message_text(
                "🏠 *Main Menu*\n\nSelect an option below:",
                reply_markup=main_kb(),
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Wallet Management
        elif data == "w_m":
            w = get_wallet(uid)
            bal = await get_sol_balance(w['public_key']) if w else 0.0
            
            txt = (
                f"👛 *Wallet Management*\n\n"
                f"Address: `{w['public_key'] if w else 'Not created'}`\n"
                f"Balance: `{bal:.4f} SOL`\n\n"
                f"Select an option:"
            )
            await query.edit_message_text(
                txt,
                reply_markup=wallet_kb(has_wallet=bool(w)),
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif data == "w_c":
            # Generate new wallet
            try:
                from solders.keypair import Keypair
                kp = Keypair()
                pub = str(kp.pubkey())
                priv = base58.b58encode(bytes(kp)).decode()
                save_wallet(uid, pub, priv)
                
                txt = (
                    f"✅ *Wallet Created Successfully!*\n\n"
                    f"Address: `{pub}`\n\n"
                    f"🔐 *Private Key:*\n`{priv}`\n\n"
                    f"⚠️ *IMPORTANT:* Save this private key immediately! "
                    f"You won't see it again. Never share it with anyone!"
                )
                await query.edit_message_text(
                    txt,
                    reply_markup=back_kb(),
                    parse_mode=ParseMode.MARKDOWN
                )
            except ImportError:
                await query.edit_message_text(
                    "❌ Wallet generation requires `solders` library. "
                    "Please install it or contact support.",
                    reply_markup=back_kb()
                )
        
        elif data == "w_i":
            await query.edit_message_text(
                "📥 *Import Wallet*\n\n"
                "Please send your private key in the next message.\n"
                "⚠️ This will overwrite any existing wallet!",
                reply_markup=back_kb(),
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data['awaiting_import'] = True
        
        elif data == "w_b":
            w = get_wallet(uid)
            if w:
                bal = await get_sol_balance(w['public_key'])
                await query.edit_message_text(
                    f"💰 *Balance Check*\n\n"
                    f"Address: `{w['public_key']}`\n"
                    f"SOL Balance: `{bal:.4f} SOL`",
                    reply_markup=back_kb(),
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.edit_message_text(
                    "❌ No wallet found. Create one first!",
                    reply_markup=wallet_kb(has_wallet=False)
                )
        
        elif data == "w_w":
            await query.edit_message_text(
                "📤 *Withdraw SOL*\n\n"
                "Feature coming soon!\n"
                "You'll be able to send SOL to any address.",
                reply_markup=back_kb()
            )
        
        elif data == "w_d":
            await query.edit_message_text(
                "🗑️ *Delete Wallet*\n\n"
                "Are you sure? This cannot be undone!\n\n"
                "⚠️ Make sure you have backed up your private key!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes, Delete", callback_data="w_d_confirm")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="w_m")]
                ])
            )
        
        elif data == "w_d_confirm":
            conn = get_conn()
            conn.execute("DELETE FROM wallets WHERE user_id=?", (uid,))
            conn.commit()
            conn.close()
            await query.edit_message_text(
                "✅ Wallet deleted successfully.",
                reply_markup=main_kb()
            )
        
        # Portfolio
        elif data == "p_v":
            await query.edit_message_text(
                "📊 *Portfolio*\n\n"
                "Your holdings will appear here.\n"
                "Connect your wallet to see your assets.",
                reply_markup=back_kb(),
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Buy/Sell Placeholders
        elif data == "b_s":
            await query.edit_message_text(
                "🛒 *Buy Tokens*\n\n"
                "Paste a token Contract Address (CA) to buy.\n\n"
                "Example: `DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263`",
                reply_markup=back_kb(),
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif data == "s_s":
            await query.edit_message_text(
                "💸 *Sell Tokens*\n\n"
                "Paste a token Contract Address (CA) to sell.\n"
                "Or select from your portfolio.",
                reply_markup=back_kb(),
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Trending
        elif data == "t_m":
            await query.edit_message_text(
                "📈 *Fetching Trending Tokens...*",
                parse_mode=ParseMode.MARKDOWN
            )
            
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get("https://api.dexscreener.com/token-boosts/top/v1")
                    boosts = r.json()
                    
                    txt = "🔥 *Trending on DEXScreener*\n\n"
                    for i, token in enumerate(boosts[:5], 1):
                        name = token.get('token', {}).get('name', 'Unknown')
                        sym = token.get('token', {}).get('symbol', '???')
                        ca = token.get('token', {}).get('address', '')
                        txt += f"{i}. *{name}* (${sym})\n`{ca}`\n\n"
                    
                    await query.edit_message_text(
                        txt,
                        reply_markup=back_kb(),
                        parse_mode=ParseMode.MARKDOWN
                    )
            except Exception as e:
                logger.error(f"Error fetching trending: {e}")
                await query.edit_message_text(
                    "📉 *Trending Tokens*\n\n"
                    "1. $PEPE (SOL)\n2. $WIF (SOL)\n3. $BONK (SOL)\n4. $JUP (SOL)\n5. $RAY (SOL)\n\n"
                    "_Data temporarily unavailable_",
                    reply_markup=back_kb(),
                    parse_mode=ParseMode.MARKDOWN
                )
        
        # Settings
        elif data == "set":
            user_data = get_user(uid) or {}
            await query.edit_message_text(
                "⚙️ *Settings*\n\nConfigure your trading preferences:",
                reply_markup=settings_kb(user_data),
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif data == "set_slippage":
            await query.edit_message_text(
                "📊 *Set Slippage*\n\n"
                "Current options: 0.5%, 1%, 2%, 3%\n\n"
                "Tap to change:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("0.5%", callback_data="slip_0.5")],
                    [InlineKeyboardButton("1.0%", callback_data="slip_1.0")],
                    [InlineKeyboardButton("2.0%", callback_data="slip_2.0")],
                    [InlineKeyboardButton("3.0%", callback_data="slip_3.0")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="set")]
                ])
            )
        
        elif data.startswith("slip_"):
            slip = float(data.split("_")[1])
            update_user_settings(uid, slippage=slip)
            await query.answer(f"Slippage set to {slip}%")
            user_data = get_user(uid) or {}
            await query.edit_message_text(
                "⚙️ *Settings Updated*\n\n"
                f"Slippage: {slip}%",
                reply_markup=settings_kb(user_data),
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif data == "set_gas":
            await query.edit_message_text(
                "⛽ *Set Gas Priority*\n\n"
                "Higher = faster transactions",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Low", callback_data="gas_low")],
                    [InlineKeyboardButton("Medium", callback_data="gas_medium")],
                    [InlineKeyboardButton("High", callback_data="gas_high")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="set")]
                ])
            )
        
        elif data.startswith("gas_"):
            gas = data.split("_")[1]
            update_user_settings(uid, gas_fee=gas)
            await query.answer(f"Gas priority: {gas}")
            user_data = get_user(uid) or {}
            await query.edit_message_text(
                "⚙️ *Settings Updated*",
                reply_markup=settings_kb(user_data),
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Referral
        elif data == "ref":
            ref_link = f"https://t.me/{context.bot.username}?start={uid}"
            await query.edit_message_text(
                f"🤝 *Referral Program*\n\n"
                f"Your referral link:\n`{ref_link}`\n\n"
                f"Share and earn rewards when friends trade!\n\n"
                f"Stats coming soon...",
                reply_markup=back_kb(),
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Alerts
        elif data == "alerts":
            await query.edit_message_text(
                "🔔 *Price Alerts*\n\n"
                "Set alerts for price movements.\n"
                "Feature coming soon!",
                reply_markup=back_kb(),
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Buy Actions
        elif data.startswith("buy_"):
            parts = data.split("_")
            amount = parts[1]
            ca = parts[2] if len(parts) > 2 else None
            
            if amount == "custom":
                await query.edit_message_text(
                    "💰 *Custom Buy Amount*\n\n"
                    "Please enter the amount of SOL you want to spend:",
                    reply_markup=back_kb(),
                    parse_mode=ParseMode.MARKDOWN
                )
                context.user_data['awaiting_buy_amount'] = ca
            else:
                await query.edit_message_text(
                    f"🛒 *Confirm Purchase*\n\n"
                    f"Amount: {amount} SOL\n"
                    f"Token: `{ca}`\n\n"
                    f"⚠️ This is a demo. Real trading coming soon!",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_buy_{amount}_{ca}")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="home")]
                    ]),
                    parse_mode=ParseMode.MARKDOWN
                )
        
        elif data.startswith("confirm_buy_"):
            parts = data.split("_")
            amount = parts[2]
            ca = parts[3]
            
            # Simulate buy
            await query.edit_message_text(
                f"⏳ *Processing Buy Order...*\n\n"
                f"Amount: {amount} SOL\n"
                f"Token: `{ca}`\n\n"
                f"Please wait...",
                parse_mode=ParseMode.MARKDOWN
            )
            await asyncio.sleep(2)
            await query.edit_message_text(
                f"✅ *Buy Order Simulated*\n\n"
                f"Amount: {amount} SOL\n"
                f"Token: `{ca}`\n\n"
                f"In production, this would execute on Solana!",
                reply_markup=main_kb(),
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Sell Actions
        elif data.startswith("sell_"):
            parts = data.split("_")
            percentage = parts[1]
            ca = parts[2] if len(parts) > 2 else "unknown"
            
            await query.edit_message_text(
                f"💸 *Confirm Sell*\n\n"
                f"Percentage: {percentage}%\n"
                f"Token: `{ca}`\n\n"
                f"⚠️ This is a demo. Real trading coming soon!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_sell_{percentage}_{ca}")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="home")]
                ]),
                parse_mode=ParseMode.MARKDOWN
            )
        
        else:
            await query.answer("Coming soon!")
            
    except Exception as e:
        logger.error(f"Callback error: {e}")
        await query.edit_message_text(
            "❌ An error occurred. Please try again.",
            reply_markup=main_kb()
        )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages"""
    text = update.message.text.strip()
    uid = update.effective_user.id
    
    # Check for awaiting states
    if context.user_data.get('awaiting_import'):
        # Handle wallet import
        try:
            # Validate private key (basic check)
            if len(text) < 32:
                await update.message.reply_text("❌ Invalid private key format.")
                return
            
            # Import logic here (simplified for demo)
            await update.message.reply_text(
                "✅ Wallet import feature coming soon!\n"
                "Please use Create Wallet for now.",
                reply_markup=main_kb()
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
        finally:
            context.user_data['awaiting_import'] = False
        return
    
    if context.user_data.get('awaiting_buy_amount'):
        ca = context.user_data['awaiting_buy_amount']
        try:
            amount = float(text)
            await update.message.reply_text(
                f"🛒 *Buy {amount} SOL*\n"
                f"Token: `{ca}`\n\n"
                f"Confirm?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_buy_{amount}_{ca}")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="home")]
                ]),
                parse_mode=ParseMode.MARKDOWN
            )
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid number.")
        finally:
            context.user_data['awaiting_buy_amount'] = False
        return
    
    # CA detection (Solana addresses are 32-44 chars, base58)
    if re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", text):
        msg = await update.message.reply_text(
            "🔍 *Searching DEXScreener...*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        data = await fetch_token_data(text)
        if data:
            price = data.get("priceUsd", "0.00")
            name = data.get("baseToken", {}).get("name", "Unknown")
            sym = data.get("baseToken", {}).get("symbol", "???")
            liq = data.get("liquidity", {}).get("usd", 0)
            vol24h = data.get("volume", {}).get("h24", 0)
            price_change = data.get("priceChange", {}).get("h24", 0)
            
            txt = (
                f"🪙 *{name}* (${sym})\n\n"
                f"💰 Price: `${price}`\n"
                f"📊 24h Change: `{price_change:+.2f}%`\n"
                f"💧 Liquidity: `${liq:,.0f}`\n"
                f"📈 24h Volume: `${vol24h:,.0f}`\n\n"
                f"CA: `{text}`"
            )
            
            await msg.edit_text(
                txt,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🛒 Buy", callback_data=f"buy_select_{text}"),
                        InlineKeyboardButton("💸 Sell", callback_data=f"sell_select_{text}")
                    ],
                    [InlineKeyboardButton("📊 Chart", url=data.get('url', f'https://dexscreener.com/solana/{text}'))],
                    [InlineKeyboardButton("🔔 Set Alert", callback_data=f"alert_{text}")],
                    [InlineKeyboardButton("⬅️ Menu", callback_data="home")]
                ]),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await msg.edit_text(
                "❌ Token not found or no liquidity on DEXScreener.\n"
                "Please check the contract address.",
                reply_markup=back_kb()
            )
    else:
        await update.message.reply_text(
            "❓ I don't understand that command.\n"
            "Use /start to see the menu or paste a token CA.",
            reply_markup=main_kb()
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ An error occurred. Please try again later."
        )

# ─────────────────────────────────────────────────────────────────────────────
# MAIN EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Initialize database
    init_db()
    
    # Start health server (for Render + UptimeRobot)
    Thread(target=run_health_server, daemon=True).start()
    logger.info("🏥 Health check server started")
    
    # Start maintenance loop
    Thread(target=maintenance_loop, daemon=True).start()
    logger.info("🧹 Maintenance loop started")
    
    # Build application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)
    
    logger.info("🌬️ Zephyr Bot is live and running!")
    
    # Run polling
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    main()
