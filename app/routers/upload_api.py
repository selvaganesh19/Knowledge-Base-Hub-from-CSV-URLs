"""CSV/Excel upload endpoint."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import UPLOAD_DIR, get_settings
from app.db import get_db
from app.models import HarvestedURL, UploadBatch
from app.schemas import UploadResult
from app.services import jobs as job_registry
from app.services.crawl_status import CrawlStatus
from app.services.harvest import run_harvest_job
from app.services.reader import (
    ReaderError,
    analyse_urls,
    detect_url_column,
    load_rows,
    normalize_url,
)
from app.services.url_guard import domain_of

logger = logging.getLogger(__name__)

settings = get_settings()

router = APIRouter(prefix="/api", tags=["upload"])

ALLOWED_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm"}


@router.post("/upload/", response_model=UploadResult, status_code=201)
async def upload_file(
    file: UploadFile = File(..., description="CSV or Excel file containing URLs"),
    name: str | None = Form(None, description="Optional batch label"),
    url_column: str | None = Form(
        None, description="Override URL column detection by naming the column"
    ),
    harvest_now: bool = Form(True, description="Start harvesting immediately"),
    session: Session = Depends(get_db),
) -> UploadResult:
    """Parse an uploaded CSV/XLSX, store its URLs, and start the harvest job."""
    filename = file.filename or "upload"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"Unsupported file type '{suffix}'. Upload a .csv or .xlsx file.",
                "headers": [],
                "candidates": [],
            },
        )

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Prefix with a batch-unique token so repeated uploads of the same filename
    # do not overwrite each other's stored copy.
    stored_path = UPLOAD_DIR / filename
    counter = 1
    while stored_path.exists():
        stored_path = UPLOAD_DIR / f"{Path(filename).stem}_{counter}{suffix}"
        counter += 1

    payload = await file.read()
    # Read before writing: a 2 GB "CSV" must not reach the disk. The check is on
    # the bytes actually received, not the declared Content-Length, which a client
    # controls.
    if len(payload) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "error": (
                    f"File is {len(payload) / 1_000_000:.1f} MB, over the "
                    f"{settings.max_upload_bytes / 1_000_000:.0f} MB limit."
                ),
                "headers": [],
                "candidates": [],
            },
        )
    stored_path.write_bytes(payload)

    try:
        headers, rows = load_rows(stored_path)
    except ReaderError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "headers": [], "candidates": []},
        ) from exc

    column = url_column if url_column in headers else detect_url_column(headers, rows)
    if column is None:
        candidates = [
            header
            for header in headers
            if any(str(row.get(header, "")).lower().startswith("http") for row in rows)
        ]
        raise HTTPException(
            status_code=400,
            detail={
                "error": "No URL column detected. Pass url_column to name it explicitly.",
                "headers": headers,
                "candidates": candidates,
            },
        )

    extraction = analyse_urls(rows, column, allow_private=settings.allow_private_hosts)
    pairs = extraction.pairs
    skipped = extraction.skipped

    if len(pairs) > settings.max_urls_per_upload:
        raise HTTPException(
            status_code=413,
            detail={
                "error": (
                    f"This file has {len(pairs)} valid URLs, over the "
                    f"{settings.max_urls_per_upload} per-upload limit. Split it into "
                    "smaller files."
                ),
                "headers": headers,
                "candidates": [],
            },
        )

    batch = UploadBatch(
        original_filename=name or filename,
        stored_path=str(stored_path),
        row_count=len(rows),
        url_count=len(pairs),
        skipped_rows=skipped,
        url_column=column,
        headers=headers,
        status="running" if harvest_now else "queued",
        notes="" if pairs else "no valid URLs found in the selected column",
        rejected_json=extraction.reasons,
    )
    session.add(batch)
    session.flush()

    for row, url in pairs:
        # The original spreadsheet row is preserved so nothing from the input file
        # is lost, whatever columns it happened to have.
        session.add(
            HarvestedURL(
                batch_id=batch.id,
                url=url,
                normalized_url=normalize_url(url),
                domain=domain_of(url),
                crawl_status=str(CrawlStatus.PENDING),
                meta_json={"row": row},
            )
        )
    session.commit()
    session.refresh(batch)

    logger.info("upload '%s': %s (column=%s)", filename, extraction.summary(), column)

    # run_harvest_job chains the index job itself when AUTO_INDEX is on, so the
    # upload handler only has to start the first stage.
    job = job_registry.create_job(session, kind="harvest", batch_id=batch.id)
    if harvest_now:
        job_registry.spawn(job.id, run_harvest_job(job.id))
    else:
        job.status = "success"
        job.message = "harvest skipped (harvest_now=false)"
        session.commit()

    return UploadResult(
        batch_id=batch.id,
        url_count=len(pairs),
        row_count=len(rows),
        skipped_rows=skipped,
        url_column=column,
        headers=headers,
        harvest_job_id=job.id,
        valid_urls=extraction.valid,
        invalid_urls=extraction.invalid + extraction.rejected,
        duplicate_urls=extraction.duplicates,
        empty_rows=extraction.blank,
        rejections=extraction.reasons,
        summary=extraction.summary(),
    )
