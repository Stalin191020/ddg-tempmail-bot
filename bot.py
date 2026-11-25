import os
import html
import time
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CopyTextButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    AIORateLimiter,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from database import Database
from ddg_client import DuckAliasClient
from imap_worker import run_imap_background_loop
from ai_helper import UniversalAIClient

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@Admin")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "").strip()
IST = timezone(timedelta(hours=5, minutes=30))

db = Database(DB_PATH)
ddg = DuckAliasClient(db)
ai_client = UniversalAIClient()

user_locks: Dict[int, asyncio.Lock] = {}
imap_task: Optional[asyncio.Task] = None
PAGE_SIZE = 5

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["➕ New Email", "📧 Active Mails"],
        ["📥 Check Inbox", "🗑️ Delete Email"],
        ["💎 Subscription"],
    ],
    resize_keyboard=True,
)

TERMS_TEXT = (
    "🛡️ <b>Terms of Use & Privacy Notice</b>\n\n"
    "Welcome! Before generating temporary emails, please review our policy:\n"
    "• <b>Purpose:</b> Strictly for personal privacy & app/website testing.\n"
    "• <b>Prohibited Use:</b> Banking, financial wallets, crypto, government portals, or any illegal activity is strictly forbidden.\n"
    "• <b>Receive-Only:</b> Outgoing emails are disabled.\n"
    "• <b>Security Compliance:</b> User IDs and alias timestamps are logged.\n\n"
    "Tap <b>✅ I Agree & Continue</b> below to unlock the bot."
)


def get_lock(user_id: int) -> asyncio.Lock:
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]


def time_ago(ts: float) -> str:
    diff = int(time.time() - ts)
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


async def send_audit_channel(bot, text: str):
    if not LOG_CHANNEL_ID:
        return
    try:
        await bot.send_message(
            chat_id=int(LOG_CHANNEL_ID),
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[Log Channel Error] {e}")


async def check_user_gate(update: Update) -> bool:
    """Enforces ban check and Terms agreement gate."""
    user = update.effective_user
    if not user:
        return False
    await db.register_user(user.id, user.username)
    agreed, banned = await db.get_security_status(user.id)

    if banned:
        if update.message:
            await update.message.reply_text(
                "🚫 <b>Access Suspended</b>\nYour account has been banned for violating our Terms of Use.",
                parse_mode=ParseMode.HTML,
            )
        return False

    if not agreed:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ I Agree & Continue", callback_data="accept_terms")]]
        )
        if update.message:
            await update.message.reply_text(
                TERMS_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb
            )
        return False

    return True


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_gate(update):
        return
    user = update.effective_user
    active, max_slots, _ = await db.get_user_quota(user.id)
    text = (
        f"👋 <b>Welcome, {html.escape(user.first_name)}!</b>\n\n"
        "Generate instant temporary emails (<code>@duck.com</code> & <code>@gmail.com</code>).\n"
        "OTPs arrive automatically here or via <b>📥 Check Inbox</b>.\n\n"
        f"📊 <b>Your Active Slots:</b> <code>{active}/{max_slots}</code>"
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD
    )


# --- 1. New Email (Provider Choice: DDG vs Gmail Alias) ---
async def handle_new_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_gate(update):
        return
    user_id = update.effective_user.id
    active, max_slots, _ = await db.get_user_quota(user_id)

    if active >= max_slots:
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🗑️ Delete an Old Email", callback_data="menu_del:0")],
                [InlineKeyboardButton("💎 Upgrade Limit (Up to 50)", callback_data="menu_sub")],
            ]
        )
        await update.message.reply_text(
            f"⚠️ <b>Slot Limit Reached ({active}/{max_slots})</b>\n\n"
            "Delete one of your active addresses first to generate a new email, "
            "or upgrade your plan (up to <b>50 active emails</b>).",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🦆 @duck.com (Anonymous)", callback_data="gen:ddg"),
                InlineKeyboardButton("📧 @gmail.com (High Accept)", callback_data="gen:gmail"),
            ]
        ]
    )
    await update.message.reply_text(
        "✨ <b>Choose Email Provider:</b>\n\n"
        "• <b>🦆 @duck.com:</b> 100% random anonymous address.\n"
        "• <b>📧 @gmail.com:</b> Real Gmail alias (accepted everywhere).",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def create_alias_for_user(query, context: ContextTypes.DEFAULT_TYPE, provider: str):
    user = query.from_user
    user_id = user.id
    agreed, banned = await db.get_security_status(user_id)
    if banned or not agreed:
        await query.edit_message_text("⚠️ Please send /start and accept the Terms first.")
        return

    async with get_lock(user_id):
        active, max_slots, _ = await db.get_user_quota(user_id)
        if active >= max_slots:
            await query.edit_message_text(
                f"⚠️ <b>Slot Limit Reached ({active}/{max_slots})</b>\nDelete an old email first.",
                parse_mode=ParseMode.HTML,
            )
            return

        label = "DuckDuckGo" if provider == "ddg" else "Gmail Alias"
        await query.edit_message_text(
            f"⏳ Generating new <b>{label}</b> address...", parse_mode=ParseMode.HTML
        )

        try:
            if provider == "gmail":
                res = await ddg.generate_gmail_alias()
            else:
                res = await ddg.generate_alias()

            await db.add_email(user_id, res["email"], res["alias_id"], res["token_id"])
            uname = f"@{user.username}" if user.username else "NoUsername"
            await db.log_audit_event(user_id, uname, "NEW_ALIAS", res["email"], provider)

            now_ist = datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")
            asyncio.create_task(
                send_audit_channel(
                    context.bot,
                    f"🆕 #NEW_ALIAS | #ID_{user_id}\n"
                    f"👤 <b>User:</b> {html.escape(user.first_name)} ({uname})\n"
                    f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                    f"📧 <b>Alias:</b> <code>{html.escape(res['email'])}</code>\n"
                    f"🕒 <b>Time:</b> {now_ist}",
                )
            )

            new_active = active + 1
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📋 Copy Email",
                            copy_text=CopyTextButton(text=res["email"]),
                        )
                    ],
                    [
                        InlineKeyboardButton("📥 Check Inbox", callback_data="refresh_all"),
                        InlineKeyboardButton("📧 Active Mails", callback_data="menu_active:0"),
                    ],
                ]
            )
            await query.edit_message_text(
                "✅ <b>New Temp Email Created!</b>\n\n"
                f"📧 <code>{html.escape(res['email'])}</code>\n"
                f"📊 <b>Slots Used:</b> <code>{new_active}/{max_slots}</code>\n\n"
                "OTPs will arrive automatically here or via <b>📥 Check Inbox</b>.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception as exc:
            await query.edit_message_text(
                f"❌ <b>Failed to generate email:</b>\n<code>{html.escape(str(exc))}</code>",
                parse_mode=ParseMode.HTML,
            )


# --- 2. Active Emails (Paginated) ---
async def render_active_mails(user_id: int, page: int = 0):
    active, max_slots, expiry = await db.get_user_quota(user_id)
    emails = await db.get_user_emails(user_id)

    plan_label = "Free"
    if max_slots > 3 and expiry:
        exp_str = datetime.utcfromtimestamp(expiry).strftime("%d %b %Y")
        plan_label = f"Premium (Expires {exp_str})"

    if not emails:
        text = (
            f"📊 <b>Active Slots:</b> <code>0/{max_slots}</code> ({plan_label})\n\n"
            "You do not have any active temporary emails right now."
        )
        return text, None

    total_pages = max(1, (len(emails) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = emails[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = [
        f"📊 <b>Active Slots:</b> <code>{active}/{max_slots}</code> ({plan_label})",
        f"📄 <b>Page {page + 1}/{total_pages}</b> — <i>Tap an address to copy:</i>\n",
    ]
    buttons = []

    for idx, item in enumerate(chunk, start=page * PAGE_SIZE + 1):
        lines.append(
            f"{idx}. <code>{html.escape(item['email'])}</code> • <i>{time_ago(item['created_at'])}</i>"
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    f"📥 Check {item['email']}", callback_data=f"check_one:{item['id']}"
                )
            ]
        )

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"menu_active:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"menu_active:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("🔄 Refresh All Inboxes", callback_data="refresh_all")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def handle_active_mails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_gate(update):
        return
    text, kb = await render_active_mails(update.effective_user.id, page=0)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# --- 3. Check Inbox ---
async def format_cached_inbox(target_aliases: list[str]) -> str:
    messages = await db.get_messages_for_aliases(target_aliases, limit=5)
    if not messages:
        return (
            "📭 <b>No new emails or OTPs found yet.</b>\n\n"
            "• Inbox updates automatically every few seconds.\n"
            "• Wait 5–10 seconds after requesting your OTP and tap <b>🔄 Refresh</b>."
        )

    blocks = ["📬 <b>Latest Received Messages:</b>\n"]
    for m in messages:
        otp_line = f"🔑 <b>OTP / Code:</b> <code>{html.escape(m['otp'])}</code>\n" if m["otp"] else ""
        blocks.append(
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📥 <b>To:</b> <code>{html.escape(m['alias'])}</code> ({time_ago(m['received_at'])})\n"
            f"📤 <b>From:</b> {html.escape(m['sender'][:50])}\n"
            f"📝 <b>Subject:</b> {html.escape(m['subject'][:80])}\n"
            f"{otp_line}"
            f"💬 <b>Message:</b>\n<pre>{html.escape(m['body'][:400])}</pre>"
        )
    return "\n".join(blocks)


async def handle_check_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_gate(update):
        return
    user_id = update.effective_user.id
    emails = await db.get_user_emails(user_id)
    if not emails:
        await update.message.reply_text(
            "You have no active emails. Tap <b>➕ New Email</b> first.",
            parse_mode=ParseMode.HTML,
        )
        return

    res_text = await format_cached_inbox([e["email"] for e in emails])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh Again", callback_data="refresh_all")]])
    await update.message.reply_text(res_text, parse_mode=ParseMode.HTML, reply_markup=kb)


# --- 4. Delete Email ---
async def render_delete_menu(user_id: int, page: int = 0):
    emails = await db.get_user_emails(user_id)
    if not emails:
        return "📭 You have no active emails to delete.", None

    total_pages = max(1, (len(emails) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = emails[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    buttons = [
        [InlineKeyboardButton(f"🗑️ {item['email']}", callback_data=f"del_ask:{item['id']}")]
        for item in chunk
    ]
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"menu_del:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"menu_del:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    text = (
        "🗑️ <b>Delete Active Email</b>\n\n"
        "Choose which email address you want to deactivate to free up a slot:"
    )
    return text, InlineKeyboardMarkup(buttons)


async def handle_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_gate(update):
        return
    text, kb = await render_delete_menu(update.effective_user.id, page=0)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# --- 5. Subscription Info ---
async def handle_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_gate(update):
        return
    user_id = update.effective_user.id
    active, max_slots, _ = await db.get_user_quota(user_id)
    text = (
        "💎 <b>Subscription Plans (3 up to 50 Slots)</b>\n\n"
        f"📊 <b>Current Quota:</b> <code>{active}/{max_slots}</code> active emails\n"
        f"🆔 <b>Your User ID:</b> <code>{user_id}</code>\n\n"
        "• <b>Free:</b> 3 active emails\n"
        "• <b>Basic:</b> 10 active emails\n"
        "• <b>Pro:</b> 25 active emails\n"
        "• <b>Ultra:</b> 50 active emails\n\n"
        f"Message {html.escape(ADMIN_USERNAME)} with your User ID to activate a plan."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# --- Inline Callbacks ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_id = user.id
    data = query.data or ""

    if data == "accept_terms":
        await db.set_user_agreed(user_id, user.username)
        await query.edit_message_text(
            "✅ <b>Terms Accepted!</b>\nYou can now generate temporary emails using the menu below.",
            parse_mode=ParseMode.HTML,
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="👇 Choose an option from the keyboard:",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if data.startswith("gen:"):
        provider = data.split(":", 1)[1]
        await create_alias_for_user(query, context, provider)

    elif data.startswith("menu_active:"):
        page = int(data.split(":")[1])
        text, kb = await render_active_mails(user_id, page)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data.startswith("menu_del:"):
        page = int(data.split(":")[1])
        text, kb = await render_delete_menu(user_id, page)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "menu_sub":
        active, max_slots, _ = await db.get_user_quota(user_id)
        await query.edit_message_text(
            "💎 <b>Subscription Plans (3 up to 50 Slots)</b>\n\n"
            f"📊 <b>Current Quota:</b> <code>{active}/{max_slots}</code>\n"
            f"🆔 <b>Your User ID:</b> <code>{user_id}</code>\n\n"
            "• <b>Basic:</b> 10 active emails\n"
            "• <b>Pro:</b> 25 active emails\n"
            "• <b>Ultra:</b> 50 active emails\n\n"
            f"Contact {html.escape(ADMIN_USERNAME)} with your User ID to upgrade.",
            parse_mode=ParseMode.HTML,
        )

    elif data == "refresh_all":
        emails = await db.get_user_emails(user_id)
        if not emails:
            await query.edit_message_text("📭 No active emails found.")
            return
        res_text = await format_cached_inbox([e["email"] for e in emails])
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh Again", callback_data="refresh_all")]])
        try:
            await query.edit_message_text(res_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            pass

    elif data.startswith("check_one:"):
        row_id = int(data.split(":")[1])
        item = await db.get_email_by_id(row_id, user_id)
        if not item:
            await query.edit_message_text("⚠️ This email no longer exists.")
            return
        res_text = await format_cached_inbox([item["email"]])
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Refresh This Mail", callback_data=f"check_one:{row_id}")],
                [InlineKeyboardButton("🗑️ Delete This Mail", callback_data=f"del_ask:{row_id}")],
                [InlineKeyboardButton("⬅️ Back to Active Mails", callback_data="menu_active:0")],
            ]
        )
        try:
            await query.edit_message_text(res_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            pass

    elif data.startswith("del_ask:"):
        row_id = int(data.split(":")[1])
        item = await db.get_email_by_id(row_id, user_id)
        if not item:
            await query.edit_message_text("📭 Email already deleted.")
            return
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Confirm Delete", callback_data=f"del_yes:{row_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data="menu_del:0"),
                ]
            ]
        )
        await query.edit_message_text(
            f"⚠️ <b>Confirm Deletion</b>\n\n"
            f"Permanently deactivate <code>{html.escape(item['email'])}</code>?\n"
            "1 slot will be freed immediately.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

    elif data.startswith("del_yes:"):
        row_id = int(data.split(":")[1])
        item = await db.get_email_by_id(row_id, user_id)
        if not item:
            await query.edit_message_text("📭 Email already deleted.")
            return
        await ddg.delete_alias(item["alias_id"], item["token_id"])
        await db.delete_email_record(row_id, user_id)
        active, max_slots, _ = await db.get_user_quota(user_id)
        await query.edit_message_text(
            f"✅ <b>Deleted:</b> <code>{html.escape(item['email'])}</code>\n"
            f"📊 <b>Slots Used:</b> <code>{active}/{max_slots}</code>",
            parse_mode=ParseMode.HTML,
        )


# --- Admin Commands ---
async def addprem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or l
