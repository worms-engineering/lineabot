"""Automatic OddsPapi key rotation.

OddsPapi free accounts come with a monthly request quota; when it runs out
the monitor signs up a fresh throwaway account (temp mail.tm address), reads
the auto-issued API key and swaps it in at runtime - no redeploy needed.

The flow mirrors the "OddsPapi Key Generator" web app (odds-gen-fast.
base44.app), which performs the same steps client-side:
  1. pick a mail.tm domain and invent an address + password;
  2. POST /auth/v1/signup on userdata.oddspapi.io (Supabase-style auth,
     anon JWT as both apikey and Bearer) -> session access_token;
  3. poll GET /rest/v1/accounts?select=api_key with that token until the
     auto-generated key appears (usually within a couple of seconds).
"""
from __future__ import annotations

import asyncio
import logging
import random
import string

import httpx

logger = logging.getLogger(__name__)

# Anonymous JWT embedded in the key-generator web app (public by design).
_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJvbGUi"
    "OiJhbm9uIiwiaWF0IjoxNzYwMzYzMDYyLCJleHAiOjE3OTE4OTkwNjJ9.qz5skjXaou3r"
    "HtXEDKrJjDRV6on46Zzl3zGTHNoX1qs"
)
_USERDATA_URL = "https://userdata.oddspapi.io"
_API_URL = "https://api.oddspapi.io/v4"
_DEFAULT_MAIL_DOMAIN = "web-library.net"


def _rnd(n: int, chars: str = string.ascii_lowercase + string.digits) -> str:
    return "".join(random.choice(chars) for _ in range(n))


def _random_password() -> str:
    groups = (string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#$%")
    pw = [random.choice(g) for g in groups]
    pw += [_rnd(8, string.ascii_letters + string.digits) for _ in range(8)]
    random.shuffle(pw)
    return "".join(pw)


class KeyRotationError(RuntimeError):
    pass


async def _mail_domain(client: httpx.AsyncClient) -> str:
    try:
        r = await client.get("https://api.mail.tm/domains", timeout=5)
        if r.status_code == 200:
            data = r.json()
            domains = data.get("hydra:member") or data
            if domains:
                return domains[0]["domain"]
    except Exception:
        pass
    return _DEFAULT_MAIL_DOMAIN


async def generate_oddspapi_key() -> str:
    """Create a fresh OddsPapi account and return its validated API key."""
    async with httpx.AsyncClient(timeout=20) as client:
        email = f"op{_rnd(10)}@{await _mail_domain(client)}"
        password = _random_password()
        r = await client.post(
            f"{_USERDATA_URL}/auth/v1/signup",
            headers={
                "apikey": _ANON_KEY,
                "Authorization": f"Bearer {_ANON_KEY}",
                "Content-Type": "application/json",
            },
            json={"email": email, "password": password},
        )
        if r.status_code not in (200, 201):
            raise KeyRotationError(
                f"signup failed ({r.status_code}): {r.text[:200]}")
        token = r.json().get("access_token")
        if not token:
            raise KeyRotationError("signup returned no access token")

        # The key is generated asynchronously on their side; poll for it.
        await asyncio.sleep(2.5)
        for attempt in range(5):
            r2 = await client.get(
                f"{_USERDATA_URL}/rest/v1/accounts?select=api_key",
                headers={
                    "apikey": _ANON_KEY,
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
            if r2.status_code == 200:
                rows = r2.json()
                if rows and rows[0].get("api_key"):
                    key = rows[0]["api_key"]
                    v = await client.get(
                        f"{_API_URL}/tournaments",
                        params={"apiKey": key, "sportId": 12},
                    )
                    if v.status_code == 401:
                        raise KeyRotationError(
                            f"generated key rejected by API (401): {key}")
                    logger.info("generated new OddsPapi key for %s", email)
                    return key
            await asyncio.sleep(2)
        raise KeyRotationError("api key never appeared on the account")
