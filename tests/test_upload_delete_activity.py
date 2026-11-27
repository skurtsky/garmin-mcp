import base64
import os
from unittest.mock import MagicMock, patch

import pytest

from tools.activities import delete_activity, upload_activity_fit

FIT = b"\x0e\x10\xd9\x07\x00\x00\x00\x00.FIT" + bytes(range(256))
B64 = base64.b64encode(FIT).decode()


def _patched(client):
    return patch("tools.activities.get_client", return_value=client)


def _ok(activity_id=555):
    return {"detailedImportResult": {"uploadId": 1, "successes": [{"internalId": activity_id}], "failures": []}}


def test_upload_writes_decoded_bytes_and_returns_id():
    seen = {}

    def fake_upload(path):
        seen["path"] = path
        with open(path, "rb") as f:
            seen["data"] = f.read()
        return _ok(555)

    c = MagicMock()
    c.upload_activity.side_effect = fake_upload
    with _patched(c):
        r = upload_activity_fit(B64, "ride.fit")
    assert seen["data"] == FIT
    assert os.path.basename(seen["path"]) == "ride.fit"
    assert not os.path.exists(seen["path"])  # temp file cleaned up
    assert r["activity_id"] == 555
    assert r["size_bytes"] == len(FIT)
    assert r["upload_status"] == _ok(555)


def test_upload_sanitises_filename():
    seen = {}
    c = MagicMock()
    c.upload_activity.side_effect = lambda p: seen.setdefault("p", p) and _ok()
    with _patched(c):
        upload_activity_fit(B64, "../../etc/evil")
    assert os.path.basename(seen["p"]) == "evil.fit"
    assert ".." not in seen["p"]


def test_upload_bad_base64_and_empty():
    c = MagicMock()
    with _patched(c):
        for bad in ("not base64!!", "", "   "):
            with pytest.raises(ValueError):
                upload_activity_fit(bad)
    c.upload_activity.assert_not_called()


def test_upload_failure_raises_and_cleans_up():
    seen = {}

    def boom(path):
        seen["path"] = path
        raise RuntimeError("boom")

    c = MagicMock()
    c.upload_activity.side_effect = boom
    with _patched(c), pytest.raises(ValueError, match="Failed to upload.*boom"):
        upload_activity_fit(B64)
    assert not os.path.exists(seen["path"])


def test_upload_duplicate_and_no_id_raise():
    c = MagicMock()
    c.upload_activity.side_effect = RuntimeError("409 Conflict")
    with _patched(c), pytest.raises(ValueError, match="duplicate"):
        upload_activity_fit(B64)
    c.upload_activity.side_effect = None
    c.upload_activity.return_value = {"detailedImportResult": {"successes": [], "failures": [{"messages": ["x"]}]}}
    with _patched(c), pytest.raises(ValueError, match="did not create"):
        upload_activity_fit(B64)


def test_delete_uses_string_id():
    c = MagicMock()
    with _patched(c):
        r = delete_activity(123)
    c.delete_activity.assert_called_once_with("123")
    assert r == {"activity_id": 123, "deleted": True}


def test_delete_errors_surface():
    c = MagicMock()
    c.delete_activity.side_effect = RuntimeError("API Error 404 - Not Found")
    with _patched(c), pytest.raises(ValueError, match="No activity found with id 9"):
        delete_activity(9)
    c.delete_activity.side_effect = RuntimeError("boom")
    with _patched(c), pytest.raises(ValueError, match="Failed to delete.*boom"):
        delete_activity(9)
