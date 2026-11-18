"""Bounded stdin/stdout PDF raster worker; no Frappe, URLs, forms or file output."""
from __future__ import annotations

import base64
from io import BytesIO
import json
import math
import resource
import sys


MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_PAGES = 200
MAX_PIXELS = 2_000_000
MAX_PNG_BYTES = 4 * 1024 * 1024


def render(data: bytes, page_number: int):
    import pypdfium2 as pdfium

    if len(data) > MAX_INPUT_BYTES or not data.lstrip().startswith(b"%PDF-"):
        raise ValueError("unsupported_pdf")
    document = pdfium.PdfDocument(data)
    try:
        count = len(document)
        if not 1 <= page_number <= count <= MAX_PAGES:
            raise ValueError("unsupported_page")
        page = document.get_page(page_number - 1)
        try:
            width, height = page.get_size()
            if not all(math.isfinite(value) and 0 < value <= 144000 for value in (width, height)):
                raise ValueError("unsupported_dimensions")
            scale = min(1.5, 1399 / width, 1999 / height, math.sqrt((MAX_PIXELS - 4000) / (width * height)))
            # Deliberately never initialize a form environment: no PDF/XFA
            # JavaScript, form actions or external-resource callbacks are enabled.
            bitmap = page.render(scale=scale, may_draw_forms=False, draw_annots=True, limit_image_cache=True)
            try:
                image = bitmap.to_pil().convert("RGB")
                try:
                    if image.width * image.height > MAX_PIXELS:
                        raise ValueError("unsupported_dimensions")
                    output = BytesIO()
                    image.save(output, format="PNG")
                    png = output.getvalue()
                    if len(png) > MAX_PNG_BYTES:
                        raise ValueError("unsupported_output")
                    return {"page": page_number, "page_count": count,
                        "png": base64.b64encode(png).decode("ascii")}
                finally:
                    image.close()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()


def main():
    # These are resource limits, not a claim of a full OS security sandbox.
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (3, 4))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    try:
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        result = render(data, int(sys.argv[1]))
    except Exception:
        return 1  # Never emit document data, paths or parser diagnostics.
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
