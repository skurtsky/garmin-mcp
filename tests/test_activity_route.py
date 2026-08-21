# tests/test_activity_route.py
"""Tests for the GPS route/elevation-profile helper (tools/activity_route.py,
issue 74). Fully offline: garmin_client.get_client is monkeypatched with a
fake client that serves canned GPX text, so no live Garmin session is
needed.
"""
import pytest

from tools import activity_route

_SIMPLE_GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test">
  <trk><name>Test</name><trkseg>
    <trkpt lat="45.4000" lon="-75.7000"><ele>60.0</ele></trkpt>
    <trkpt lat="45.4010" lon="-75.7005"><ele>65.0</ele></trkpt>
    <trkpt lat="45.4020" lon="-75.7010"><ele>55.0</ele></trkpt>
    <trkpt lat="45.4030" lon="-75.7015"><ele>70.0</ele></trkpt>
  </trkseg></trk>
</gpx>"""

_EMPTY_GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test"><trk><name>Empty</name><trkseg></trkseg></trk></gpx>"""


class _FakeDownloadFormat:
    GPX = "gpx"


class _FakeClient:
    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls = 0
        self.ActivityDownloadFormat = _FakeDownloadFormat

    def download_activity(self, activity_id, dl_fmt=None):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._response


@pytest.fixture(autouse=True)
def clear_cache():
    """The route cache is a module-level dict shared across tests."""
    activity_route._route_cache.clear()
    yield
    activity_route._route_cache.clear()


def test_returns_none_when_download_raises(monkeypatch):
    fake = _FakeClient(raises=Exception("no GPS data for this activity"))
    monkeypatch.setattr(activity_route, "get_client", lambda: fake)
    assert activity_route.get_activity_route(123) is None


def test_returns_none_when_download_is_empty(monkeypatch):
    fake = _FakeClient(response=b"")
    monkeypatch.setattr(activity_route, "get_client", lambda: fake)
    assert activity_route.get_activity_route(123) is None


def test_returns_none_for_track_with_fewer_than_two_points(monkeypatch):
    fake = _FakeClient(response=_EMPTY_GPX.encode("utf-8"))
    monkeypatch.setattr(activity_route, "get_client", lambda: fake)
    assert activity_route.get_activity_route(123) is None


def test_returns_none_for_malformed_gpx(monkeypatch):
    fake = _FakeClient(response=b"not xml at all")
    monkeypatch.setattr(activity_route, "get_client", lambda: fake)
    assert activity_route.get_activity_route(123) is None


def test_parses_points_and_elevation_profile(monkeypatch):
    fake = _FakeClient(response=_SIMPLE_GPX.encode("utf-8"))
    monkeypatch.setattr(activity_route, "get_client", lambda: fake)

    result = activity_route.get_activity_route(123)

    assert result is not None
    assert len(result["points"]) == 4
    assert result["points"][0] == {"lat": 45.4, "lon": -75.7}
    assert len(result["elevation_profile"]) == 4
    assert result["elevation_profile"][0]["distance_km"] == 0.0
    assert result["elevation_profile"][0]["elevation_m"] == 60.0
    assert result["elevation_profile"][-1]["distance_km"] > 0
    # gpxpy.get_uphill_downhill() applies its own smoothing over the raw
    # elevation deltas — just check gain/loss come through as non-negative
    # numbers rather than pinning its exact smoothing behavior.
    assert result["elevation_gain_m"] >= 0
    assert result["elevation_loss_m"] >= 0
    assert result["elevation_gain_m"] > 0 or result["elevation_loss_m"] > 0


def test_accepts_str_response_as_well_as_bytes(monkeypatch):
    fake = _FakeClient(response=_SIMPLE_GPX)
    monkeypatch.setattr(activity_route, "get_client", lambda: fake)
    result = activity_route.get_activity_route(123)
    assert result is not None
    assert len(result["points"]) == 4


def test_downsamples_long_tracks(monkeypatch):
    points_xml = "".join(
        f'<trkpt lat="45.{i:04d}" lon="-75.{i:04d}"><ele>{60 + (i % 10)}</ele></trkpt>'
        for i in range(2000)
    )
    gpx = f'<?xml version="1.0"?><gpx version="1.1"><trk><trkseg>{points_xml}</trkseg></trk></gpx>'
    fake = _FakeClient(response=gpx.encode("utf-8"))
    monkeypatch.setattr(activity_route, "get_client", lambda: fake)

    result = activity_route.get_activity_route(123)

    assert result is not None
    assert len(result["points"]) < 2000
    assert len(result["points"]) <= activity_route._MAX_POINTS + 1
    # The final recorded point is always kept, not dropped by the stride.
    assert result["points"][-1] == {"lat": 45.1999, "lon": -75.1999}


def test_result_is_cached_across_calls(monkeypatch):
    fake = _FakeClient(response=_SIMPLE_GPX.encode("utf-8"))
    monkeypatch.setattr(activity_route, "get_client", lambda: fake)

    first = activity_route.get_activity_route(123)
    second = activity_route.get_activity_route(123)

    assert fake.calls == 1
    assert first is second


def test_none_result_is_also_cached(monkeypatch):
    """A no-GPS activity (or any failed parse) is cached too, so re-opening
    its detail page doesn't retry the download every time."""
    fake = _FakeClient(raises=Exception("no track"))
    monkeypatch.setattr(activity_route, "get_client", lambda: fake)

    assert activity_route.get_activity_route(123) is None
    assert activity_route.get_activity_route(123) is None
    assert fake.calls == 1


def test_different_activity_ids_are_cached_independently(monkeypatch):
    fake = _FakeClient(response=_SIMPLE_GPX.encode("utf-8"))
    monkeypatch.setattr(activity_route, "get_client", lambda: fake)

    activity_route.get_activity_route(123)
    activity_route.get_activity_route(456)

    assert fake.calls == 2
