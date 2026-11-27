import base64
import io
import zipfile
from unittest.mock import MagicMock, patch

import pytest
from garminconnect import Garmin

from tools.activities import download_activity_fit

FIT = b"\x0e\x10\xd9\x07\x00\x00\x00\x00.FIT" + bytes(range(256))


def _zip(name, payload):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


def _patched(client):
    return patch("tools.activities.get_client", return_value=client)


def test_requests_original_format():
    c = MagicMock()
    c.download_activity.return_value = _zip("123_ACTIVITY.fit", FIT)
    with _patched(c):
        download_activity_fit(123)
    c.download_activity.assert_called_once_with(
        123, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL
    )


def test_base64_round_trips_zip_and_raw():
    for payload in (_zip("123_ACTIVITY.fit", FIT), FIT):
        c = MagicMock()
        c.download_activity.return_value = payload
        with _patched(c):
            r = download_activity_fit(123)
        assert base64.b64decode(r["fit_base64"]) == FIT
        assert r["size_bytes"] == len(FIT)
        assert r["filename"] == "123.fit"
        assert r["activity_id"] == 123


def test_download_failure_raises():
    c = MagicMock()
    c.download_activity.side_effect = RuntimeError("boom")
    with _patched(c), pytest.raises(ValueError, match="Failed to download.*boom"):
        download_activity_fit(123)


def test_unknown_activity_raises():
    c = MagicMock()
    c.download_activity.side_effect = RuntimeError("API Error 404 - Not Found")
    with _patched(c), pytest.raises(ValueError, match="No activity found with id 9"):
        download_activity_fit(9)


def test_empty_and_zip_without_fit_raise():
    for payload in (b"", _zip("x.txt", b"hi")):
        c = MagicMock()
        c.download_activity.return_value = payload
        with _patched(c), pytest.raises(ValueError):
            download_activity_fit(1)
