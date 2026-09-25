# tests/test_client_recovery.py
"""Automatic recovery from Garmin 401s in garmin_client.py.

Fully offline: garmin_client.Garmin is replaced by a fake whose behaviour is
driven by a shared FakeGarminWorld (which tokens Garmin accepts, what's on
disk, whether login works), so no credentials or network are needed.
"""
import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

import garmin_client
from garmin_client import GarminAuthExpired, get_client, is_auth_error


def _api_401():
    # Exactly what garminconnect 0.3.2's Garmin.connectapi raises for a 401.
    return GarminConnectConnectionError("HTTP error: API Error 401 - ")


class FakeGarminWorld:
    """Garmin's side of things, shared by every FakeGarmin instance."""

    def __init__(self, token_dir):
        self.token_dir = token_dir
        self.valid_tokens = set()
        self.login_token = "fresh"
        self.login_error = None
        self.logins = 0
        self.loads = 0
        self.lock = threading.Lock()
        self.barrier = None         # set to make first-round 401s wait for each other
        self.always_401 = set()     # method names that 401 regardless of token
        self.errors = {}            # method name -> exception to raise once

    @property
    def token_file(self):
        return os.path.join(self.token_dir, "garmin_tokens.json")

    def write_disk(self, token):
        os.makedirs(self.token_dir, exist_ok=True)
        with open(self.token_file, "w") as f:
            json.dump({"di_token": token}, f)

    def read_disk(self):
        with open(self.token_file) as f:
            return json.load(f)["di_token"]

    def check(self, token):
        if token not in self.valid_tokens:
            raise _api_401()


class FakeSession:
    """Stands in for garminconnect.client.Client (client.client)."""

    def __init__(self, world):
        self.world = world
        self.token = None
        self._tokenstore_path = None

    def load(self, path):
        with self.world.lock:
            self.world.loads += 1
        try:
            with open(os.path.join(path, "garmin_tokens.json")) as f:
                self.token = json.load(f)["di_token"]
        except Exception as e:
            raise GarminConnectConnectionError(f"Token path not loading cleanly: {e}") from e
        self._tokenstore_path = path

    def dumps(self):
        return json.dumps({"di_token": self.token})

    def dump(self, path):
        raise AssertionError("non-atomic library dump used")

    def connectapi(self, path):
        self.world.check(self.token)
        return {"displayName": "athlete"}


def make_fake_garmin(world):
    class FakeGarmin:
        instances = []

        def __init__(self, email, password):
            self.username = email
            self.client = FakeSession(world)
            self.display_name = None
            FakeGarmin.instances.append(self)

        def login(self):
            with world.lock:
                world.logins += 1
            if world.login_error is not None:
                raise world.login_error
            self.client.token = world.login_token

        def get_user_profile(self):
            world.check(self.client.token)
            return {"userData": {}}

        def get_stats(self, day):
            if "get_stats" in world.errors:
                raise world.errors.pop("get_stats")
            if "get_stats" in world.always_401:
                raise _api_401()
            if self.client.token not in world.valid_tokens:
                if world.barrier is not None:
                    world.barrier.wait(timeout=5)
                raise _api_401()
            return {"day": day, "token": self.client.token}

    return FakeGarmin


@pytest.fixture
def world(tmp_path, monkeypatch):
    world = FakeGarminWorld(str(tmp_path))
    fake_cls = make_fake_garmin(world)
    world.cls = fake_cls
    monkeypatch.setattr(garmin_client, "Garmin", fake_cls)
    monkeypatch.setattr(garmin_client, "TOKEN_DIR", str(tmp_path))
    monkeypatch.setattr(garmin_client, "_client", None)
    monkeypatch.setattr(garmin_client, "_generation", 0)
    monkeypatch.setattr(garmin_client, "_last_login_failure", None)
    monkeypatch.setattr(garmin_client, "_last_recovery_failure", None)
    monkeypatch.delenv("GARMIN_RELOGIN_COOLDOWN_SECONDS", raising=False)
    return world


def _start_with_tokens(world, token="old"):
    """Server starts with valid `token` on disk, then Garmin stops accepting it."""
    world.write_disk(token)
    world.valid_tokens = {token}
    client = get_client()
    client.get_stats("warmup")
    world.valid_tokens = set()
    world.loads = 0
    return client


# ── detection ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("exc, expected", [
    (_api_401(), True),
    (GarminConnectConnectionError("API Error 401 - Unauthorized"), True),
    (GarminConnectAuthenticationError("Authentication failed"), True),
    (GarminConnectConnectionError("HTTP error: API Error 429 - "), False),
    (GarminConnectTooManyRequestsError("Rate limit exceeded"), False),
    (GarminConnectConnectionError("HTTP error: API Error 500 - "), False),
    (GarminConnectConnectionError("API client error (404): activity 24011401 not found"), False),
    (ValueError("bad date"), False),
    (GarminAuthExpired("already tried"), False),
])
def test_is_auth_error(exc, expected):
    assert is_auth_error(exc) is expected


def test_is_auth_error_follows_the_cause_chain():
    try:
        try:
            raise _api_401()
        except Exception as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert is_auth_error(outer)


# ── 1. reload from disk ──────────────────────────────────────────────────────

def test_401_reloads_tokens_from_disk_and_retries_once(world):
    client = _start_with_tokens(world)
    world.write_disk("synced")          # the sync job refreshed and saved
    world.valid_tokens = {"synced"}

    assert client.get_stats("2026-09-01") == {"day": "2026-09-01", "token": "synced"}
    assert world.loads == 1
    assert world.logins == 0


# ── 2. fresh login ───────────────────────────────────────────────────────────

def test_reload_failure_falls_back_to_fresh_login_and_saves_tokens(world):
    client = _start_with_tokens(world)
    world.valid_tokens = {"fresh"}      # disk still holds the dead "old" token

    assert client.get_stats("d")["token"] == "fresh"
    assert world.loads == 1
    assert world.logins == 1
    assert world.read_disk() == "fresh"
    assert not [f for f in os.listdir(world.token_dir) if f.endswith(".tmp")]


def test_library_token_refresh_is_persisted_atomically(world):
    _start_with_tokens(world)
    world.write_disk("synced")
    world.valid_tokens = {"synced"}
    get_client().get_stats("d")

    session = garmin_client.current_garmin().client
    assert session._tokenstore_path == world.token_dir
    session.token = "refreshed-in-memory"
    session.dump(session._tokenstore_path)   # what the library calls after a refresh
    assert world.read_disk() == "refreshed-in-memory"


# ── 3. exactly one retry ─────────────────────────────────────────────────────

def test_retry_that_still_401s_raises_without_looping(world, monkeypatch):
    client = _start_with_tokens(world)
    world.write_disk("synced")
    world.valid_tokens = {"synced"}
    world.always_401.add("get_stats")

    calls = []
    real = world.cls.get_stats
    monkeypatch.setattr(world.cls, "get_stats",
                        lambda self, day: (calls.append(day), real(self, day))[1])

    with pytest.raises(GarminAuthExpired, match="even after re-authenticating"):
        client.get_stats("d")
    assert len(calls) == 2
    assert world.loads == 1


# ── 4. non-auth errors pass straight through ─────────────────────────────────

@pytest.mark.parametrize("error", [
    GarminConnectConnectionError("HTTP error: API Error 429 - "),
    GarminConnectTooManyRequestsError("Rate limit exceeded"),
    GarminConnectConnectionError("HTTP error: API Error 500 - "),
    ValueError("invalid date"),
])
def test_non_auth_errors_are_not_retried(world, error):
    world.write_disk("old")
    world.valid_tokens = {"old"}
    client = get_client()
    world.loads = 0
    world.errors["get_stats"] = error

    with pytest.raises(type(error)) as raised:
        client.get_stats("d")
    assert raised.value is error
    assert world.loads == 0
    assert world.logins == 0


# ── 5. concurrency ───────────────────────────────────────────────────────────

def test_concurrent_401s_recover_once(world):
    client = _start_with_tokens(world)
    world.write_disk("synced")
    world.barrier = threading.Barrier(8)   # all 8 calls fail before any recovers
    world.valid_tokens = set()

    def call(i):
        return client.get_stats(i)

    # Garmin starts accepting the synced token only once every thread has 401'd.
    original_wait = world.barrier.wait

    def wait_then_rotate(timeout=None):
        original_wait(timeout=timeout)
        world.valid_tokens = {"synced"}
        world.barrier = None
    world.barrier.wait = wait_then_rotate

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(8)))

    assert [r["token"] for r in results] == ["synced"] * 8
    assert world.loads == 1
    assert world.logins == 0


def test_concurrent_401s_share_one_failed_recovery(world):
    client = _start_with_tokens(world)
    world.login_error = GarminConnectAuthenticationError("MFA Required but no prompt_mfa mechanism supplied")
    world.barrier = threading.Barrier(8)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(client.get_stats, i) for i in range(8)]
        errors = [f.exception(timeout=10) for f in futures]

    assert all(isinstance(e, GarminAuthExpired) for e in errors)
    assert world.loads == 1
    assert world.logins == 1


# ── 6. cooldown ──────────────────────────────────────────────────────────────

def test_failed_login_starts_a_cooldown_but_disk_reload_still_runs(world):
    client = _start_with_tokens(world)
    world.login_error = GarminConnectTooManyRequestsError("All login strategies rate limited (429).")

    with pytest.raises(GarminAuthExpired, match="automatic re-login failed") as first:
        client.get_stats("d")
    assert "test-deployment.md" in str(first.value)
    assert world.logins == 1 and world.loads == 1

    with pytest.raises(GarminAuthExpired, match="next automatic attempt"):
        client.get_stats("d")
    assert world.logins == 1          # no second SSO attempt inside the cooldown
    assert world.loads == 2           # but tokens on disk were re-checked

    # A manual token upload takes effect on the next call, no restart needed.
    world.write_disk("uploaded")
    world.valid_tokens = {"uploaded"}
    assert client.get_stats("d")["token"] == "uploaded"
    assert world.logins == 1


def test_login_is_retried_once_the_cooldown_has_passed(world, monkeypatch):
    client = _start_with_tokens(world)
    world.login_error = GarminConnectAuthenticationError("MFA Required")
    with pytest.raises(GarminAuthExpired):
        client.get_stats("d")

    monkeypatch.setenv("GARMIN_RELOGIN_COOLDOWN_SECONDS", "0")
    world.login_error = None
    world.valid_tokens = {"fresh"}
    assert client.get_stats("d")["token"] == "fresh"
    assert world.logins == 2


# ── 7. clients held from before the recovery ─────────────────────────────────

def test_client_grabbed_before_recovery_uses_the_new_instance(world):
    client = _start_with_tokens(world)
    before = garmin_client.current_garmin()
    world.write_disk("synced")
    world.valid_tokens = {"synced"}

    client.get_stats("first")           # triggers recovery
    after = garmin_client.current_garmin()
    assert after is not before
    assert client.client is after.client
    assert client.get_stats("second")["token"] == "synced"
    assert get_client() is client


# ── 8. token files are never deleted up front ────────────────────────────────

def test_recovery_never_deletes_saved_tokens(world):
    client = _start_with_tokens(world)
    world.login_error = GarminConnectAuthenticationError("401 Unauthorized (Invalid Username or Password)")

    with pytest.raises(GarminAuthExpired):
        client.get_stats("d")
    assert world.read_disk() == "old"


def test_reset_client_keeps_tokens_unless_asked(world):
    _start_with_tokens(world, token="old")
    garmin_client.reset_client()
    assert os.path.exists(world.token_file)
    garmin_client.reset_client(clear_tokens=True)
    assert not os.path.exists(world.token_file)


def test_reset_client_reauthenticates_from_disk(world):
    client = _start_with_tokens(world)
    world.write_disk("synced")
    world.valid_tokens = {"synced"}
    garmin_client.reset_client()
    assert client.get_stats("d")["token"] == "synced"
    assert world.logins == 0


# ── error surfacing through MCP ──────────────────────────────────────────────

def test_auth_expired_reaches_the_mcp_caller_readably(monkeypatch):
    import server
    from fastmcp import Client

    def expired():
        raise GarminAuthExpired(garmin_client._expired_message("MFA Required"))
    monkeypatch.setattr(server, "get_athlete_profile", expired)

    async def call():
        async with Client(server.mcp) as mcp_client:
            return await mcp_client.call_tool("athlete_profile", {}, raise_on_error=False)

    result = asyncio.run(call())
    assert result.is_error
    text = result.content[0].text
    assert "Garmin session expired and automatic re-login failed (MFA Required)" in text
    assert "Refreshing Garmin Tokens" in text
