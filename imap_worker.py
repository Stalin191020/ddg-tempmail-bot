import os
import re
import html
import imaplib
import email
import asyncio
import hashlib
from email.header import decode_header
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional
from bs4 import BeautifulSoup
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton
from telegram.constants import ParseMode
from telegram.ext import Application
from database import Database


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


def _extract_body(msg: email.message.Message) -> str:
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

    if plain_text.strip():
        clean = plain_text
    elif html_text.strip():
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        clean = soup.get_text(separator="\n")
    else:
        clean = "(Empty body)"

    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    return "\n".join(lines)[:900]


def _extract_otp(subject: str, body: str) -> str:
    combined = f"{subject}\n{body}"
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
        self.host = os.getenv("IMAP_HOST", "outlook.office365.com")
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

        for folder in ["INBOX", "Junk"]:
            status, _ = self.conn.select(folder, readonly=True)
            if status != "OK":
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
                body = _extract_body(msg)
                otp = _extract_otp(subject, body)

                new_items.append(
                    {
                        "uid": uid_hash,
                        "alias": matched_alias,
                        "sender": sender,
                        "subject": subject,
                        "otp": otp,
                        "body": body,
                    }
                )

        if len(self.processed_uids) > 5000:
            self.processed_uids = set(list(self.processed_uids)[-2500:])

        return new_items


async def run_imap_background_loop(app: Application, db: Database):
    poller = PersistentImapPoller()
    cleanup_counter = 0

    while True:
        try:
            alias_map = await db.get_all_active_alias_map()
            if alias_map:
                items = await asyncio.to_thread(
                    poller.poll_once_sync, set(alias_map.keys())
                )
                for item in items:
                    inserted = await db.save_inbox_message(
                        msg_uid=item["uid"],
                        alias=item["alias"],
                        sender=item["sender"],
                        subject=item["subject"],
                        otp=item["otp"],
                        body=item["body"],
                    )
                    target_user_id = alias_map.get(item["alias"])
                    if inserted and target_user_id:
                        otp_code = item["otp"]
                        otp_line = (
                            f"🔑 <b>OTP / Code:</b> <code>{html.escape(otp_code)}</code>\n"
                            if otp_code
                            else ""
                        )
                        text = (
                            "🔔 <b>New Email / OTP Received!</b>\n\n"
                            f"📧 <b>To:</b> <code>{html.escape(item['alias'])}</code>\n"
                            f"👤 <b>From:</b> {html.escape(item['sender'][:50])}\n"
                            f"📝 <b>Subject:</b> {html.escape(item['subject'][:80])}\n"
                            f"{otp_line}"
                            f"💬 <b>Message:</b>\n<blockquote>{html.escape(item['body'][:450])}</blockquote>"
                        )
                        reply_markup = (
                            InlineKeyboardMarkup(
                                [
                                    [
                                        InlineKeyboardButton(
                                            f"📋 Copy OTP ({otp_code})",
                                            copy_text=CopyTextButton(text=otp_code),
                                        )
                                    ]
                                ]
                            )
                            if otp_code
                            else None
                        )
                        asyncio.create_task(
                            app.bot.send_message(
                                chat_id=target_user_id,
                                text=text,
                                parse_mode=ParseMode.HTML,
                                reply_markup=reply_markup,
                            )
                        )

            cleanup_counter += 1
            if cleanup_counter >= 450:
                await db.purge_old_messages(86400)
                cleanup_counter = 0

        except Exception as exc:
            print(f"[IMAP Worker Warning] {exc}")
            poller.conn = None

        await asyncio.sleep(8)
