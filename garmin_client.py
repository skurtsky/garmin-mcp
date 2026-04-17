# garmin_client.py
import os
import logging
from garminconnect import Garmin

logger = logging.getLogger(__name__)

TOKEN_DIR = os.path.expanduser("~/.garminconnect")
_client: Garmin | None = None

# Corporate proxy SSL fix — only apply if the cert bundle actually exists
ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE")
if ca_bundle:
    if os.path.exists(ca_bundle):
        os.environ["REQUESTS_CA_BUNDLE"] = ca_bundle
    else:
        # Path doesn't exist (e.g. running in Docker) — remove it entirely
        del os.environ["REQUESTS_CA_BUNDLE"]

def get_client() -> Garmin:
    """
    Return a cached authenticated Garmin client.
    Attempts to resume from saved tokens first, falls back to fresh login.
    """
    global _client
    if _client is not None:
        return _client

    email    = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    token_file = os.path.join(TOKEN_DIR, "garmin_tokens.json")

    client = Garmin(email, password)

    if os.path.exists(token_file):
        try:
            client.client.load(TOKEN_DIR)
            logger.info("Resumed Garmin session from saved tokens")
        except Exception as e:
            logger.warning(f"Token load failed ({e}), attempting fresh login")
            _fresh_login(client, token_file)
    else:
        _fresh_login(client, token_file)

    _client = client
    return _client


def _fresh_login(client: Garmin, token_file: str) -> None:
    """Perform a fresh login and save tokens."""
    os.makedirs(TOKEN_DIR, exist_ok=True)
    client.login()
    client.client.dump(TOKEN_DIR)
    logger.info(f"Fresh login successful, tokens saved to {TOKEN_DIR}")