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


def test_create_workout_sport_type_is_restricted_to_running_and_cycling():
    schema = _get_tool_schema("create_workout")
    sport_type_schema = schema["properties"]["sport_type"]
    assert sport_type_schema["enum"] == ["running", "cycling"]


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
