"""Reusable Supabase client and a read-only connectivity check."""

from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from supabase import Client, create_client

from config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL


@lru_cache(maxsize=1)
def get_supabase_client() -> Client:
    """Return the process-wide Supabase client for trusted bot code."""
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def check_supabase_connection(timeout: float = 10) -> None:
    """Check the Supabase REST endpoint without reading or changing data."""
    get_supabase_client()

    request = Request(
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Accept": "application/openapi+json",
        },
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError("Supabase REST API returned an unexpected status.")
    except (HTTPError, URLError, TimeoutError, OSError):
        raise RuntimeError(
            "Could not reach Supabase REST API. Check the URL, key, and network."
        ) from None
