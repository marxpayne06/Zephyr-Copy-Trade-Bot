import logging
import os
import sqlite3
import asyncio
import base58
import httpx
import re
import base64
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BOT_TOKEN         = "8544776626:AAFtqbjhQbC3vtw-ECW4np75J8iDeAJ28Ls"
WELCOME_PHOTO_URL = "https://i.ibb.co/YBfYSqTw"
SOLANA_RPC_URL    = "https://solana-mainnet.g.alchemy.com/v2/HKpG0b8eDkPcgqUadfkYw"
BOT_NAME          = "Zephyr Copy Trade Bot"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# HEALTH SERVER (Render Fix)
# ─────────────────────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Zephyr is alive")
    def log_message(self, *args): pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"✅ Health server online on port {port}")
    server.serve_forever()

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE ENGINE
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
    c.execute("""CREATE TABLE IF NOT EXISTS wallets (
        user_id INTEGER PRIMARY KEY, public_key TEXT NOT NULL, private_key TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS referrals (
        referrer_id INTEGER, referee_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (referrer_id, referee_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS monitors (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        token_ca TEXT, token_name TEXT, target_price REAL,
        direction TEXT, active INTEGER DEFAULT 1)""")
    conn.commit(); conn.close()

def upsert_user(user_id, username, first_name, referrer_id=None):
    conn = get_conn()
    conn.execute("""INSERT INTO users (user_id, username, first_name, referrer_id) VALUES (?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
        (user_id, username, first_name, referrer_id))
    conn.commit(); conn.close()

def get_user(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close(); return dict(row) if row else None

def update_settings(user_id, slippage=None, gas_fee=None):
    conn = get_conn()
    if slippage is not None: conn.execute("UPDATE users SET slippage=? WHERE user_id=?", (slippage, user_id))
    if gas_fee  is not None: conn.execute("UPDATE users SET gas_fee=?  WHERE user_id=?", (gas_fee,  user_id))
    conn.commit(); conn.close()

def save_wallet(user_id, pub, priv):
    conn = get_conn()
    conn.execute("""INSERT INTO wallets (user_id,public_key,private_key) VALUES (?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET public_key=excluded.public_key, private_key=excluded.private_key""",
        (user_id, pub, priv))
    conn.commit(); conn.close()

def get_wallet(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    conn.close(); return dict(row) if row else None

def add_referral(referrer_id, referee_id):
    conn = get_conn()
    try: conn.execute("INSERT INTO referrals (referrer_id,referee_id) VALUES (?,?)", (referrer_id, referee_id)); conn.commit()
    except sqlite3.IntegrityError: pass
    conn.close()

def get_referral_count(user_id):
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (user_id,)).fetchone()[0]
    conn.close(); return n

def add_monitor(user_id, ca, name, price, direction):
    conn = get_conn()
    conn.execute("INSERT INTO monitors (user_id,token_ca,token_name,target_price,direction) VALUES (?,?,?,?,?)",
                 (user_id, ca, name, price, direction))
    conn.commit(); conn.close()

def get_monitors(user_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM monitors WHERE user_id=? AND active=1", (user_id,)).fetchall()
    conn.close(); return [dict(r) for r in rows]

def deactivate_monitor(mid):
    conn = get_conn()
    conn.execute("UPDATE monitors SET active=0 WHERE id=?", (mid,))
    conn.commit(); conn.close()

def get_all_active_monitors():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM monitors WHERE active=1").fetchall()
    conn.close(); return [dict(r) for r in rows]

# ─────────────────────────────────────────────────────────────────────────────
# SOLANA & JUPITER CORE
# ─────────────────────────────────────────────────────────────────────────────

from solders.keypair import Keypair
from solders.pubkey import Pubkey
SOL_MINT = "So11111111111111111111111111111111111111112"

def create_wallet():
    kp = Keypair()
    return str(kp.pubkey()), base58.b58encode(bytes(kp)).decode()

def keypair_from_pk(pk_b58):
    return Keypair.from_bytes(base58.b58decode(pk_b58))

async def get_sol_balance(pub):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(SOLANA_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[pub]})
            return r.json().get("result",{}).get("value",0) / 1e9
    except: return 0.0

async def get_token_accounts(pub):
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(SOLANA_RPC_URL, json={
                "jsonrpc":"2.0","id":1,"method":"getTokenAccountsByOwner",
                "params":[pub,{"programId":"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},{"encoding":"jsonParsed"}]})
            accs = r.json().get("result",{}).get("value",[])
            return [{"mint":a["account"]["data"]["parsed"]["info"]["mint"],
                     "amount":float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0),
                     "decimals":a["account"]["data"]["parsed"]["info"]["tokenAmount"]["decimals"]}
                    for a in accs if float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)>0]
    except: return []

async def get_token_price_dex(ca):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}")
            pairs = r.json().get("pairs")
            if not pairs: return None
            p = sorted(pairs, key=lambda x: float(x.get("liquidity",{}).get("usd",0) or 0), reverse=True)[0]
            return {"name":p.get("baseToken",{}).get("name","Unknown"),
                    "symbol":p.get("baseToken",{}).get("symbol","???"),
                    "price_usd":float(p.get("priceUsd") or 0),
                    "market_cap":p.get("marketCap"), "volume_24h":p.get("volume",{}).get("h24"),
                    "change_1h":p.get("priceChange",{}).get("h1"), "change_24h":p.get("priceChange",{}).get("h24"),
                    "liquidity":p.get("liquidity",{}).get("usd"), "pair_url":p.get("url")}
    except: return None

async def jupiter_swap(private_key, input_mint, output_mint, amount_units, slippage=1.0):
    try:
        kp = keypair_from_pk(private_key); pub = str(kp.pubkey())
        async with httpx.AsyncClient(timeout=25) as c:
            q = (await c.get("https://quote-api.jup.ag/v6/quote", params={
                "inputMint":input_mint,"outputMint":output_mint,
                "amount":amount_units,"slippageBps":int(slippage*100)})).json()
            if "error" in q: return {"success":False,"error":q["error"]}
            s = (await c.post("https://quote-api.jup.ag/v6/swap", json={
                "quoteResponse":q,"userPublicKey":pub,"wrapAndUnwrapSol":True,
                "dynamicComputeUnitLimit":True,"prioritizationFeeLamports":"auto"})).json()
            if "swapTransaction" not in s: return {"success":False,"error":"Could not build swap tx"}
            from solders.transaction import VersionedTransaction
            tx = VersionedTransaction.from_bytes(base64.b64decode(s["swapTransaction"]))
            res = (await c.post(SOLANA_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"sendTransaction",
                "params":[base64.b64encode(bytes(tx)).decode(),{"encoding":"base64"}]})).json()
            sig = res.get("result")
            return {"success":True,"tx_signature":sig} if sig else {"success":False,"error":res.get("error",{}).get("message","Unknown")}
    except Exception as e: return {"success":False,"error":str(e)}

async def send_sol(private_key, to_address, amount_sol):
    try:
        from solders.system_program import transfer, TransferParams
        from solders.transaction import Transaction
        from solders.message import Message
        from solders.hash import Hash
        kp = keypair_from_pk(private_key)
        async with httpx.AsyncClient(timeout=15) as c:
            bh = (await c.post(SOLANA_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"getLatestBlockhash","params":[]})).json()
            blockhash = bh["result"]["value"]["blockhash"]
            ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Pubkey.from_string(to_address), lamports=int(amount_sol*1e9)))
            tx = Transaction([kp], Message([ix], kp.pubkey()), Hash.from_string(blockhash))
            res = (await c.post(SOLANA_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"sendTransaction",
                "params":[base64.b64encode(bytes(tx)).decode(),{"encoding":"base64"}]})).json()
            sig = res.get("result")
            return {"success":True,"tx_signature":sig} if sig else {"success":False,"error":res.get("error",{}).get("message","Unknown")}
    except Exception as e: return {"success":False,"error":str(e)}

# ─────────────────────────────────────────────────────────────────────────────
# UI HELPERS
# ─────────────────────────────────────────────────────────────────────────────

CA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

def fmt(n, pre="$"):
    if n is None: return "N/A"
    n = float(n)
    if n >= 1_000_000: return f"{pre}{n/1_000_000:.2f}M"
    if n >= 1_000: return f"{pre}{n/1_000:.1f}K"
    return f"{pre}{n:.6f}" if n < 0.01 else f"{pre}{n:.4f}"

def bk(cb="back_home"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=cb)]])

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👛 Wallet", callback_data="wallet_menu"),
         InlineKeyboardButton("📊 Portfolio", callback_data="portfolio_view")],
        [InlineKeyboardButton("🛒 Buy", callback_data="buy_start"),
         InlineKeyboardButton("💸 Sell", callback_data="sell_start")],
        [InlineKeyboardButton("🔄 Swap", callback_data="swap_start"),
         InlineKeyboardButton("🔔 Monitors", callback_data="monitor_menu")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings_menu"),
         InlineKeyboardButton("🤝 Referral", callback_data="referral_menu")]
    ])

async def send_home(target, user, context):
    text = (f"👋 Welcome, *{user.first_name}* to *{BOT_NAME}*!\n\n"
            f"Paste any Solana *Contract Address* (CA) to trade instantly.\n"
            f"Ensure your wallet is funded before swapping.")
    if hasattr(target, "reply_photo"):
        await target.reply_photo(photo=WELCOME_PHOTO_URL, caption=text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())
    else:
        await target.edit_message_caption(caption=text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())

# ─────────────────────────────────────────────────────────────────────────────
# CALLBACK ROUTER
# ─────────────────────────────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid = query.from_user.id; d = query.data

    if d == "back_home": await send_home(query, query.from_user, context)
    
    elif d == "wallet_menu":
        w = get_wallet(uid); pub = w['public_key'] if w else "❌ No wallet connected"
        await query.edit_message_caption(caption=f"👛 *Wallet Management*\n\nAddress: `{pub}`", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🆕 Create New", callback_data="wallet_create")],
                [InlineKeyboardButton("📥 Import Key", callback_data="wallet_import")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back_home")]
            ]))

    elif d == "wallet_create":
        pub, priv = create_wallet(); save_wallet(uid, pub, priv)
        await query.edit_message_caption(caption=f"✅ *Wallet Created!*\n\nAddress: `{pub}`\n\nPrivate Key:\n`{priv}`\n\n⚠️ *Save this key now!* It will not be shown again.", parse_mode=ParseMode.MARKDOWN, reply_markup=bk())

    elif d == "portfolio_view":
        w = get_wallet(uid)
        if not w: await query.edit_message_caption(caption="❌ No wallet found.", reply_markup=bk()); return
        await query.edit_message_caption(caption="⌛ Loading Portfolio...")
        bal = await get_sol_balance(w['public_key'])
        toks = await get_token_accounts(w['public_key'])
        lines = [f"📊 *Portfolio*\nSOL: `{bal:.4f}`\n"]
        for t in toks[:10]: lines.append(f"• `{t['mint'][:6]}...`: {t['amount']}")
        await query.edit_message_caption(caption="\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=bk())

    elif d == "buy_start":
        context.user_data["awaiting"] = "buy_ca"
        await query.edit_message_caption(caption="🛒 *Buy Token*\nPaste the Contract Address (CA):", reply_markup=bk())

    elif d.startswith("buy_amount_"):
        amt = float(d.split("_")[-1]); ca = context.user_data.get("trade_ca")
        w = get_wallet(uid); u = get_user(uid) or {}
        await query.edit_message_caption(caption="⏳ Executing Buy...")
        res = await jupiter_swap(w["private_key"], SOL_MINT, ca, int(amt*1e9), u.get("slippage", 1.0))
        text = f"✅ *Success!* [Tx](https://solscan.io/tx/{res['tx_signature']})" if res["success"] else f"❌ *Failed:* {res['error']}"
        await query.edit_message_caption(caption=text, parse_mode=ParseMode.MARKDOWN, reply_markup=bk())

    elif d == "settings_menu":
        u = get_user(uid) or {}
        await query.edit_message_caption(caption=f"⚙️ *Settings*\n\nSlippage: `{u.get('slippage', 1.0)}%`", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Set Slippage", callback_data="set_slip")], [InlineKeyboardButton("⬅️ Back", callback_data="back_home")]]))

    elif d == "referral_menu":
        count = get_referral_count(uid)
        bot = await context.bot.get_me()
        link = f"https://t.me/{bot.username}?start=ref_{uid}"
        await query.edit_message_caption(caption=f"🤝 *Referrals*\nEarn rewards for inviting friends!\n\nTotal Referrals: *{count}*\n\nYour Link:\n`{link}`", parse_mode=ParseMode.MARKDOWN, reply_markup=bk())

# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip(); uid = update.effective_user.id; state = context.user_data.get("awaiting")
    
    if state == "buy_ca" and CA_RE.match(text):
        context.user_data["trade_ca"] = text; context.user_data["awaiting"] = None
        info = await get_token_price_dex(text)
        name = info["name"] if info else "Token"
        await update.message.reply_text(f"🛒 *Buy {name}*\nSelect SOL amount:", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("0.1 SOL", callback_data="buy_amount_0.1"), InlineKeyboardButton("0.5 SOL", callback_data="buy_amount_0.5")],
                [InlineKeyboardButton("1.0 SOL", callback_data="buy_amount_1.0"), InlineKeyboardButton("❌ Cancel", callback_data="back_home")]
            ]))
        return

    if CA_RE.match(text):
        info = await get_token_price_dex(text)
        if not info: await update.message.reply_text("❌ Token not found."); return
        await update.message.reply_text(
            f"🪙 *{info['name']}* (${info['symbol']})\n\nPrice: `${info['price_usd']:.8f}`\nLiq: `${info['liquidity']:,}`\nMCap: `${info['market_cap']:,}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy", callback_data="buy_start"), InlineKeyboardButton("📈 Chart", url=info['pair_url'])]]))

# ─────────────────────────────────────────────────────────────────────────────
# CORE EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ref = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            rid = int(context.args[0][4:])
            if rid != user.id: add_referral(rid, user.id); ref = rid
        except: pass
    upsert_user(user.id, user.username or "", user.first_name or "Trader", ref)
    await send_home(update.message, user, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"⚠️ Exception: {context.error}")

def main():
    init_db()
    Thread(target=run_health_server, daemon=True).start()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(error_handler)

    logger.info(f"🌬️ {BOT_NAME} starting...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
