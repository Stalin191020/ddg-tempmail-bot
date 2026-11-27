import os
import re
import html
import imaplib
import email
import asyncio
import hashlib
from email.header import decode_header
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Set, Optional
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton
from telegram.constants import ParseMode
from telegram.ext import Application

from database import Database
from ai_helper import UniversalAIClient

IST = timezone(timedelta(hours=5, minutes=30))
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "").strip()

BLOCKED_DOMAINS = (
    ".gov.in", ".nic.in", ".gov", ".mil",
    "sbi.co.in", "hdfcbank.com", "icicibank.com", "axisbank.com",
    "kotak.com", "paytm.com", "phonepe.com", "razorpay.com",
    "binance.com", "coindcx.com", "wazirx.com", "paypal.com",
)


def _decode_mime(s: Optional[str]) -> str:
    if not s:
        return ""
    out = []
    for content, charset in decode_header(s):
        if isinstance(content, bytes):
            out.append(content.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(content)
    return "".join(out)


def _is_high_risk_sender(from_header: str) -> bool:
    match = re.search(r"@([\w\.-]+)", from_header.lower())
    if not match:
        return False
    domain = match.group(1)
    return any(domain == b or domain.endswith(b) for b in BLOCKED_DOMAINS)


def _extract_body_and_link(msg: email.message.Message) -> tuple[str, str]:
    plain_text, html_text = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition")):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if part.get_content_type() == "text/plain" and not plain_text:
                plain_text = text
            elif part.get_content_type() == "text/html" and not html_text:
                html_text = text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            raw = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html_text = raw
            else:
                plain_text = raw

    verify_link = ""
    if html_text.strip():
        soup = BeautifulSoup(html_text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            label = a.get_text(" ", strip=True).lower()
            if href.startswith("http") and any(
                k in label or k in href.lower()
                for k in ("verify", "confirm", "activate", "token=", "code=")
            ):
                verify_link = href
                break
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        clean = plain_text if plain_text.strip() else soup.get_text(separator="\n")
    elif plain_text.strip():
        clean = plain_text
        m_link = re.search(
            r"https?://[^\s<>\"']+(?:verify|confirm|activate|token)[^\s<>\"']*",
            plain_text,
            re.I,
        )
        if m_link:
            verify_link = m_link.group(0)
    else:
        clean = "(Empty body)"

    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    return "\n".join(lines)[:900], verify_link


def _extract_otp(subject: str, body: str) -> str:
    clean_body = re.sub(r"https?://\S+", " ", body)
    combined = f"{subject}\n{clean_body}"

    m = re.search(
        r"(?:otp|code|pin|verification|verify|passcode)[^\d]{0,25}(\b\d{4,8}\b)",
        combined,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    for num in re.findall(r"\b\d{4,8}\b", combined):
        if num not in {"2024", "2025", "2026", "2027"}:
            return num
    return ""


class PersistentImapPoller:
    def __init__(self):
        self.host = os.getenv("IMAP_HOST", "imap.zoho.in")
        self.user = os.getenv("IMAP_USER", "")
        self.password = os.getenv("IMAP_PASS", "")
        self.conn: Optional[imaplib.IMAP4_SSL] = None
        self.processed_uids: Set[str] = set()

    def _ensure_connected(self):
        if self.conn is not None:
            try:
                self.conn.noop()
                return
            except Exception:
                try:
                    self.conn.logout()
                except Exception:
                    pass
                self.conn = None

        self.conn = imaplib.IMAP4_SSL(self.host)
        self.conn.login(self.user, self.password)

    def poll_once_sync(self, active_aliases: Set[str]) -> List[Dict[str, str]]:
        if not self.user or not self.password or not active_aliases:
            return []

        self._ensure_connected()
        since_date = (datetime.utcnow() - timedelta(days=1)).strftime("%d-%b-%Y")
        new_items: List[Dict[str, str]] = []

        for folder in ["INBOX", "Junk", "Spam"]:
            try:
                status, _ = self.conn.select(folder, readonly=True)
                if status != "OK":
                    continue
            except Exception:
                continue

            status, data = self.conn.search(None, f'(SINCE "{since_date}")')
            if status != "OK" or not data or not data[0]:
                continue

            for num in reversed(data[0].split()[-60:]):
                res, msg_data = self.conn.fetch(num, "(BODY.PEEK[])")
                if res != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw_bytes = msg_data[0][1]
                uid_hash = hashlib.sha256(raw_bytes[:2048]).hexdigest()[:24]
                if uid_hash in self.processed_uids:
                    continue
                self.processed_uids.add(uid_hash)

                msg = email.message_from_bytes(raw_bytes)
                raw_headers = "\n".join(f"{k}: {v}" for k, v in msg.items()).lower()
                matched_alias = next(
                    (alias for alias in active_aliases if alias in raw_headers), None
                )
                if not matched_alias:
                    continue

                subject = _decode_mime(msg.get("Subject", "(No Subject)"))
                sender = _decode_mime(msg.get("From", "Unknown"))
                body, verify_link = _extract_body_and_link(msg)
                otp = _extract_otp(subject, body)

                new_items.append(
                    {
                        "uid": uid_hash,
                        "alias": matched_alias,
                        "sender": sender,
                        "subject": subject,
                        "otp": otp,
                        "body": body,
                        "verify_link": verify_link,
                    }
                )

        if len(self.processed_uids) > 5000:
            self.processed_uids = set(list(self.processed_uids)[-2500:])

        return new_items


async def _send_audit_log(app: Application, text: str):
    if not LOG_CHANNEL_ID:
        return
    try:
        await app.bot.send_message(
            chat_id=int(LOG_CHANNEL_ID),
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[Audit Log Error] {e}")


async def run_imap_background_loop(app: Application, db: Database):
    poller = PersistentImapPoller()
    ai_client = UniversalAIClient()
    cleanup_counter = 0

    try:
        while True:
            try:
                alias_map = await db.get_all_active_alias_map()
                if alias_map:
                    items = await asyncio.to_thread(
                        poller.poll_once_sync, set(alias_map.keys())
                    )
                    for item in items:
                        target_user_id = alias_map.get(item["alias"])
                        if not target_user_id:
                            continue

                        now_ist = datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")

                        if _is_high_risk_sender(item["sender"]):
                            await db.log_audit_event(
                                target_user_id, "", "BLOCKED_DOMAIN", item["alias"], item["sender"]
                            )
                            asyncio.create_task(
                                app.bot.send_message(
                                    chat_id=target_user_id,
                                    text=(
                                        "⛔ <b>Blocked High-Risk Email</b>\n\n"
                                        "Verification codes from banking, financial, crypto, or government portals "
                                        "are strictly prohibited under our Terms of Use."
                                    ),
                                    parse_mode=ParseMode.HTML,
                                )
                            )
                            asyncio.create_task(
                                _send_audit_log(
                                    app,
                                    f"🚨 #BLOCKED_DOMAIN | #ID_{target_user_id}\n"
                                    f"🆔 <b>User ID:</b> <code>{target_user_id}</code>\n"
                                    f"📧 <b>Alias:</b> <code>{html.escape(item['alias'])}</code>\n"
                                    f"📤 <b>From:</b> {html.escape(item['sender'][:60])}\n"
                                    f"🕒 <b>Time:</b> {now_ist}",
                                )
                            )
                            continue

                        ai_info = await ai_client.analyze_email(item["subject"], item["body"])
                        if not item["otp"] and ai_info.get("otp"):
                            item["otp"] = ai_info["otp"]

                        inserted = await db.save_inbox_message(
                            msg_uid=item["uid"],
                            alias=item["alias"],
                            sender=item["sender"],
                            subject=item["subject"],
                            otp=item["otp"],
                            body=item["body"],
                        )

                        if inserted:
                            await db.log_audit_event(
                                target_user_id, "", "OTP_RECEIVED", item["alias"], item["sender"]
                            )

                            otp_line = (
                                f"🔑 <b>OTP / Code:</b> <code>{html.escape(item['otp'])}</code>\n"
                                if item["otp"]
                                else ""
                            )

                            ai_note = ""
                            if ai_info.get("warning"):
                                ai_note = f"\n⚠️ <b>Security Alert:</b> {html.escape(ai_info['warning'])}"
                            elif ai_info.get("summary"):
                                ai_note = f"\n🤖 <b>Summary:</b> <i>{html.escape(ai_info['summary'])}</i>"

                            text = (
                                "📩 <b>New Email / OTP Received!</b>\n\n"
                                f"📥 <b>To:</b> <code>{html.escape(item['alias'])}</code>\n"
                                f"📤 <b>From:</b> {html.escape(item['sender'][:50])}\n"
                                f"📝 <b>Subject:</b> {html.escape(item['subject'][:80])}\n"
                                f"{otp_line}"
                                f"💬 <b>Message:</b>\n<pre>{html.escape(item['body'][:400])}</pre>"
                                f"{ai_note}"
                            )

                            btn_rows = []
                            if item["otp"]:
                                btn_rows.append(
                                    [
                                        InlineKeyboardButton(
                                            f"📋 Copy OTP: {item['otp']}",
                                            copy_text=CopyTextButton(text=item["otp"]),
                                        )
                                    ]
                                )
                            if item["verify_link"]:
                                btn_rows.append(
                                    [
                                        InlineKeyboardButton(
                                            "🔗 Open Verification Link",
                                            url=item["verify_link"],
                                        )
                                    ]
                                )
                            btn_rows.append(
                                [InlineKeyboardButton("🔄 Check Inbox", callback_data="refresh_all")]
                            )

                            asyncio.create_task(
                                app.bot.send_message(
                                    chat_id=target_user_id,
                                    text=text,
                                    parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup(btn_rows),
                                    disable_web_page_preview=True,
                                )
                            )

                            asyncio.create_task(
                                _send_audit_log(
                                    app,
                                    f"📩 #OTP_RECEIVED | #ID_{target_user_id}\n"
                                    f"🆔 <b>User ID:</b> <code>{target_user_id}</code>\n"
                                    f"📧 <b>Alias:</b> <code>{html.escape(item['alias'])}</code>\n"
                                    f"📤 <b>From:</b> {html.escape(item['sender'][:60])}\n"
                                    f"🕒 <b>Time:</b> {now_ist}",
                                )
                            )

                cleanup_counter += 1
                if cleanup_counter >= 450:
                    await db.purge_old_messages(86400)
                    cleanup_counter = 0

            except Exception as exc:
                print(f"[IMAP Worker Warning] {exc}")
                poller.conn = None

            await asyncio.sleep(6)
    finally:
        await ai_client.close()
