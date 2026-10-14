# tests/test_pdf_route.py
"""The plan PDF route's cache and the download-link helper (tools/pdf_route.py).
The renderer itself is replaced — building a real PDF needs WeasyPrint."""
import asyncio

import pytest

from tests.plan_fakes import sample_plan
from tools import pdf_route


@pytest.fixture
def renders(monkeypatch):
    calls = []

    def fake_build(plan):
        calls.append(plan["meta"]["id"])
        return b"%PDF-" + plan["meta"]["id"].encode()

    monkeypatch.setattr(pdf_route, "_build_pdf_sync", fake_build)
    monkeypatch.setattr(pdf_route, "_cache", type(pdf_route._cache)())
    return calls


def _get(plan):
    return asyncio.run(pdf_route.render_plan_pdf(plan))


def test_an_unchanged_plan_is_rendered_once(renders):
    first, second = _get(sample_plan()), _get(sample_plan())

    assert renders == ["test-block-2026"]
    assert (first.headers["x-pdf-cache"], second.headers["x-pdf-cache"]) == ("miss", "hit")
    assert second.body == b"%PDF-test-block-2026"
    assert second.headers["content-disposition"] == 'attachment; filename="test-block-2026.pdf"'


def test_any_change_to_the_plan_renders_again(renders):
    plan = sample_plan()
    _get(plan)
    plan["overrides"] = {"ftp": 262}
    _get(plan)

    assert renders == ["test-block-2026", "test-block-2026"]


def test_the_cache_keeps_the_most_recent_plans(renders, monkeypatch):
    monkeypatch.setattr(pdf_route, "_CACHE_MAX", 2)
    for plan_id in ("a", "b", "c", "a"):
        _get(sample_plan(id=plan_id))

    assert renders == ["a", "b", "c", "a"]       # "a" had been evicted by "c"
    assert list(pdf_route._cache) and len(pdf_route._cache) == 2


def test_a_failed_render_is_a_500_and_is_not_cached(monkeypatch):
    monkeypatch.setattr(pdf_route, "_cache", type(pdf_route._cache)())

    def boom(plan):
        raise RuntimeError("no pango")
    monkeypatch.setattr(pdf_route, "_build_pdf_sync", boom)

    r = _get(sample_plan())
    assert r.status_code == 500 and b"no pango" in r.body
    assert len(pdf_route._cache) == 0


def test_inject_pdf_links_adds_the_script_once():
    page = pdf_route.inject_pdf_links("<html><body><a data-pdf href='/x'>PDF</a></body></html>")

    assert page.count("window.__pdfLinks") == 2 and page.index("__pdfLinks") < page.index("</body>")
    assert pdf_route.inject_pdf_links(page) == page
    assert "Generating PDF" in page and "a[data-pdf]" in page
