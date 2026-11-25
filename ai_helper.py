import os
import json
import httpx
from typing import Dict

AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv(
    "AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
).rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "gemini-2.5-flash").strip()


class UniversalAIClient:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=20.0)

    @property
    def is_enabled(self) -> bool:
        return bool(AI_API_KEY)

    async def _chat(self, system_prompt: str, user_prompt: str, max_tokens: int = 500) -> str:
        if not self.is_enabled:
            print("[AI Helper] AI_API_KEY is empty!")
            return ""

        url = f"{AI_BASE_URL}/chat/completions"
        headers = {
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }

        try:
            resp = await self.http.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"].get("content") or ""
                return content.strip()
            else:
                print(f"[AI API Error {resp.status_code}] {resp.text[:300]}")
        except Exception as exc:
            print(f"[AI Helper Exception] {exc}")
        return ""

    async def analyze_email(self, subject: str, body: str) -> Dict[str, str]:
        result = {"otp": "", "summary": "", "warning": ""}
        if not self.is_enabled:
            return result

        sys_prompt = (
            "You are an email security & OTP parser. Analyze the email and reply ONLY in valid JSON "
            'with exact keys: {"otp": "", "summary": "", "warning": ""}.\n'
            "- otp: exact verification code/OTP if present (do NOT extract numbers from URLs), else empty string.\n"
            "- summary: max 15-word plain English summary of what this email is about.\n"
            "- warning: short warning ONLY if it looks like a phishing/scam/fake KYC email, else empty string."
        )
        user_prompt = f"Subject: {subject[:120]}\nBody:\n{body[:1200]}"

        raw = await self._chat(sys_prompt, user_prompt, max_tokens=400)
        if not raw:
            return result

        clean_json = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            parsed = json.loads(clean_json)
            result["otp"] = str(parsed.get("otp", "")).strip()
            result["summary"] = str(parsed.get("summary", "")).strip()
            result["warning"] = str(parsed.get("warning", "")).strip()
        except Exception as e:
            print(f"[AI JSON Parse Warning] {e} | Raw: {raw[:200]}")

        return result

    async def answer_support_query(self, user_question: str) -> str:
        if not self.is_enabled:
            return ""

        sys_prompt = (
            "You are the friendly support assistant for a Telegram Temp-Mail & OTP Bot. "
            "Keep answers under 3 short lines in simple Hinglish/English. "
            "Tell users to use '➕ New Email' to create an address and '📥 Check Inbox' to read OTPs. "
            "Banking, crypto, and government accounts are strictly prohibited."
        )
        return await self._chat(sys_prompt, user_question[:300], max_tokens=400)

    async def close(self):
        await self.http.aclose()
