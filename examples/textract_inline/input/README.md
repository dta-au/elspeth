# Drop documents here

Copy the JPEG, PNG, or single-page PDF files you want analyzed into this
directory (one format per staging run), then run
`python examples/textract_inline/scripts/prepare_document_blobs.py`.
This file is ignored by the prepare script. A multipage PDF is not accepted
here — this directory only feeds documents ready to analyze as-is. To
process a multipage PDF, wire `pdf_rasterize` in-pipeline between
`blob_rows` and `aws_textract_inline_analysis` instead; see the top-level
README.
