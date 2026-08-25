# Drop documents here

Copy the JPEG, PNG, or single-page PDF files you want analyzed into this
directory (one format per staging run), then run
`python examples/textract_inline/scripts/prepare_document_blobs.py`.
This file is ignored by the prepare script. A multipage PDF is not accepted
here — rasterize it into per-page PNGs with the `pdf_rasterize` transform
first, then stage the rendered pages.
