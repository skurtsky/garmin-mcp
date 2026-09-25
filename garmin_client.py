# garmin_client.py
"""Shared, self-healing Garmin Connect client.

get_client() returns one GarminClientProxy for the life of the process. The
proxy forwards every attribute to the *current* underlying ``Garmin``
instance, and wraps method calls so that a Garmin auth failure (an HTTP 401 /
expired session) triggers recovery and exactly one retry:

  A. reload tokens from disk — the garmin-sync job (or a manual upload) may
     have saved fresh ones — and verify them with a cheap call;
  B. only if that fails, a fresh login with GARMIN_EMAIL / GARMIN_PASSWORD,
     saving the new tokens atomically.

Fresh logins are rate-limited by a cooldown (GARMIN_RELOGIN_COOLDOWN_SECONDS,
default 900s) after a failure, so a broken login (MFA, 429, bad password)
can't hammer Garmin's SSO once per tool call. Step A still runs during the
cooldown, so manually uploaded tokens take effect on the next 401.

Because the proxy always resolves to the current instance, a tool that took
``client = get_client()`` before a recovery keeps working afterwards.
Direct ``client.client.*`` access resolves to the current inner session but
isn't wrapped.
"""
import os
import re
import glob
import time
import uuid
import logging
import functools
import threading

from garminconnect import Garmin, GarminConnectAuthenticationError

logger = logging.getLogger(__name__)

TOKEN_DIR = os.path.expanduser("~/.garminconnect")
TOKEN_FILENAME = "garmin_tokens.json"

_DEFAULT_RELOGIN_COOLDOWN_SECONDS = 900

# The live Garmin instance behind the proxy, and a counter bumped every time
# it's replaced — callers compare generations to tell whether another thread
# already recovered while they waited on the lock.
_client: Garmin | None = None
_generation = 0
_client_init_lock = threading.Lock()

# (monotonic time, reason) of the last failed fresh login, for the cooldown.
_last_login_failure: tuple[float, str] | None = None
# (generation, monotonic time, error) of the last failed recovery, so threads
# that queued behind it re-raise instead of each repeating it.
_last_recovery_failure: tuple[int, float, Exception] | None = None

# Corporate proxy SSL fix — only apply if the cert bundle actually exists
ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE")
if ca_bundle:
    if os.path.exists(ca_bundle):
        os.environ["REQUESTS_CA_BUNDLE"] = ca_bundle
    else:
        # Path doesn't exist (e.g. running in Docker) — remove it entirely
        del os.environ["REQUESTS_CA_BUNDLE"]


class GarminAuthExpired(GarminConnectAuthenticationError):
    """Garmin rejected our session and automatic recovery couldn't fix it."""


# garminconnect 0.3.2 reports a rejected request as
# GarminConnectConnectionError("HTTP error: API Error 401 - …") — the status
# only survives in the message, since the error carries no .response.
_API_401_RE = re.compile(r"\bAPI Error 401\b")


def is_auth_error(exc: BaseException) -> bool:
    """True for a Garmin 401 / expired-session error (following the
    __cause__/__context__ chain); False for 429s, 5xx, and everything else."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, GarminAuthExpired):
            return False  # already recovered and failed — don't loop
        if isinstance(exc, GarminConnectAuthenticationError):
            return True
        if getattr(getattr(exc, "response", None), "status_code", None) == 401:
            return True
        if _API_401_RE.search(str(exc)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _describe(exc: BaseException) -> str:
    """Short, log-safe summary of an error (Garmin's messages carry no
    credentials or tokens; trimmed anyway to keep logs readable)."""
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 300 else text[:297] + "..."


def _token_file() -> str:
    return os.path.join(TOKEN_DIR, TOKEN_FILENAME)


def _relogin_cooldown() -> float:
    try:
        return float(os.environ.get("GARMIN_RELOGIN_COOLDOWN_SECONDS",
                                    _DEFAULT_RELOGIN_COOLDOWN_SECONDS))
    except ValueError:
        return _DEFAULT_RELOGIN_COOLDOWN_SECONDS


# ── TOKEN PERSISTENCE ────────────────────────────────────────────────────────

def _save_tokens(session) -> None:
    """Write the session's tokens atomically (temp file + os.replace): the
    token file lives on an SMB share that the garmin-sync job also writes,
    and SMB has no file locks, so a reader must never see a half-written file."""
    os.makedirs(TOKEN_DIR, exist_ok=True)
    path = _token_file()
    tmp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(session.dumps())
    try:
        os.replace(tmp_path, path)
    except PermissionError:
        if os.path.exists(path):
            os.remove(path)
        os.replace(tmp_path, path)


def _persist_refreshes(client: Garmin) -> None:
    """Make the library's own token refreshes land on disk, atomically.

    garminconnect refreshes an expiring access token by itself and then
    calls ``session.dump(session._tokenstore_path)`` — but only when a token
    path is set, and with a plain (non-atomic) write. Point it at our token
    file and route that dump through _save_tokens.
    """
    session = client.client
    session._tokenstore_path = TOKEN_DIR
    session.dump = lambda _path=None: _save_tokens(session)


# ── AUTHENTICATION ───────────────────────────────────────────────────────────

def _new_garmin() -> Garmin:
    return Garmin(os.environ.get("GARMIN_EMAIL"), os.environ.get("GARMIN_PASSWORD"))


def _load_from_disk() -> Garmin:
    """Step A: a new client from the saved tokens, verified with a cheap call."""
    client = _new_garmin()
    client.client.load(TOKEN_DIR)
    _persist_refreshes(client)
    prof = client.client.connectapi("/userprofile-service/socialProfile")
    client.display_name = prof.get("displayName", client.username)
    client.get_user_profile()  # verify tokens are still valid
    return client


def _fresh_login() -> Garmin:
    """Step B: log in with credentials and save the new tokens."""
    client = _new_garmin()
    client.login()
    _persist_refreshes(client)
    _save_tokens(client.client)
    return client


def _authenticate() -> Garmin:
    """Build a working client: tokens from disk first, fresh login second.

    Only overwrites the token file after a fresh login succeeds — the saved
    tokens may be ones the sync job just refreshed, so they're never
    deleted up front.
    """
    global _last_login_failure

    if os.path.exists(_token_file()):
        try:
            client = _load_from_disk()
        except Exception as e:
            if not is_auth_error(e) and not _is_token_load_error(e):
                # Garmin itself is failing (429, 5xx, network): a fresh login
                # wouldn't help and would only spend the login budget.
                logger.warning("Garmin token check failed with a non-auth error: %s", _describe(e))
                raise
            logger.warning("Reloading Garmin tokens from disk failed: %s", _describe(e))
        else:
            logger.info("Garmin tokens reloaded from disk OK")
            return client
    else:
        logger.info("No saved Garmin tokens at %s", _token_file())

    if _last_login_failure is not None:
        failed_at, reason = _last_login_failure
        remaining = _relogin_cooldown() - (time.monotonic() - failed_at)
        if remaining > 0:
            logger.warning("Skipping Garmin fresh login: cooling down for %ds after: %s",
                           remaining, reason)
            raise GarminAuthExpired(_expired_message(
                f"{reason}; next automatic attempt in {int(remaining) // 60 + 1} min"))

    try:
        client = _fresh_login()
    except Exception as e:
        reason = _describe(e)
        _last_login_failure = (time.monotonic(), reason)
        logger.warning("Garmin fresh login failed: %s", reason)
        raise GarminAuthExpired(_expired_message(reason)) from e

    _last_login_failure = None
    logger.info("Garmin fresh login OK; tokens saved to %s", _token_file())
    return client


def _is_token_load_error(exc: BaseException) -> bool:
    """garminconnect wraps an unreadable/malformed token file this way."""
    text = str(exc)
    return "Token path not loading cleanly" in text or "Token extraction" in text


def _expired_message(reason: str) -> str:
    return (
        f"Garmin session expired and automatic re-login failed ({reason}). "
        "Refresh tokens manually — see test-deployment.md 'Refreshing Garmin Tokens'."
    )


def _current() -> tuple[int, Garmin]:
    """(generation, instance) of the live client, creating it on first use."""
    global _client, _generation
    generation, client = _generation, _client
    if client is not None:
        return generation, client

    # Dashboard sections initialize in parallel. Only one thread may resume or
    # create the shared Garmin session; otherwise every section logs in at once.
    with _client_init_lock:
        if _client is None:
            _client = _authenticate()
            _generation += 1
        return _generation, _client


def _recover(seen_generation: int, call_started_at: float) -> None:
    """Replace the client after an auth failure on generation `seen_generation`.

    Runs once per failure burst: calls whose 401 came in while another thread
    recovered see the generation has moved on and just retry; calls that
    started before a *failed* recovery finished re-raise its error instead of
    repeating it. Calls made after that failure try again (step A at least).
    """
    global _client, _generation, _last_recovery_failure
    with _client_init_lock:
        if _generation != seen_generation:
            return
        if _last_recovery_failure is not None:
            failed_gen, failed_at, error = _last_recovery_failure
            if failed_gen == seen_generation and failed_at >= call_started_at:
                raise error
        logger.warning("Recovering Garmin session: reloading tokens from disk first")
        try:
            new_client = _authenticate()
        except Exception as e:
            _last_recovery_failure = (seen_generation, time.monotonic(), e)
            raise
        _client = new_client
        _generation += 1
        _last_recovery_failure = None


class GarminClientProxy:
    """Stands in for the ``Garmin`` instance; see the module docstring."""

    __slots__ = ()

    def __getattr__(self, name):
        generation, client = _current()
        attr = getattr(client, name)
        if name.startswith("_") or not callable(attr):
            return attr

        @functools.wraps(attr)
        def call(*args, **kwargs):
            started_at = time.monotonic()
            try:
                return attr(*args, **kwargs)
            except Exception as e:
                if not is_auth_error(e):
                    raise
                logger.warning("Garmin 401 detected in %s(): %s", name, _describe(e))
                _recover(generation, started_at)
            # One retry, against whatever instance is current now. A 401 means
            # Garmin rejected the request, so retrying a write is safe.
            try:
                return getattr(_current()[1], name)(*args, **kwargs)
            except Exception as e:
                if is_auth_error(e):
                    raise GarminAuthExpired(
                        f"Garmin rejected {name}() even after re-authenticating "
                        f"({_describe(e)}). Refresh tokens manually — see "
                        "test-deployment.md 'Refreshing Garmin Tokens'."
                    ) from e
                raise

        return call

    def __setattr__(self, name, value):
        setattr(_current()[1], name, value)

    def __repr__(self):
        return f"<GarminClientProxy for {_client!r}>"


_proxy = GarminClientProxy()


def get_client() -> GarminClientProxy:
    """The shared Garmin client (always the same proxy object)."""
    _current()
    return _proxy


def current_garmin() -> Garmin:
    """The real ``Garmin`` instance currently behind the proxy."""
    return _current()[1]


def _clear_tokens() -> None:
    """Remove saved token files from disk."""
    for f in glob.glob(os.path.join(TOKEN_DIR, "*.json")):
        os.remove(f)


def reset_client(clear_tokens: bool = False) -> None:
    """Drop the in-memory client so the next call re-authenticates (tokens
    from disk first). Saved tokens are only deleted with clear_tokens=True —
    they may be fresh ones another process just wrote."""
    global _client, _generation
    with _client_init_lock:
        _client = None
        _generation += 1
        if clear_tokens:
            _clear_tokens()
    logger.info("Cleared cached Garmin client%s", " and saved tokens" if clear_tokens else "")
