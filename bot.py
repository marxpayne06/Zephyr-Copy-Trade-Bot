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
REFERRAL_BONUS    = 0.01

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# HEALTH SERVER (Optimized for Render & UptimeRobot)
# ─────────────────────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Zephyr bot is alive!")
    def log_message(self, *args):
        pass

def run_health_server():
    # Render binds to a dynamic port; we must use the PORT env var
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"✅ Health server online on port {port}")
    server.serve_forever()

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
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

def get_monthly_user_count():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM users WHERE joined_at >= datetime('now','-30 days')").fetchone()[0]
    conn.close(); return n

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
# SOLANA UTILS
# ─────────────────────────────────────────────────────────────────────────────

try:
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    SOLDERS_OK = True
except ImportError:
    SOLDERS_OK = False

SOL_MINT = "So11111111111111111111111111111111111111112"

def create_wallet():
    if SOLDERS_OK:
        kp = Keypair()
        return str(kp.pubkey()), base58.b58encode(bytes(kp)).decode()
    import os as _os; seed = _os.urandom(32)
    return base58.b58encode(seed[::-1]).decode()[:44], base58.b58encode(seed).decode()

def keypair_from_pk(pk_b58):
    if not SOLDERS_OK: raise RuntimeError("solders not installed")
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
            tx   = VersionedTransaction.from_bytes(base64.b64decode(s["swapTransaction"]))
            res  = (await c.post(SOLANA_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"sendTransaction",
                "params":[base64.b64encode(bytes(tx)).decode(),{"encoding":"base64"}]})).json()
            sig  = res.get("result")
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
            ix  = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Pubkey.from_string(to_address), lamports=int(amount_sol*1e9)))
            tx  = Transaction([kp], Message([ix], kp.pubkey()), Hash.from_string(blockhash))
            res = (await c.post(SOLANA_RPC_URL, json={"jsonrpc":"2.0","id":1,"method":"sendTransaction",
                "params":[base64.b64encode(bytes(tx)).decode(),{"encoding":"base64"}]})).json()
            sig = res.get("result")
            return {"success":True,"tx_signature":sig} if sig else {"success":False,"error":res.get("error",{}).get("message","Unknown")}
    except Exception as e: return {"success":False,"error":str(e)}

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

CA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

def fmt(n, pre="$"):
    if n is None: return "N/A"
    n = float(n)
    if n >= 1_000_000: return f"{pre}{n/1_000_000:.2f}M"
    if n >= 1_000:     return f"{pre}{n/1_000:.1f}K"
    return f"{pre}{n:.6f}" if n < 0.01 else f"{pre}{n:.4f}"

def bk(cb="back_home"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=cb)]])

async def edit(query, text, kb=None, md=True):
    kw = dict(parse_mode=ParseMode.MARKDOWN if md else None, reply_markup=kb or bk())
    try:    await query.edit_message_caption(caption=text, **kw)
    except: await query.edit_message_text(text=text, **kw)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN MENU
# ─────────────────────────────────────────────────────────────────────────────

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👛  Wallet Management", callback_data="wallet_menu")],
        [InlineKeyboardButton("🛒  Buy",  callback_data="buy_start"),
         InlineKeyboardButton("💸  Sell", callback_data="sell_start")],
        [InlineKeyboardButton("💰  Withdraw SOL",      callback_data="withdraw_start")],
        [InlineKeyboardButton("📊  My Portfolio",      callback_data="portfolio_view"),
         InlineKeyboardButton("📈  Generate PnL",      callback_data="pnl_generate")],
        [InlineKeyboardButton("🔄  Swap Token",        callback_data="swap_start")],
        [InlineKeyboardButton("🔔  Token Monitor",     callback_data="monitor_menu")],
        [InlineKeyboardButton("📉  Trending",          callback_data="trending_menu")],
        [InlineKeyboardButton("⚙️  Settings",          callback_data="settings_menu"),
         InlineKeyboardButton("📚  FAQ",               callback_data="faq_menu")],
        [InlineKeyboardButton("🤝  Referral",          callback_data="referral_menu")],
    ])

async def send_home(target, user, context):
    wallet  = get_wallet(user.id)
    monthly = get_monthly_user_count()
    bal     = f"\n💼 *Wallet:* `{wallet['public_key'][:6]}...{wallet['public_key'][-4:]}`" if wallet else ""
    text    = (f"👋 Welcome, *{user.first_name}* to *{BOT_NAME}*!\n\n"
               f"⚡ *Makes swaps faster and safer on Solana.*\n"
               f"Just paste any *CA* to start trading tokens instantly.\n"
               f"{bal}\n\n👥 *{monthly:,}* active users this month")
    if hasattr(target, "reply_photo"):
        try:
            await target.reply_photo(photo=WELCOME_PHOTO_URL, caption=text,
                                     parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb()); return
        except: pass
        await target.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())
    else:
        await edit(target, text, main_menu_kb())

# ─────────────────────────────────────────────────────────────────────────────
# COMMAND HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ref  = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            rid = int(context.args[0][4:])
            if rid != user.id: add_referral(rid, user.id); ref = rid
        except ValueError: pass
    upsert_user(user.id, user.username or "", user.first_name or "Trader", ref)
    await send_home(update.message, user, context)

# ─────────────────────────────────────────────────────────────────────────────
# CALLBACK HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    d = query.data; uid = query.from_user.id

    if d == "back_home":
        await send_home(query, query.from_user, context)

    elif d == "wallet_menu":
        w = get_wallet(uid); st = "✅ Connected" if w else "❌ No wallet yet"
        await edit(query, f"👛 *Wallet Management*\n\nStatus: {st}\n\nCreate or import a wallet below.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🆕 Create New Wallet",  callback_data="wallet_create")],
                [InlineKeyboardButton("📥 Import Wallet",      callback_data="wallet_import")],
                [InlineKeyboardButton("👁️ View Address",       callback_data="wallet_view")],
                [InlineKeyboardButton("🔑 Export Private Key", callback_data="wallet_export")],
                [InlineKeyboardButton("⬅️ Back",               callback_data="back_home")],
            ]))
    elif d == "wallet_create":
        pub, priv = create_wallet(); save_wallet(uid, pub, priv)
        await edit(query, f"✅ *Wallet Created!*\n\n📬 *Address:*\n`{pub}`\n\n🔑 *Private Key:*\n`{priv}`\n\n⚠️ Save your key — won't be shown again.")
    elif d == "wallet_import":
        context.user_data["awaiting"] = "import_wallet"
        await edit(query, "📥 *Import Wallet*\n\nSend your *private key* (base58 format).")
    elif d == "wallet_view":
        w = get_wallet(uid)
        if not w: await edit(query, "❌ No wallet found."); return
        bal = await get_sol_balance(w["public_key"])
        await edit(query, f"👛 *Your Wallet*\n\n📬 `{w['public_key']}`\n\n💰 Balance: `{bal:.4f} SOL`",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="wallet_view"),
                                   InlineKeyboardButton("⬅️ Back",    callback_data="wallet_menu")]]))
    elif d == "wallet_export":
        w = get_wallet(uid)
        if not w: await edit(query, "❌ No wallet found."); return
        await edit(query, f"🔑 *Private Key*\n\n`{w['private_key']}`\n\n⚠️ Never share this with anyone.")

    # ── BUY ──────────────────────────────────────────────────────────────────
    elif d == "buy_start":
        if not get_wallet(uid): await edit(query, "❌ No wallet. Create one in Wallet Management."); return
        context.user_data["awaiting"] = "buy_ca"
        await edit(query, "🛒 *Buy Token*\n\nPaste the token *Contract Address (CA)*:")
    elif d.startswith("buy_amount_"):
        amt = float(d[11:]); context.user_data["trade_amount"] = amt
        ca = context.user_data.get("trade_ca",""); info = await get_token_price_dex(ca)
        name = info["name"] if info else ca[:10]+"..."
        await edit(query, f"🛒 *Confirm Buy*\n\nToken: *{name}*\nAmount: *{amt} SOL*\n\nProceed?",
            InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data="buy_confirm"),
                                   InlineKeyboardButton("❌ Cancel",  callback_data="back_home")]]))
    elif d == "buy_confirm":
        ca = context.user_data.get("trade_ca"); amt = context.user_data.get("trade_amount",0.1)
        w = get_wallet(uid); u = get_user(uid) or {}
        await edit(query, "⏳ Executing buy...")
        res = await jupiter_swap(w["private_key"], SOL_MINT, ca, int(amt*1e9), u.get("slippage",1.0))
        await edit(query, f"✅ *Buy Successful!*\n\n🔗 [Solscan](https://solscan.io/tx/{res['tx_signature']})" if res["success"] else f"❌ *Buy Failed*\n\n`{res['error']}`")

    # ── SELL ─────────────────────────────────────────────────────────────────
    elif d == "sell_start":
        if not get_wallet(uid): await edit(query, "❌ No wallet found."); return
        context.user_data["awaiting"] = "sell_ca"
        await edit(query, "💸 *Sell Token*\n\nPaste the token *Contract Address (CA)*:")
    elif d.startswith("sell_pct_"):
        pct = int(d[9:]); context.user_data["sell_pct"] = pct
        ca = context.user_data.get("trade_ca",""); info = await get_token_price_dex(ca)
        name = info["name"] if info else ca[:10]+"..."
        await edit(query, f"💸 *Confirm Sell*\n\nToken: *{name}*\nSelling: *{pct}%*\n\nProceed?",
            InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data="sell_confirm"),
                                   InlineKeyboardButton("❌ Cancel",  callback_data="back_home")]]))
    elif d == "sell_confirm":
        ca = context.user_data.get("trade_ca"); pct = context.user_data.get("sell_pct",100)
        w = get_wallet(uid); u = get_user(uid) or {}
        await edit(query, "⏳ Executing sell...")
        accs = await get_token_accounts(w["public_key"])
        acc  = next((a for a in accs if a["mint"]==ca), None)
        if not acc: await edit(query, "❌ Token not found in wallet."); return
        units = int(acc["amount"] * (pct/100) * (10**acc["decimals"]))
        res   = await jupiter_swap(w["private_key"], ca, SOL_MINT, units, u.get("slippage",1.0))
        await edit(query, f"✅ *Sell Successful!*\n\n🔗 [Solscan](https://solscan.io/tx/{res['tx_signature']})" if res["success"] else f"❌ *Sell Failed*\n\n`{res['error']}`")

    # ── SWAP ─────────────────────────────────────────────────────────────────
    elif d == "swap_start":
        context.user_data["awaiting"] = "swap_from_ca"
        await edit(query, "🔄 *Swap Token*\n\nPaste the *input token CA* (or type `SOL`):")
    elif d == "swap_confirm":
        fc = context.user_data.get("swap_from",""); tc = context.user_data.get("swap_to","")
        amt = context.user_data.get("swap_amount",0)
        w = get_wallet(uid); u = get_user(uid) or {}
        im = SOL_MINT if fc.upper()=="SOL" else fc; om = SOL_MINT if tc.upper()=="SOL" else tc
        await edit(query, "⏳ Executing swap...")
        res = await jupiter_swap(w["private_key"], im, om, int(amt*1e9) if im==SOL_MINT else int(amt), u.get("slippage",1.0))
        await edit(query, f"✅ *Swap Successful!*\n\n🔗 [Solscan](https://solscan.io/tx/{res['tx_signature']})" if res["success"] else f"❌ *Swap Failed*\n\n`{res['error']}`")

    # ── WITHDRAW ─────────────────────────────────────────────────────────────
    elif d == "withdraw_start":
        w = get_wallet(uid)
        if not w: await edit(query, "❌ No wallet found."); return
        bal = await get_sol_balance(w["public_key"])
        context.user_data["awaiting"] = "withdraw_address"
        await edit(query, f"💰 *Withdraw SOL*\n\nAvailable: `{bal:.4f} SOL`\n\nSend the *destination address*:")
    elif d == "withdraw_confirm":
        to = context.user_data.get("withdraw_to"); amt = context.user_data.get("withdraw_amount",0)
        w = get_wallet(uid)
        await edit(query, "⏳ Sending SOL...")
        res = await send_sol(w["private_key"], to, amt)
        await edit(query, f"✅ *Sent `{amt} SOL`*\n\n🔗 [Solscan](https://solscan.io/tx/{res['tx_signature']})" if res["success"] else f"❌ *Failed*\n\n`{res['error']}`")

    # ── PORTFOLIO ─────────────────────────────────────────────────────────────
    elif d == "portfolio_view":
        w = get_wallet(uid)
        if not w: await edit(query, "❌ No wallet found."); return
        await edit(query, "📊 Loading portfolio...")
        sol  = await get_sol_balance(w["public_key"])
        toks = await get_token_accounts(w["public_key"])
        lines = [f"📊 *Your Portfolio*\n\n💰 SOL: `{sol:.4f}`\n"]
        if toks:
            infos = await asyncio.gather(*[get_token_price_dex(t["mint"]) for t in toks[:8]])
            total = 0.0
            for i, t in enumerate(toks[:8]):
                inf = infos[i]; name = inf["name"] if inf else t["mint"][:8]+"..."
                if inf and inf.get("price_usd"):
                    val = t["amount"] * float(inf["price_usd"]); total += val
                    lines.append(f"• *{name}*: `{t['amount']:.2f}` ≈ `${val:.2f}`")
                else: lines.append(f"• *{name}*: `{t['amount']:.2f}`")
            if total: lines.append(f"\n💎 *Est. Total:* `${total:.2f}`")
        else: lines.append("_No SPL tokens found._")
        await edit(query, "\n".join(lines),
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="portfolio_view"),
                                   InlineKeyboardButton("⬅️ Back",    callback_data="back_home")]]))

    # ── TRENDING ─────────────────────────────────────────────────────────────
    elif d == "trending_menu":
        await edit(query, "📉 *Trending*\n\nChoose a source:",
            InlineKeyboardMarkup([[InlineKeyboardButton("🦅 DEXScreener", callback_data="trending_dex")],
                                  [InlineKeyboardButton("🦎 CoinGecko",   callback_data="trending_cg")],
                                  [InlineKeyboardButton("⬅️ Back",         callback_data="back_home")]]))
    elif d == "trending_dex":
        await edit(query, "⏳ Fetching...")
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                items = (await c.get("https://api.dexscreener.com/token-boosts/top/v1")).json()[:10]
            lines = ["🦅 *DEXScreener — Top Trending*\n"]
            for i, t in enumerate(items, 1):
                ca = t.get("tokenAddress","")
                lines.append(f"{i}. *{t.get('description','?')[:20]}* ({t.get('chainId','?').upper()})\n   `{ca[:6]}...{ca[-4:]}` 🔥 {t.get('totalAmount',0)}")
            await edit(query, "\n".join(lines),
                InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="trending_dex"),
                                       InlineKeyboardButton("⬅️ Back",    callback_data="trending_menu")]]))
        except: await edit(query, "❌ Could not fetch DEXScreener data.")
    elif d == "trending_cg":
        await edit(query, "⏳ Fetching...")
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                coins = (await c.get("https://api.coingecko.com/api/v3/search/trending")).json().get("coins",[])[:8]
            lines = ["🦎 *CoinGecko — Top Trending*\n"]
            for i, c in enumerate(coins, 1):
                item = c.get("item",{}); rank = f"#{item.get('market_cap_rank','?')}"
                lines.append(f"{i}. *{item.get('name','?')}* (${item.get('symbol','?')}) — {rank}")
            await edit(query, "\n".join(lines),
                InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="trending_cg"),
                                       InlineKeyboardButton("⬅️ Back",    callback_data="trending_menu")]]))
        except: await edit(query, "❌ Could not fetch CoinGecko data.")

    # ── MONITOR ──────────────────────────────────────────────────────────────
    elif d == "monitor_menu":
        mons = get_monitors(uid)
        lines = ["🔔 *Token Monitor*\n"]
        for m in mons:
            lines.append(f"• *{m['token_name']}* — {'📈 above' if m['direction']=='above' else '📉 below'} `${m['target_price']}`")
        if not mons: lines.append("_No active monitors._")
        await edit(query, "\n".join(lines),
            InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add",    callback_data="monitor_add")],
                                  [InlineKeyboardButton("🗑️ Remove", callback_data="monitor_remove")],
                                  [InlineKeyboardButton("⬅️ Back",   callback_data="back_home")]]))
    elif d == "monitor_add":
        context.user_data["awaiting"] = "monitor_ca"
        await edit(query, "🔔 *Add Monitor*\n\nPaste the token *CA*:")
    elif d.startswith("monitor_dir_"):
        direction = d[12:]; ca = context.user_data.get("monitor_ca",""); price = context.user_data.get("monitor_price",0)
        info = await get_token_price_dex(ca); name = info["name"] if info else ca[:8]+"..."
        add_monitor(uid, ca, name, price, direction)
        await edit(query, f"✅ *Monitor Set!*\n\n*{name}* — alert {'📈 above' if direction=='above' else '📉 below'} `${price}`")
    elif d == "monitor_remove":
        mons = get_monitors(uid)
        if not mons: await edit(query, "❌ No monitors to remove."); return
        btns = [[InlineKeyboardButton(f"🗑️ {m['token_name']} — ${m['target_price']}", callback_data=f"monitor_del_{m['id']}")] for m in mons]
        btns.append([InlineKeyboardButton("⬅️ Back", callback_data="monitor_menu")])
        await edit(query, "🗑️ Select monitor to remove:", InlineKeyboardMarkup(btns))
    elif d.startswith("monitor_del_"):
        deactivate_monitor(int(d[12:])); await edit(query, "✅ Monitor removed.")

    # ── SETTINGS ─────────────────────────────────────────────────────────────
    elif d == "settings_menu":
        u = get_user(uid) or {}
        await edit(query, f"⚙️ *Settings*\n\n🔀 Slippage: `{u.get('slippage',1.0)}%`\n⛽ Gas: `{u.get('gas_fee','medium').capitalize()}`",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔀 Slippage", callback_data="settings_slippage")],
                                  [InlineKeyboardButton("⛽ Gas Fee",  callback_data="settings_gas")],
                                  [InlineKeyboardButton("⬅️ Back",     callback_data="back_home")]]))
    elif d == "settings_slippage":
        await edit(query, "🔀 *Set Slippage Tolerance:*",
            InlineKeyboardMarkup([[InlineKeyboardButton("0.5%", callback_data="settings_slip_0.5"),
                                   InlineKeyboardButton("1%",   callback_data="settings_slip_1.0"),
                                   InlineKeyboardButton("2%",   callback_data="settings_slip_2.0"),
                                   InlineKeyboardButton("5%",   callback_data="settings_slip_5.0")],
                                  [InlineKeyboardButton("⬅️ Back", callback_data="settings_menu")]]))
    elif d.startswith("settings_slip_"):
        val = float(d[14:]); update_settings(uid, slippage=val); await edit(query, f"✅ Slippage set to `{val}%`")
    elif d == "settings_gas":
        await edit(query, "⛽ *Set Gas Priority:*",
            InlineKeyboardMarkup([[InlineKeyboardButton("🐢 Low",    callback_data="settings_gas_low"),
                                   InlineKeyboardButton("⚡ Medium", callback_data="settings_gas_medium"),
                                   InlineKeyboardButton("🚀 High",   callback_data="settings_gas_high")],
                                  [InlineKeyboardButton("⬅️ Back",   callback_data="settings_menu")]]))
    elif d.startswith("settings_gas_"):
        level = d[13:]; update_settings(uid, gas_fee=level); await edit(query, f"✅ Gas set to `{level.capitalize()}`")

    # ── FAQ ───────────────────────────────────────────────────────────────────
    elif d == "faq_menu":
        qs = ["What is Zephyr?","How do I start trading?","Is my private key safe?",
              "What is slippage?","What are gas fees?","How does Token Monitor work?","How do referrals work?"]
        btns = [[InlineKeyboardButton(f"❓ {q}", callback_data=f"faq_{i}")] for i,q in enumerate(qs)]
        btns.append([InlineKeyboardButton("⬅️ Back", callback_data="back_home")])
        await edit(query, "📚 *FAQ — Choose a topic:*", InlineKeyboardMarkup(btns))
    elif d.startswith("faq_") and d[4:].isdigit():
        ans = [
            ("What is Zephyr?","Zephyr Copy Trade Bot lets you buy, sell, and swap Solana tokens directly in Telegram by pasting a CA."),
            ("How do I start trading?","1. Create or import a wallet.\n2. Fund it with SOL.\n3. Paste any token CA to trade instantly."),
            ("Is my private key safe?","Your key is stored in the bot database. Never share it with anyone. You are responsible for its security."),
            ("What is slippage?","Max price difference accepted between quote and execution. Higher = more fills but worse price. Set in ⚙️ Settings."),
            ("What are gas fees?","Priority fees paid to Solana validators. Higher gas = faster confirmation."),
            ("How does Token Monitor work?","Set a price target for any token. Zephyr alerts you when it crosses that level."),
            ("How do referrals work?","Share your link. Earn SOL for every new trader who signs up through it."),
        ]
        q, a = ans[int(d[4:])]
        await edit(query, f"❓ *{q}*\n\n{a}", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to FAQ", callback_data="faq_menu")]]))

    # ── REFERRAL ─────────────────────────────────────────────────────────────
    elif d == "referral_menu":
        count = get_referral_count(uid)
        bot   = await context.bot.get_me()
        link  = f"https://t.me/{bot.username}?start=ref_{uid}"
        await edit(query, f"🤝 *Referral Program*\n\nEarn SOL for every trader you bring!\n\n👥 Referrals: *{count}*\n\n🔗 Your link:\n`{link}`",
            InlineKeyboardMarkup([[InlineKeyboardButton("📤 Share", switch_inline_query=f"Join {BOT_NAME}! {link}")],
                                  [InlineKeyboardButton("⬅️ Back",  callback_data="back_home")]]))

# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip(); uid = update.effective_user.id; state = context.user_data.get("awaiting")
    def bkb(cb): return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=cb)]])
    pm = ParseMode.MARKDOWN

    if state == "import_wallet":
        try:
            kp = keypair_from_pk(text); pub = str(kp.pubkey()); save_wallet(uid, pub, text)
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(f"✅ *Wallet Imported!*\n\n📬 `{pub}`", parse_mode=pm, reply_markup=bkb("wallet_menu"))
        except Exception as e: await update.message.reply_text(f"❌ Invalid key: `{e}`", parse_mode=pm)
        return

    if state == "buy_ca" and CA_RE.match(text):
        context.user_data.update({"trade_ca":text,"awaiting":None})
        info = await get_token_price_dex(text); w = get_wallet(uid); bal = await get_sol_balance(w["public_key"]) if w else 0
        name = info["name"] if info else text[:10]+"..."; price = fmt(info["price_usd"]) if info else "N/A"
        await update.message.reply_text(f"🛒 *Buy {name}*\n\n💲 Price: `{price}`\n💼 Balance: `{bal:.4f} SOL`\n\nHow much SOL?", parse_mode=pm,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("0.1 SOL", callback_data="buy_amount_0.1"), InlineKeyboardButton("0.5 SOL", callback_data="buy_amount_0.5")],
                [InlineKeyboardButton("1 SOL",   callback_data="buy_amount_1.0"), InlineKeyboardButton("2 SOL",   callback_data="buy_amount_2.0")],
                [InlineKeyboardButton("❌ Cancel", callback_data="back_home")]])); return

    if state == "sell_ca" and CA_RE.match(text):
        context.user_data.update({"trade_ca":text,"awaiting":None})
        info = await get_token_price_dex(text); name = info["name"] if info else text[:10]+"..."
        await update.message.reply_text(f"💸 *Sell {name}*\n\nHow much % to sell?", parse_mode=pm,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("25%",  callback_data="sell_pct_25"),  InlineKeyboardButton("50%",  callback_data="sell_pct_50")],
                [InlineKeyboardButton("75%",  callback_data="sell_pct_75"),  InlineKeyboardButton("100%", callback_data="sell_pct_100")],
                [InlineKeyboardButton("❌ Cancel", callback_data="back_home")]])); return

    if state == "swap_from_ca":
        context.user_data.update({"swap_from":text,"awaiting":"swap_to_ca"})
        await update.message.reply_text("🔄 Now paste the *output token CA* (or type `SOL`):", parse_mode=pm, reply_markup=bkb("back_home")); return
    if state == "swap_to_ca":
        context.user_data.update({"swap_to":text,"awaiting":"swap_amount"})
        await update.message.reply_text("🔄 Enter the *amount* to swap:", parse_mode=pm, reply_markup=bkb("back_home")); return
    if state == "swap_amount":
        try:
            context.user_data.update({"swap_amount":float(text),"awaiting":None})
            f = context.user_data.get("swap_from","SOL"); t = context.user_data.get("swap_to","?")
            await update.message.reply_text(f"🔄 *Confirm Swap*\n\nFrom: `{f[:10]}`\nTo: `{t[:10]}`\nAmount: `{text}`", parse_mode=pm,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data="swap_confirm"),
                                                    InlineKeyboardButton("❌ Cancel",  callback_data="back_home")]]))
        except ValueError: await update.message.reply_text("❌ Invalid amount.")
        return

    if state == "withdraw_address" and CA_RE.match(text):
        context.user_data.update({"withdraw_to":text,"awaiting":"withdraw_amount"})
        w = get_wallet(uid); bal = await get_sol_balance(w["public_key"]) if w else 0
        await update.message.reply_text(f"💰 To: `{text}`\nAvailable: `{bal:.4f} SOL`\n\nHow much SOL?", parse_mode=pm, reply_markup=bkb("back_home")); return
    if state == "withdraw_amount":
        try:
            amt = float(text); context.user_data.update({"withdraw_amount":amt,"awaiting":None})
            to  = context.user_data.get("withdraw_to","")
            await update.message.reply_text(f"💰 *Confirm*\n\nSend `{amt} SOL` to:\n`{to}`", parse_mode=pm,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data="withdraw_confirm"),
                                                    InlineKeyboardButton("❌ Cancel",  callback_data="back_home")]]))
        except ValueError: await update.message.reply_text("❌ Enter a number like `0.5`", parse_mode=pm)
        return

    if state == "monitor_ca" and CA_RE.match(text):
        context.user_data.update({"monitor_ca":text,"awaiting":"monitor_price"})
        info = await get_token_price_dex(text); name = info["name"] if info else text[:8]+"..."; cur = fmt(info["price_usd"]) if info else "N/A"
        await update.message.reply_text(f"🔔 *{name}*\nNow: `{cur}`\n\nEnter *target price in USD*:", parse_mode=pm, reply_markup=bkb("monitor_menu")); return
    if state == "monitor_price":
        try:
            price = float(text); context.user_data.update({"monitor_price":price,"awaiting":None})
            await update.message.reply_text("🔔 Alert when price goes:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📈 Above", callback_data="monitor_dir_above"),
                                                    InlineKeyboardButton("📉 Below", callback_data="monitor_dir_below")]]))
        except ValueError: await update.message.reply_text("❌ Enter a number like `0.005`", parse_mode=pm)
        return

    if CA_RE.match(text):
        msg = await update.message.reply_text("🔍 Looking up token...")
        info = await get_token_price_dex(text)
        if not info: await msg.edit_text(f"❌ Token not found.\n\nCA: `{text}`", parse_mode=pm); return
        c1 = info.get("change_1h"); c24 = info.get("change_24h")
        s1  = ("🟢 +" if c1  and float(c1)  >= 0 else "🔴 ") + (f"{float(c1):.2f}%"  if c1  else "N/A")
        s24 = ("🟢 +" if c24 and float(c24) >= 0 else "🔴 ") + (f"{float(c24):.2f}%" if c24 else "N/A")
        await msg.edit_text(
            f"🪙 *{info['name']}* (${info['symbol']})\n\n"
            f"💲 Price:      `{fmt(info['price_usd'])}`\n"
            f"💎 Market Cap: `{fmt(info['market_cap'])}`\n"
            f"📊 Volume 24h: `{fmt(info['volume_24h'])}`\n"
            f"💧 Liquidity:  `{fmt(info['liquidity'])}`\n"
            f"📈 1h:  {s1}\n📉 24h: {s24}\n\n`{text}`",
            parse_mode=pm,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Buy",         callback_data="buy_start"),
                 InlineKeyboardButton("💸 Sell",        callback_data="sell_start")],
                [InlineKeyboardButton("🔔 Set Alert",   callback_data="monitor_menu"),
                 InlineKeyboardButton("🔗 DEXScreener", url=info.get("pair_url","https://dexscreener.com"))],
                [InlineKeyboardButton("⬅️ Home",        callback_data="back_home")]]))

# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND JOB & ERROR HANDLING
# ─────────────────────────────────────────────────────────────────────────────

async def check_monitors(context: ContextTypes.DEFAULT_TYPE):
    for m in get_all_active_monitors():
        info = await get_token_price_dex(m["token_ca"])
        if not info or not info.get("price_usd"): continue
        cur = float(info["price_usd"]); tgt = float(m["target_price"])
        if (m["direction"]=="above" and cur>=tgt) or (m["direction"]=="below" and cur<=tgt):
            deactivate_monitor(m["id"])
            try:
                await context.bot.send_message(chat_id=m["user_id"], parse_mode=ParseMode.MARKDOWN,
                    text=(f"🔔 *Price Alert!*\n\n{'📈' if m['direction']=='above' else '📉'} *{m['token_name']}* crossed your target!\n\n"
                          f"🎯 Target: `${tgt}`\n💲 Now: `${cur:.6f}`"),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy",  callback_data="buy_start"),
                                                        InlineKeyboardButton("💸 Sell", callback_data="sell_start")]]))
            except: pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"⚠️ Exception while handling an update: {context.error}")

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    init_db()
    Thread(target=run_health_server, daemon=True).start()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(error_handler)
    
    # Jobs
    app.job_queue.run_repeating(check_monitors, interval=60, first=10)

    logger.info(f"🌬️ {BOT_NAME} is launching...")
    
    # Render Fix: Use drop_pending_updates to avoid clutter on restart
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
