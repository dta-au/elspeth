# Mock PDF fixtures

`report.pdf` and `broken.pdf` are hand-built, deterministic fixtures — no
external PDF tooling is involved, and regenerating them from the same source
produces byte-identical files.

- `report.pdf` — a valid 3-page PDF built by
  `tests/fixtures/pdf_documents.py::minimal_pdf`. `pdf_rasterize` renders it
  into 3 PNG page rows.
- `broken.pdf` — the fixture module's `MALFORMED_PDF` constant: a PDF header
  with no valid xref table. `pdf_rasterize` refuses it with reason
  `pdf_malformed`, demonstrating the quarantine path.

Regenerate both with:

```bash
PYTHONPATH=src .venv/bin/python -c "
import sys; sys.path.insert(0, 'tests')
from fixtures.pdf_documents import minimal_pdf, MALFORMED_PDF
from pathlib import Path
Path('examples/pdf_rasterize/input/report.pdf').write_bytes(minimal_pdf(3))
Path('examples/pdf_rasterize/input/broken.pdf').write_bytes(MALFORMED_PDF)
"
```
