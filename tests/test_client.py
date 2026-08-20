# tests/test_client.py
from concurrent.futures import ThreadPoolExecutor

from garminconnect import Garmin

def test_client_returns_garmin_instance(client):
    """Client should return an authenticated Garmin instance."""
    assert isinstance(client, Garmin)

def test_client_is_cached(client):
    """Calling get_client() twice should return the same instance."""
    from garmin_client import get_client
    client2 = get_client()
    assert client is client2

def test_client_can_fetch_profile(client):
    """Client should be able to make a basic API call."""
    profile = client.get_user_profile()
    assert profile is not None
    assert 'userData' in profile
    assert profile['userData'].get('weight') is not None


def test_client_initialization_is_shared_across_threads(monkeypatch, tmp_path):
    """Parallel dashboard tasks must perform only one Garmin login."""
    import garmin_client

    class FakeSession:
        def dump(self, directory):
            pass

    class FakeGarmin:
        login_count = 0
        session = FakeSession()

        def __init__(self, email, password):
            self.client = self.session

        def login(self):
            type(self).login_count += 1

    monkeypatch.setattr(garmin_client, "Garmin", FakeGarmin)
    monkeypatch.setattr(garmin_client, "_client", None)
    monkeypatch.setattr(garmin_client, "TOKEN_DIR", str(tmp_path))

    with ThreadPoolExecutor(max_workers=8) as pool:
        clients = list(pool.map(lambda _: garmin_client.get_client(), range(8)))

    assert len({id(client) for client in clients}) == 1
    assert FakeGarmin.login_count == 1