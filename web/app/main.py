"""Web UI container: serves the single-page console and proxies to the core API.

The browser never talks to the core container directly, so the control token
stays server-side.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

CORE_URL = os.environ.get("CORE_URL", "http://core:8080")
TOKEN = os.environ.get("PROXYARR_TOKEN", "")
MAX_CONFIG_BYTES = 64 * 1024

INDEX = Path(__file__).parent / "static" / "index.html"
client: httpx.AsyncClient


@asynccontextmanager
async def lifespan(_: FastAPI):
    global client
    client = httpx.AsyncClient(
        base_url=CORE_URL,
        headers={"X-Proxyarr-Token": TOKEN},
        timeout=httpx.Timeout(30.0, connect=5.0),
    )
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(title="proxyarr", lifespan=lifespan)


class TunnelPatch(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=9999)


class PoolPatch(BaseModel):
    balance: str


class ProbePatch(BaseModel):
    urls: str


async def forward(method: str, path: str, **kwargs) -> Response:
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"core unreachable: {exc}") from exc
    if response.status_code == 204:
        return Response(status_code=204)
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text[:500]}
    return JSONResponse(status_code=response.status_code, content=payload)


@app.get("/")
async def index():
    return FileResponse(INDEX, media_type="text/html")


@app.get("/api/pool")
async def get_pool():
    return await forward("GET", "/api/pool")


@app.put("/api/pool")
async def set_pool(patch: PoolPatch):
    return await forward("PUT", "/api/pool", json=patch.model_dump())


@app.post("/api/tunnels")
async def create_tunnel(
    name: str = Form(default=""),
    config: str = Form(default=""),
    file: UploadFile | None = File(default=None),
):
    if file is not None and file.filename:
        raw = await file.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise HTTPException(status_code=413, detail="config larger than 64 KiB")
        try:
            config = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="config must be UTF-8 text") from exc
        name = name.strip() or Path(file.filename).stem
    elif len(config.encode()) > MAX_CONFIG_BYTES:
        raise HTTPException(status_code=413, detail="config larger than 64 KiB")

    if not config.strip():
        raise HTTPException(status_code=400, detail="upload a .conf file or paste its contents")
    if not name.strip():
        raise HTTPException(status_code=400, detail="name is required")

    return await forward(
        "POST", "/api/tunnels", json={"name": name.strip(), "config": config}
    )


@app.patch("/api/tunnels/{name}")
async def patch_tunnel(name: str, patch: TunnelPatch):
    return await forward(
        "PATCH", f"/api/tunnels/{name}", json=patch.model_dump(exclude_none=True)
    )


@app.delete("/api/tunnels/{name}")
async def delete_tunnel(name: str):
    return await forward("DELETE", f"/api/tunnels/{name}")


@app.post("/api/tunnels/{name}/restart")
async def restart_tunnel(name: str):
    return await forward("POST", f"/api/tunnels/{name}/restart")


@app.get("/api/tunnels/{name}/exit-ip")
async def tunnel_exit_ip(name: str):
    return await forward("GET", f"/api/tunnels/{name}/exit-ip")


@app.put("/api/probe")
async def set_probe(patch: ProbePatch):
    return await forward("PUT", "/api/probe", json=patch.model_dump())
