# tests/test_server.py
"""
Tests that the MCP tool surface itself (not just the underlying tools/workout.py
helpers) restricts workout creation to running and cycling — this is the schema
the calling LLM actually sees, so it must be correct and self-contained.
"""
import asyncio

import server


def _get_tool_schema(tool_name: str) -> dict:
    async def _fetch():
        tool = await server.mcp.get_tool(tool_name)
        return tool.parameters

    return asyncio.run(_fetch())


def test_create_workout_sport_type_supports_running_cycling_and_strength():
    schema = _get_tool_schema("create_workout")
    sport_type_schema = schema["properties"]["sport_type"]
    assert sport_type_schema["enum"] == ["running", "cycling", "strength_training"]


def test_create_workout_docstring_documents_repeat_and_targets():
    """
    The MCP-facing docstring is the only schema the calling LLM sees — it must
    spell out the repeat-step and required-target rules inline, not just point
    at tools/workout.py (which the LLM cannot read).
    """
    doc = server.create_workout.__doc__
    assert "repeat" in doc
    assert "target" in doc.lower()
    assert "pace" in doc.lower()
    assert "power" in doc.lower()


def test_week_offset_is_clamped_to_zero_or_more():
    assert server._week_offset({"week": ["3"]}) == 3
    assert server._week_offset({"week": ["-2"]}) == 0
    assert server._week_offset({"week": ["nope"]}) == 0
    assert server._week_offset({}) == 0


def test_accepts_gzip_reads_the_request_header():
    assert server._accepts_gzip({"headers": [(b"accept-encoding", b"br, GZIP")]})
    assert not server._accepts_gzip({"headers": [(b"accept-encoding", b"identity")]})
    assert not server._accepts_gzip({"headers": []})


def test_gzip_flushing_makes_each_chunk_readable_as_soon_as_it_is_sent():
    """The dashboard's loading skeleton (the first chunk) must be decodable
    before the rest of the page exists — that's the point of streaming it."""
    import zlib

    async def chunks():
        yield "<head>skeleton</head>"
        yield "<main>page</main>"

    async def collect():
        return [part async for part in server._gzip_flushing(chunks())]

    parts = asyncio.run(collect())
    decoder = zlib.decompressobj(31)
    assert decoder.decompress(parts[0]) == b"<head>skeleton</head>"
    assert decoder.decompress(b"".join(parts[1:])) == b"<main>page</main>"


def _asgi_get(path):
    from starlette.testclient import TestClient
    return TestClient(server.build_asgi_app()).get(path)


def test_manifest_is_branded_ridgeline(monkeypatch):
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    manifest = _asgi_get("/manifest.webmanifest").json()
    assert manifest["name"] == "Ridgeline"
    assert manifest["short_name"] == "Ridgeline"
    assert manifest["theme_color"] == "#161826"
    assert len(manifest["icons"]) == 3


def test_strength_tools_are_registered_with_documented_parameters():
    patch_schema = _get_tool_schema("patch_workout_step")
    assert set(patch_schema["properties"]) >= {"workout_id", "exercise_name", "fields", "occurrence", "step_type"}
    search_schema = _get_tool_schema("search_exercises")
    assert set(search_schema["properties"]) >= {"query", "category", "equipment", "muscle", "found_only"}
    sets_schema = _get_tool_schema("strength_sets")
    assert "activity_id" in sets_schema["properties"]
    update_schema = _get_tool_schema("update_workout_weights")
    # original parameters are unchanged; new ones are optional additions
    assert set(update_schema["required"]) == {"workout_name", "weight_updates"}
    assert {"include_warmup", "warmup_updates"} <= set(update_schema["properties"])


def test_create_workout_docstring_documents_strength_timers_reps_and_pounds():
    doc = server.create_workout.__doc__
    for needle in ("duration_s", "reps", "rest_duration_s", "weight_lb", "set_weights_lb", "warnings", "SIDE_PLANK"):
        assert needle in doc
