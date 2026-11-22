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
                "All DDG tokens are rate-limited or invalid. Try again later or add new tokens."
            )

        random.shuffle(healthy)
        last_error = "Unknown error"

        for tok in healthy:
            token_id = tok["token_id"]
            bearer = tok["bearer_token"]
            try:
                resp = await self.http.post(
                    f"{self.BASE_URL}/addresses",
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
                    # Cooldown exhausted token for 6 hours
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

    async def delete_alias(self, alias_id: str, token_id: str):
        tokens = await self.db.get_all_tokens()
        tok = next((t for t in tokens if t["token_id"] == token_id), None)
        if not tok:
            return
        try:
            await self.http.delete(
                f"{self.BASE_URL}/addresses/{alias_id}",
                headers={"Authorization": f"Bearer {tok['bearer_token']}"},
            )
        except Exception:
            pass
