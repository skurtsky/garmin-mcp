"""PDF rendering for the training-plan wall chart (tools/build_pdf.py).

Used by ``GET/POST /training-plan/pdf`` in tools/training_plan.py: a GET
renders the plan JSON extracted from the uploaded HTML, a POST renders
whatever the browser currently has (including live zone overrides).

WeasyPrint is the only engine, matching the container, so a local render
is a faithful preview of production.
"""
import hashlib
import json
import logging
import pathlib
import tempfile
import threading
from collections import OrderedDict

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from tools.build_pdf import build_html, via_weasyprint

logger = logging.getLogger(__name__)

# Rendering takes seconds; the PDF depends only on the plan document (zones
# and overrides live in it), so a finished one is kept, keyed by a hash of
# that document. Any edit changes the hash, so a stale PDF is never served.
_CACHE_MAX = 8
_cache: "OrderedDict[str, bytes]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_key(plan: dict) -> str:
    blob = json.dumps(plan, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _cached_pdf(plan: dict) -> tuple[bytes, bool]:
    """(pdf bytes, whether it came from the cache). Blocking."""
    key = _cache_key(plan)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit, True
    pdf = _build_pdf_sync(plan)
    with _cache_lock:
        _cache[key] = pdf
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return pdf, False


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
        pdf_bytes, cached = await run_in_threadpool(_cached_pdf, plan)
    except Exception as exc:
        logger.exception("PDF generation failed")
        return Response(f"PDF generation failed: {exc}", status_code=500)

    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.pdf"',
            "Cache-Control": "no-store",
            "X-PDF-Cache": "hit" if cached else "miss",
        },
    )

# Download links marked ``data-pdf`` (the plan viewer's Settings, the plans
# list): rendering takes a few seconds, so the link shows a spinner and the
# elapsed time while the PDF is fetched, then saves it — instead of a click
# that seems to do nothing. Without JavaScript the link still downloads.
PDF_LINK_STYLE = """
@keyframes pdf-spin { to { transform: rotate(360deg); } }
a[data-pdf].pdf-busy { pointer-events:none; opacity:.85; cursor:progress; }
a[data-pdf] .pdf-spin { display:inline-block; width:.9em; height:.9em; margin-right:.45em; vertical-align:-.12em;
  border:2px solid currentColor; border-right-color:transparent; border-radius:50%; animation:pdf-spin .8s linear infinite; }
a[data-pdf] .pdf-msg { margin-left:.35em; }
@media (prefers-reduced-motion: reduce) { a[data-pdf] .pdf-spin { animation-duration:2.4s; } }
"""

PDF_LINK_SCRIPT = """
(function () {
  if (window.__pdfLinks) return;
  window.__pdfLinks = true;
  function filename(res) {
    var m = /filename="?([^";]+)"?/.exec(res.headers.get('Content-Disposition') || '');
    return m ? m[1] : 'training-plan.pdf';
  }
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[data-pdf]');
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    e.stopPropagation();
    if (a.classList.contains('pdf-busy')) return;
    var label = a.innerHTML, started = Date.now();
    a.classList.add('pdf-busy');
    a.setAttribute('aria-busy', 'true');
    function busy() {
      var s = Math.floor((Date.now() - started) / 1000);
      a.innerHTML = '<span class="pdf-spin" aria-hidden="true"></span>Generating PDF\u2026' + (s >= 2 ? ' ' + s + 's' : '');
    }
    busy();
    var tick = setInterval(busy, 1000);
    function done(text) {
      clearInterval(tick);
      a.classList.remove('pdf-busy');
      a.removeAttribute('aria-busy');
      a.innerHTML = text;
      setTimeout(function () { a.innerHTML = label; }, 2500);
    }
    fetch(a.href, { credentials: 'same-origin' }).then(function (res) {
      if (!res.ok) return res.text().then(function (t) { throw new Error(t || ('Server returned ' + res.status)); });
      var name = filename(res);
      return res.blob().then(function (blob) {
        var url = URL.createObjectURL(blob), link = document.createElement('a');
        link.href = url; link.download = name; document.body.appendChild(link); link.click(); link.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
        done('\u2713<span class="pdf-msg">Downloaded</span>');
      });
    }).catch(function (err) {
      done('Try again');
      alert('Could not make the PDF: ' + err.message);
    });
  }, true);
})();
"""


def inject_pdf_links(page: str) -> str:
    """Add the data-pdf download behaviour to a full HTML page."""
    if "window.__pdfLinks" in page:
        return page
    snippet = f"<style>{PDF_LINK_STYLE}</style><script>{PDF_LINK_SCRIPT}</script>"
    i = page.rfind("</body>")
    return page[:i] + snippet + page[i:] if i != -1 else page + snippet
