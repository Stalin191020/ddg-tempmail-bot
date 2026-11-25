import os
import json
import httpx
from typing import Dict

AI_API_KEY = os.getenv("AI_API_KEY", "").strip()


class UniversalAIClient:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=20.0)
        self.cached_model = ""

    @property
    def is_enabled(self) -> bool:
        return bool(AI_API_KEY)

    async def _resolve_gemini_model(self) -> str:
        if self.cached_model:
            return self.cached_model
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={AI_API_KEY}"
        try:
            resp = await self.http.get(url)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                for m in models:
                    name = m.get("name", "").replace("models/", "")
                    methods = m.get("supportedGenerationMethods", [])
                    if "flash" in name and "generateContent" in methods:
                        self.cached_model = name
                        print(f"[AI Auto-Selected Model] {name}")
                        return name
        except Exception as e:
            print(f"[AI Model Discovery Error] {e}")
        return "gemini-1.5-flash"

    async def _chat(self, system_prompt: str, user_prompt: str, max_tokens: int = 500) -> str:
        if not self.is_enabled:
            return ""

        model = await self._resolve_gemini_model()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={AI_API_KEY}"
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
        }

        try:
            resp = await self.http.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                parts = data["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts).strip()
            else:
                self.cached_model = ""
                print(f"[AI Error {resp.status_code}] {resp.text[:250]}")
        except Exception as exc:
            print(f"[AI Exception] {exc}")
        return ""

    async def analyze_email(self, subject: str, body: str) -> Dict[str, str]:
        result = {"otp": "", "summary": "", "warning": ""}
        if not self.is_enabled:
            return result

        sys_prompt = (
            "You are an email security & OTP parser. Reply ONLY in valid JSON "
            'with exact keys: {"otp": "", "summary": "", "warning": ""}. '
            "otp: verification code if present (never extract numbers from URLs), else empty. "
            "summary: max 15-word plain English summary. "
            "warning: short phishing warning only if suspicious, else empty."
        )
        raw = await self._chat(sys_prompt, f"Subject: {subject[:120]}\nBody:\n{body[:1200]}", 400)
        if not raw:
            return result

        clean = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            parsed = json.loads(clean)
            result["otp"] = str(parsed.get("otp", "")).strip()
            result["summary"] = str(parsed.get("summary", "")).strip()
            result["warning"] = str(parsed.get("warning", "")).strip()
        except Exception:
            pass
        return result

    async def answer_support_query(self, user_question: str) -> str:
        if not self.is_enabled:
            return ""
        sys_prompt = (
            "You are the friendly support assistant for a Telegram Temp-Mail & OTP Bot. "
            "Reply in under 3 short lines in simple Hinglish/English. "
            "Guide users to use '➕ New Email' and '📥 Check Inbox'. "
            "Banking, crypto, and government accounts are strictly prohibited."
        )
        return await self._chat(sys_prompt, user_question[:300], 400)

    async def close(self):
        await self.http.aclose()
