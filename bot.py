import os
import html
import time
import asyncio
from datetime import datetime
from typing import Dict, Optional
from dotenv import load_dotenv

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@Admin")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

db = Database(DB_PATH)
ddg = DuckAliasClient(db)

user_locks: Dict[int, asyncio.Lock] = {}
imap_task: Optional[asyncio.Task] = None

PAGE_SIZE = 5

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["➕ New Email", "📧 Active Mails"],
        ["🔄 Check Inbox", "🗑️ Delete Email"],
        ["💎 Subscription"],
    ],
    resize_keyboard=True,
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


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.register_user(user.id, user.username)
    active, max_slots, _ = await db.get_user_quota(user.id)

    text = (
        f"👋 <b>Welcome, {html.escape(user.first_name)}!</b>\n\n"
        "Generate private <code>@duck.com</code> temporary emails. "
        "OTPs are pushed automatically and available anytime via <b>Check Inbox</b>.\n\n"
        f"📊 <b>Your Active Slots:</b> <code>{active}/{max_slots}</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)


# --- 1. New Email (3-Slot Free Limit + Up to 50 Subscription) ---
async def handle_new_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await db.register_user(user_id, update.effective_user.username)

    async with get_lock(user_id):
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

        wait_msg = await update.message.reply_text("⏳ Generating new DuckDuckGo address...")
        try:
            res = await ddg.generate_alias()
            await db.add_email(user_id, res["email"], res["alias_id"], res["token_id"])
            new_active = active + 1

            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Check Inbox / OTP", callback_data="refresh_all")],
                    [InlineKeyboardButton("📧 View All Active Mails", callback_data="menu_active:0")],
                ]
            )
            await wait_msg.edit_text(
                "✅ <b>New Temp Email Created!</b>\n\n"
                f"📧 <code>{html.escape(res['email'])}</code> <i>(Tap to copy)</i>\n"
                f"📊 <b>Slots Used:</b> <code>{new_active}/{max_slots}</code>\n\n"
                "OTPs will arrive automatically here or via <b>Check Inbox</b>.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception as exc:
            await wait_msg.edit_text(
                f"❌ <b>Failed to generate email:</b>\n<code>{html.escape(str(exc))}</code>",
                parse_mode=ParseMode.HTML,
            )


# --- 2. Know Active Emails (Paginated for up to 50 slots) ---
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
    text, kb = await render_active_mails(update.effective_user.id, page=0)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# --- 3. Check SMS / Inbox (Instant DB Cache Read — Zero IMAP Bottleneck) ---
async def format_cached_inbox(target_aliases: list[str]) -> str:
    messages = await db.get_messages_for_aliases(target_aliases, limit=5)
    if not messages:
        return (
            "📭 <b>No new emails or OTPs found yet.</b>\n\n"
            "• Inbox updates automatically every 8 seconds.\n"
            "• Wait a few seconds after requesting your OTP and tap <b>🔄 Refresh</b>."
        )

    blocks = ["📬 <b>Latest Received Messages:</b>\n"]
    for m in messages:
        otp_line = f"🔑 <b>OTP / Code:</b> <code>{html.escape(m['otp'])}</code>\n" if m["otp"] else ""
        blocks.append(
            "━━━━━━━━━━━━━━━━━━\n"
            f"📧 <b>To:</b> <code>{html.escape(m['alias'])}</code> ({time_ago(m['received_at'])})\n"
            f"👤 <b>From:</b> {html.escape(m['sender'][:50])}\n"
            f"📝 <b>Subject:</b> {html.escape(m['subject'][:80])}\n"
            f"{otp_line}"
            f"💬 <b>Message:</b>\n<pre>{html.escape(m['body'][:420])}</pre>"
        )
    return "\n".join(blocks)


async def handle_check_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    emails = await db.get_user_emails(user_id)
    if not emails:
        await update.message.reply_text(
            "📭 You have no active emails. Tap <b>➕ New Email</b> first.",
            parse_mode=ParseMode.HTML,
        )
        return

    res_text = await format_cached_inbox([e["email"] for e in emails])
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Refresh Again", callback_data="refresh_all")]]
    )
    await update.message.reply_text(res_text, parse_mode=ParseMode.HTML, reply_markup=kb)


# --- 4. Delete Current Email (Selective Delete + Slot Free) ---
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
        "Choose which email address you want to cancel to free up a slot:"
    )
    return text, InlineKeyboardMarkup(buttons)


async def handle_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = await render_delete_menu(update.effective_user.id, page=0)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# --- 5. Subscription Info ---
async def handle_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    active, max_slots, _ = await db.get_user_quota(user_id)
    text = (
        "💎 <b>Subscription Plans (3+ up to 50 Slots)</b>\n\n"
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
    user_id = query.from_user.id
    data = query.data or ""

    if data.startswith("menu_active:"):
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
            "💎 <b>Subscription Plans (3+ up to 50 Slots)</b>\n\n"
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
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 Refresh Again", callback_data="refresh_all")]]
        )
        try:
            await query.edit_message_text(res_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            pass  # Ignore "Message is not modified" if user clicks rapidly

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
            await query.edit_message_text("⚠️ Email already deleted.")
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
            await query.edit_message_text("⚠️ Email already deleted.")
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
    if update.effective_user.id not in ADMIN_IDS or len(context.args) != 3:
        return
    target_id, slots, days = int(context.args[0]), int(context.args[1]), int(context.args[2])
    await db.set_subscription(target_id, slots, days)
    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> upgraded to <b>{min(50, max(3, slots))} slots</b> for <b>{days} days</b>.",
        parse_mode=ParseMode.HTML,
    )


async def removeprem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or len(context.args) != 1:
        return
    target_id = int(context.args[0])
    await db.remove_subscription(target_id)
    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> reset to <b>3 free slots</b>.",
        parse_mode=ParseMode.HTML,
    )


async def addtoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or len(context.args) != 2:
        return
    await db.upsert_token(context.args[0], context.args[1])
    await update.message.reply_text(
        f"✅ Token <code>{html.escape(context.args[0])}</code> added.", parse_mode=ParseMode.HTML
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    stats = await db.get_global_stats()
    tokens = await db.get_all_tokens()
    now = time.time()
    active_toks = sum(1 for t in tokens if t["is_valid"] == 1 and t["exhausted_until"] <= now)
    await update.message.reply_text(
        "📊 <b>Bot Statistics</b>\n\n"
        f"• Total Users: <b>{stats['users']}</b>\n"
        f"• Premium Users: <b>{stats['premium_users']}</b>\n"
        f"• Active Emails: <b>{stats['active_emails']}</b>\n"
        f"• Healthy DDG Tokens: <b>{active_toks}/{len(tokens)}</b>",
        parse_mode=ParseMode.HTML,
    )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt == "➕ New Email":
        await handle_new_email(update, context)
    elif txt == "📧 Active Mails":
        await handle_active_mails(update, context)
    elif txt == "🔄 Check Inbox":
        await handle_check_inbox(update, context)
    elif txt == "🗑️ Delete Email":
        await handle_delete_menu(update, context)
    elif txt == "💎 Subscription":
        await handle_subscription(update, context)


async def post_init(application: Application):
    global imap_task
    await db.init_db()
    raw_tokens = os.getenv("DDG_TOKENS", "")
    for pair in raw_tokens.split(","):
        if ":" in pair:
            name, tok = pair.split(":", 1)
            if name.strip() and tok.strip():
                await db.upsert_token(name.strip(), tok.strip())

    # Start single persistent background IMAP worker
    imap_task = asyncio.create_task(run_imap_background_loop(application, db))


async def post_shutdown(application: Application):
    if imap_task:
        imap_task.cancel()
    await ddg.http.aclose()
    await db.close()


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(256)  # Handle 500+ concurrent users without queueing
        .rate_limiter(AIORateLimiter(max_retries=3))  # Prevent Telegram 429 flood errors
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addprem", addprem_cmd))
    app.add_handler(CommandHandler("removeprem", removeprem_cmd))
    app.add_handler(CommandHandler("addtoken", addtoken_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    print("High-Concurrency Bot (500+ users ready) is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
