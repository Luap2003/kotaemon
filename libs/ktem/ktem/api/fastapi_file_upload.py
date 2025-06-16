# fastapi_file_upload.py
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks,status, Body, Path as APIPath
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import shutil
import os
from pathlib import Path
from sqlalchemy.exc import IntegrityError
from ktem.index.file.index import FileIndex
from ktem.index.file.pipelines import IndexPipeline
from ktem.db.engine import engine
from ktem.db.models import User
from sqlmodel import Session, select, SQLModel
from typing import Optional
import logging
import hashlib
import uuid
from datetime import datetime
from fastapi import status
from typing import Dict
import zipfile, io
import httpx
from pydantic import BaseModel
import sys
import asyncio
from typing import List
from pathlib import Path
import time
from ktem.api.endpoint_helpers import fetch_users, upload_pdf, duplicate_pdf
sys.path.append(str(Path(__file__).parents[4]))
from flowsettings import (
    INDEX_ID,
    INDEX_NAME,
    INDEX_CONFIG,
    UPLOAD_TEMP_DIR,
)

# --- app setup ---------------------------------------------------------------
app = FastAPI(title="Kotaemon File‐Upload & Search API",
                  openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class UserCreate(SQLModel):
    username: str
    password: str
    admin: Optional[bool] = False



class GroupPayload(BaseModel):
    group_id: Optional[str] = None    # pass None to create a new group
    group_name: str
    file_ids: List[str]               # the list of file UUIDs to add
    user_id: str   
    
class AddFilesPayload(BaseModel):
    file_ids: List[str]
    user_id: str

class DistributePdfPayload(BaseModel):
    wait_time: Optional[int] = 20  # seconds to wait for processing
    
# make sure our temp‐upload directory exists
Path(UPLOAD_TEMP_DIR).mkdir(parents=True, exist_ok=True)

# one FileIndex instance per index
file_index = FileIndex(app, id=INDEX_ID, name=INDEX_NAME, config=INDEX_CONFIG)

file_index.on_create()
file_index.on_start()


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
    Source = doc_pipeline.Source  

    # 2) query all rows
    try:
        with Session(engine) as session:
            stmt = select(Source).where(Source.user == user_id)
            rows = session.exec(stmt).all()
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

@app.get("/users/")
async def list_users():
    """
    List every user row with its public fields.
    Adjust the fields you expose as needed.
    """
    try:
        with Session(engine) as session:
            rows = session.exec(select(User)).all()
    except Exception as e:
        raise HTTPException(500, f"Could not list users: {e}")

    return {
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "admin": u.admin,
            }
            for u in rows
        ]
    }

@app.post("/users/", status_code=status.HTTP_201_CREATED)
async def add_user(user_in: UserCreate = Body(...)):
    """
    Create a new user.

    Expects **JSON**:
    {
      "username": "<string>",
      "password": "<string>",
      "admin": false   # optional
    }
    """
    username_lower = user_in.username.lower()
    password_hash = hashlib.sha256(user_in.password.encode()).hexdigest()

    with Session(engine) as session:
        # enforce one-username-per-instance, case-insensitive
        duplicate = session.exec(
            select(User).where(User.username_lower == username_lower)
        ).first()
        if duplicate:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Username '{user_in.username}' already exists.",
            )

        db_user = User(
            id=uuid.uuid4().hex,  # Explicitly generate UUID to avoid NOT NULL constraint
            username=user_in.username,
            username_lower=username_lower,
            password=password_hash,
            admin=user_in.admin or False,
        )
        session.add(db_user)
        session.commit()
        session.refresh(db_user)

    return {
        "id": db_user.id,
        "username": db_user.username,
        "admin": db_user.admin,
    }  


@app.post("/files/{file_id}/duplicate/", status_code=status.HTTP_201_CREATED)
async def duplicate_file(
    background_tasks: BackgroundTasks,
    file_id: str,
    original_user_id: str = Form(...),
    new_user_id: str = Form(...),
):
    """
    Clone a file’s metadata *and* its index‐table entries
    so the new user sees the exact same chunks & embeddings.
    """
    # 1) spin up two pipelines—one for the original user, one for the new user—
    #    and route on a dummy “.pdf” so we get the right models & FSPath.
    dummy_pdf = Path("dummy.pdf")
    orig_pipe = file_index.get_indexing_pipeline({}, original_user_id).route(dummy_pdf)
    new_pipe  = file_index.get_indexing_pipeline({}, new_user_id).route(dummy_pdf)

    Source     = orig_pipe.Source       # SQLModel class for your files table
    IndexEntry = orig_pipe.Index        # SQLModel class for your index‐mapping table

    with Session(engine) as session:
        # --- fetch the original file (inside the session!) ----------------
        stmt = select(Source).where(
            Source.id   == file_id,
            Source.user == original_user_id
        )
        original = session.exec(stmt).one_or_none()
        if not original:
            raise HTTPException(status_code=404, detail="Original file not found")

        # --- create the new Source row ------------------------------------
        new_id = str(uuid.uuid4())
        cloned = Source(
            id           = new_id,
            name         = original.name,
            path         = original.path,
            size         = original.size,
            user         = new_user_id,
            note         = original.note,
            date_created = datetime.utcnow(),
        )
        session.add(cloned)

        # --- clone every Index entry for that file ------------------------
        idxs = session.exec(
            select(IndexEntry).where(IndexEntry.source_id == file_id)
        ).all()
        for entry in idxs:
            session.add(IndexEntry(
                source_id     = new_id,
                target_id     = entry.target_id,
                relation_type = entry.relation_type,
            ))

        # --- commit all at once -------------------------------------------
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(
                status_code=500,
                detail="Failed to clone file/index entries"
            )

    return {"new_file_id": new_id}



@app.post("/bulk_upload/")
async def bulk_upload_zip(
    zip_file: UploadFile = File(...),
    user_id: str = Form("api"),
    upload_url: str = Form("http://localhost:7860/dokumentation/upload/"),
):
    # read entire zip into memory
    data = await zip_file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(400, "Uploaded file is not a valid ZIP archive.")

    results = []
    # async client to call our own upload endpoint
    async with httpx.AsyncClient() as client:
        for name in zf.namelist():
            if not name.lower().endswith(".pdf"):
                continue

            pdf_bytes = zf.read(name)
            files = {
                "file": (name, pdf_bytes, "application/pdf")
            }
            form = {"user_id": user_id}

            try:
                resp = await client.post(upload_url, files=files, data=form)
                resp.raise_for_status()
                payload = resp.json()
                results.append({
                    "filename": name,
                    "status": "accepted",
                    "file_id": payload.get("file_id")
                })
            except httpx.HTTPStatusError as e:
                # non-2xx from /upload/
                results.append({
                    "filename": name,
                    "status": "error",
                    "detail": f"{e.response.status_code}: {e.response.text}"
                })
            except Exception as e:
                # network / parsing error
                results.append({
                    "filename": name,
                    "status": "failed",
                    "detail": str(e)
                })

    if not results:
        raise HTTPException(400, "No PDF files found in the ZIP.")

    return {"results": results}

@app.post("/groups/", status_code=201)
async def upsert_group(payload: GroupPayload):
    """
    Create a new file‐group or update an existing one.
    - If payload.group_id is None, this will create a new group named payload.group_name.
    - Otherwise it will rename/update the membership of the existing group.
    """
    try:
        new_group_id = file_index.save_group(
            payload.group_id,
            payload.group_name,
            payload.file_ids,
            payload.user_id
        )
    except Exception as e:
        # e.g. IntegrityError if you try to re-create an existing name
        raise HTTPException(status_code=400, detail=str(e))
    return {"group_id": new_group_id}

@app.post("/groups/{group_id}/files", status_code=200)
async def add_files_to_group(
    group_id: str = APIPath(..., description="UUID of the group to update"),
    payload: AddFilesPayload = Body(...)
):
    """
    Add files to an existing group.
    - `group_id`: the UUID of the group to modify
    - `payload.file_ids`: list of file UUIDs to add
    - `payload.user_id`: owner of the group (must match)
    """
    # Access the SQLModel for FileGroup
    FileGroup = file_index._resources["FileGroup"]
    # Fetch the group and verify ownership
    from sqlmodel import Session, select
    with Session(engine) as session:
        group = session.exec(
            select(FileGroup).where(
                FileGroup.id == group_id,
                FileGroup.user == payload.user_id
            )
        ).one_or_none()
        if not group:
            raise HTTPException(404, detail="Group not found or not yours")

    # Merge existing files with new ones (dedupe)
    existing = group.data.get("files", [])
    merged = list(dict.fromkeys(existing + payload.file_ids))

    # Delegate to your FileIndex method to save
    try:
        updated_group_id = file_index.save_group(
            group_id,
            group.name,
            merged,
            payload.user_id
        )
    except Exception as e:
        raise HTTPException(400, detail=str(e))

    return {"group_id": updated_group_id, "files": merged}


@app.post(
    "/distribute_pdf/",
    summary="Upload a PDF to the admin account and duplicate it to every other user",
    status_code=status.HTTP_202_ACCEPTED,
)
async def distribute_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="The PDF you want to distribute"),
    wait_time: int = Form(20, description="Seconds to wait before duplicating"),
    api_base_url: str = Form(
        "http://localhost:7860/dokumentation",
        description="Root URL where this API is reachable",
    ),
):
    """
    • GET **/users/** → detect the single *admin* account  
    • POST **/upload/**  → store the PDF for that admin  
    • After `wait_time` s, POST **/files/{id}/duplicate/** for every non-admin user

    All work is done via ordinary HTTP calls so you reuse exactly the same
    validation, deduping, and background-indexing logic already exposed by
    your existing routes.
    """
    # -----------------------------------------------------------------------
    # Step 1 – Collect the user list + find the admin
    # -----------------------------------------------------------------------
    users = await fetch_users(api_base_url)
    admin = next((u for u in users if u.get("admin")), None)
    if not admin:
        raise HTTPException(500, "No admin user found")
    admin_user_id = admin["id"]

    # -----------------------------------------------------------------------
    # Step 2 – Upload the PDF to the admin account
    # -----------------------------------------------------------------------
    file_bytes = await file.read()
    file_id = await upload_pdf(
        api_base_url=api_base_url,
        file_bytes=file_bytes,
        filename=file.filename,
        user_id=admin_user_id,
    )

    # -----------------------------------------------------------------------
    # Step 3 – schedule the duplicate run in the background
    # -----------------------------------------------------------------------
    async def _duplicate_to_everyone():
        await asyncio.sleep(max(0, wait_time))

        tasks = []
        for u in users:
            if u["id"] == admin_user_id:   # skip the admin themself
                continue
            tasks.append(
                duplicate_pdf(
                    api_base_url=api_base_url,
                    file_id=file_id,
                    original_user_id=admin_user_id,
                    new_user_id=u["id"],
                )
            )

        # run the /duplicate/ calls concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        successes = sum(1 for r in results if not isinstance(r, Exception))
        failures  = len(results) - successes
        logging.info(
            "[distribute_pdf] finished – duplicated to %d users (%d failures)",
            successes,
            failures,
        )

    background_tasks.add_task(_duplicate_to_everyone)

    return {
        "status": "accepted",
        "file_id": file_id,
        "admin_user_id": admin_user_id,
        "msg": f"PDF stored for admin; will distribute to everyone else in ≈{wait_time}s",
    }
    
import uvicorn


if __name__ == "__main__":    
    uvicorn.run(
        "fastapi_file_upload:app",
        host="0.0.0.0",
        port=8000,
        reload=True,           # auto-reload on code changes in dev
    )