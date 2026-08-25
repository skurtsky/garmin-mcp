"""PDF rendering for the training-plan wall chart (tools/build_pdf.py).

Used by ``GET/POST /training-plan/pdf`` in tools/training_plan.py: a GET
renders the plan JSON extracted from the uploaded HTML, a POST renders
whatever the browser currently has (including live zone overrides).

WeasyPrint is the only engine, matching the container, so a local render
is a faithful preview of production.
"""
import logging
import pathlib
import tempfile

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from tools.build_pdf import build_html, via_weasyprint

logger = logging.getLogger(__name__)


def _build_pdf_sync(plan: dict) -> bytes:
    """Blocking work — call through run_in_threadpool, never on the loop."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        html_path = tmp_path / "plan-print.html"
        pdf_path = tmp_path / "plan.pdf"
        html_path.write_text(build_html(plan), encoding="utf-8")

        if not via_weasyprint(html_path, pdf_path):
            raise RuntimeError(
                "WeasyPrint is unavailable on this server "
                "(needs pango/harfbuzz)"
            )

        return pdf_path.read_bytes()


async def render_plan_pdf(plan: dict) -> Response:
    """Render a plan dict to a PDF download response."""
    plan_id = (plan.get("meta") or {}).get("id") or "training-plan"
    # The id lands in a Content-Disposition header — keep it to safe characters.
    filename = "".join(c for c in str(plan_id) if c.isalnum() or c in "-_") or "training-plan"

    try:
        pdf_bytes = await run_in_threadpool(_build_pdf_sync, plan)
    except Exception as exc:
        logger.exception("PDF generation failed")
        return Response(f"PDF generation failed: {exc}", status_code=500)

    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.pdf"',
            "Cache-Control": "no-store",
        },
    )