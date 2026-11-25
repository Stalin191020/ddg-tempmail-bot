import os
import time
import random
from typing import Dict
import httpx
from database import Database


class DuckAliasClient:
    BASE_URL = "https://quack.duckduckgo.com/api/email"

    def __init__(self, db: Database):
        self.db = db
        self.http = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Origin": "https://duckduckgo.com",
                "Referer": "https://duckduckgo.com/",
            },
        )

    async def generate_alias(self) -> Dict[str, str]:
        tokens = await self.db.get_all_tokens()
        now = time.time()
        healthy = [
            t for t in tokens if t["is_valid"] == 1 and t["exhausted_until"] <= now
        ]
        if not healthy:
            raise RuntimeError(
                "All DDG tokens are rate-limited or invalid. Try Gmail Alias or try again later."
            )

        random.shuffle(healthy)
        last_error = "Unknown error"

        for tok in healthy:
            token_id = tok["token_id"]
            bearer = tok["bearer_token"].strip()
            try:
                resp = await self.http.post(
                    "https://quack.duckduckgo.com/api/email/addresses",
                    headers={"Authorization": f"Bearer {bearer}"},
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    prefix = data.get("address", "").strip()
                    if prefix:
                        return {
                            "email": f"{prefix}@duck.com",
                            "alias_id": prefix,
                            "token_id": token_id,
                        }
                elif resp.status_code == 429:
                    await self.db.update_token_status(
                        token_id, exhausted_until=time.time() + 21600
                    )
                    last_error = f"Token {token_id} hit daily limit (429)."
                elif resp.status_code in (401, 403):
                    await self.db.update_token_status(token_id, is_valid=0)
                    last_error = f"Token {token_id} expired or unauthorized."
                else:
                    last_error = f"DDG API returned HTTP {resp.status_code}"
            except Exception as exc:
                last_error = str(exc)

        raise RuntimeError(last_error)

    async def generate_gmail_alias(self) -> Dict[str, str]:
        """Generates a unique dot-trick @gmail.com alias from GMAIL_BASES."""
        raw_bases = os.getenv("GMAIL_BASES", "")
        bases = [
            b.strip().lower()
            for b in raw_bases.split(",")
            if "@" in b.strip()
        ]
        if not bases:
            raise RuntimeError(
                "Gmail provider is not configured yet (GMAIL_BASES missing)."
            )

        for _ in range(40):
            base = random.choice(bases)
            local, domain = base.split("@", 1)
            clean_local = local.replace(".", "")
            if len(clean_local) < 4:
                continue

            chars = [clean_local[0]]
            for ch in clean_local[1:]:
                if random.random() < 0.45:
                    chars.append(".")
                chars.append(ch)
            dotted_local = "".join(chars)
            candidate = f"{dotted_local}@{domain}"

            if candidate != base and not await self.db.email_exists(candidate):
                return {
                    "email": candidate,
                    "alias_id": dotted_local,
                    "token_id": "GMAIL_DOT",
                }

        raise RuntimeError("Could not generate a unique Gmail alias. Try again.")

    async def delete_alias(self, alias_id: str, token_id: str):
        if token_id == "GMAIL_DOT":
            return
        tokens = await self.db.get_all_tokens()
        tok = next((t for t in tokens if t["token_id"] == token_id), None)
        if not tok:
            return
        try:
            await self.http.delete(
                f"https://quack.duckduckgo.com/api/email/addresses/{alias_id}",
                headers={"Authorization": f"Bearer {tok['bearer_token'].strip()}"},
            )
        except Exception:
            pass
