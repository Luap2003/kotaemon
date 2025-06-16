"""
Tiny async helpers that call *your own* REST endpoints with httpx.
Nothing here touches internal SQLModel classes or pipelines.
"""
from __future__ import annotations
from typing import List, Dict
import httpx

# -------- users -------------------------------------------------------------
async def fetch_users(api_base_url: str) -> List[Dict]:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{api_base_url}/users/")
        r.raise_for_status()
        return r.json()["users"]


# -------- upload ------------------------------------------------------------
async def upload_pdf(
    *,
    api_base_url: str,
    file_bytes: bytes,
    filename: str,
    user_id: str,
) -> str:
    files = {"file": (filename, file_bytes, "application/pdf")}
    data  = {"user_id": user_id}
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{api_base_url}/upload/", files=files, data=data)
        r.raise_for_status()
        return r.json()["file_id"]


# -------- duplicate ---------------------------------------------------------
async def duplicate_pdf(
    *,
    api_base_url: str,
    file_id: str,
    original_user_id: str,
    new_user_id: str,
) -> str:
    data = {"original_user_id": original_user_id, "new_user_id": new_user_id}
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{api_base_url}/files/{file_id}/duplicate/", data=data
        )
        r.raise_for_status()
        return r.json()["new_file_id"]
