# fastapi_file_upload.py
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import shutil
import os
from pathlib import Path
from sqlalchemy.exc import IntegrityError
from ktem.index.file.index import FileIndex
from ktem.index.file.pipelines import IndexPipeline
from ktem.db.engine import engine
from sqlmodel import Session, select

import logging

from flowsettings import (
    INDEX_ID,
    INDEX_NAME,
    INDEX_CONFIG,
    UPLOAD_TEMP_DIR,
)

# --- app setup ---------------------------------------------------------------
app = FastAPI(title="Kotaemon File‐Upload & Search API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# make sure our temp‐upload directory exists
Path(UPLOAD_TEMP_DIR).mkdir(parents=True, exist_ok=True)

# one FileIndex instance per index
file_index = FileIndex(app=None, id=INDEX_ID, name=INDEX_NAME, config=INDEX_CONFIG)


@app.on_event("startup")
def startup_index():
    # 1) create the tables (only needed first time, but safe to rerun)
    file_index.on_create()
    # 2) wire up the pipelines & resources
    file_index.on_start()
    
def _background_index(file_id: str, tmp_path: Path, user_id: str):
    # 1) get the “document” pipeline factory
    doc_pipeline = file_index.get_indexing_pipeline({}, user_id)
    # 2) resolve it to the real IndexPipeline
    index_pipeline = doc_pipeline.route(tmp_path)

    # 2) look up the Source row to find the SHA256 “path” you stored
    with Session(engine) as session:
        src = session.get(index_pipeline.Source, file_id)
        if not src:
            # somehow the DB record went missing
            return

    # 3) the real PDF lives at FSPath/<sha256>
    real_pdf = index_pipeline.FSPath / src.path
    if not real_pdf.exists():
        # safety check
        print(f"[index_failure] file_id={file_id} missing on disk: {real_pdf}")
        return
    # 3) load the raw pages/thumbnails/text
    extra_info = {
        "file_name": tmp_path.name,
        "file_id": file_id,
        "collection_name": index_pipeline.collection_name,
    }
    docs = index_pipeline.loader.load_data(tmp_path, extra_info=extra_info)

    # 4) walk through handle_docs to populate the Index & DocStore/VectorStore
    for _ in index_pipeline.handle_docs(docs, file_id, tmp_path.name):
        pass

    # 5) now that there *are* document chunks, finish() will compute tokens
    index_pipeline.finish(file_id, tmp_path)
# --- Upload endpoint --------------------------------------------------------
@app.post("/upload/")
async def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: str = Form("api"),
):
    # Normalize allowed extensions & max size
    allowed_exts = [e.strip().lower() for e in INDEX_CONFIG["supported_file_types"].split(",") if e.strip()]
    max_bytes = INDEX_CONFIG["max_file_size"] * 1_000_000

    # Read everything into memory so we can validate before touching disk
    filename = Path(file.filename).name
    ext = Path(filename).suffix.lower()
    contents = await file.read()
    size = len(contents)

    # Validate
    if ext not in allowed_exts:
        raise HTTPException(400, f"Unsupported file type: {ext}. Allowed: {allowed_exts}")
    if size > max_bytes:
        raise HTTPException(400, f"File too large: {size} bytes (max {max_bytes})")

    # Write temp file
    temp_path = Path(UPLOAD_TEMP_DIR) / filename
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_bytes(contents)

    # Prepare pipelines up front
    doc_pipeline = file_index.get_indexing_pipeline({}, user_id)
    index_pipeline = doc_pipeline.route(temp_path)

    # Try to store or dedupe
    try:
        file_id = index_pipeline.store_file(temp_path)
    except IntegrityError:
        # already exists for this (name,user) combo
        existing = index_pipeline.get_id_if_exists(temp_path)
        if not existing:
            raise HTTPException(500, "Duplicate record but could not find existing ID.")
        file_id = existing
    except Exception as e:
        # on any other error, clean up and bubble
        temp_path.unlink(missing_ok=True)
        logging.exception("Failed to store file")
        raise HTTPException(500, f"Failed to store file: {e}")

    # Kick off the background finish (chunking/indexing)
    background_tasks.add_task(_background_index, file_id, temp_path, user_id)

    # Return the stored or deduped ID
    return JSONResponse({
        "status": "accepted",
        "file_id": file_id,
        "filename": filename
    })
    
    
# --- Search endpoint --------------------------------------------------------
@app.post("/search/")
async def search_files(query: str = Form(...), top_k: int = Form(5), user_id: str = Form("api")):
    """
    Simple full-text search over all indexed files.
    Returns up to `top_k` hits.
    """
    try:
        # adjust this call to match your actual search API
        results = file_index.search(query, top_k=top_k, user_id=user_id)
    except AttributeError:
        # maybe you need to build a query pipeline instead?
        pipeline = file_index.get_query_pipeline({}, user_id)
        results = pipeline.search(query, top_k)
    except Exception as e:
        raise HTTPException(500, f"Search failed: {e}")

    return {"query": query, "top_k": top_k, "results": results}


@app.get("/files/")
async def list_files(user_id: str = "api"):
    # 1) get the indexing “factory” and then use its Source model
    doc_pipeline = file_index.get_indexing_pipeline({}, user_id)
    Source = doc_pipeline.Source  # this is the SQLModel/SQLAlchemy class for your source table

    # 2) query all rows
    try:
        with Session(engine) as session:
            rows = session.exec(select(Source)).all()
    except Exception as e:
        raise HTTPException(500, f"Could not list files: {e}")

    # 3) serialize to JSON-able dicts
    files = []
    for src in rows:
        files.append({
            "id": src.id,
            "name": src.name,
            "path": src.path,
            "size": src.size,
            "user": src.user,
            "created": src.date_created.isoformat(),
            "note": src.note,
        })

    return {"files": files}


import uvicorn


if __name__ == "__main__":    
    uvicorn.run(
        "fastapi_file_upload:app",
        host="0.0.0.0",
        port=8000,
        reload=True,           # auto-reload on code changes in dev
    )