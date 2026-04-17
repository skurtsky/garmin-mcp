# tests/test_client.py
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