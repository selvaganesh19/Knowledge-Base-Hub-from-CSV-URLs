# Example inputs

## `demo_urls.csv`

A three-URL list covering both harvesting paths: two HTML leadership pages and one
PDF document.

```bash
# Serve the PDF so its URL is reachable, then run the pipeline.
python -m http.server 8099 --directory examples

python -m app.cli import-urls examples/demo_urls.csv
```

Or paste `examples/demo_urls.csv` into the upload page while that server is running.

## `northwind_leadership.pdf`

A small four-page PDF describing the leadership team of "Northwind Robotics" — a
company that does not exist. The people and roles in it were invented for this
example, which makes it a useful check that answers really come from the harvested
data:

```bash
python -m app.cli search "Who is the CFO of Northwind Robotics?"
```

If the answer names Marta Reyes and cites the PDF, then the text was fetched, stored
in SQLite, chunked, embedded into the FAISS index, retrieved, and handed to the
model as context. No model could know this from its training data, so a correct
answer can only have come from the pipeline.

PDFs are read with `pypdf`; `.docx`, `.txt`, `.md`, `.csv`, `.json` and `.xml` are
also supported. Anything else binary (images, video, archives) is stored as a row
with the reason recorded rather than silently dropped.
