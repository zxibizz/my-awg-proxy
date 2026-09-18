"""Control API consumed by the web container. Never published to the host."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config
from .manager import Manager
from .store import BALANCE_MODES, Store
from .tunnel import TunnelError
from .wgconf import ConfigError, parse

log = logging.getLogger("proxyarr.api")

settings = config.load()
store = Store(settings.data_dir, settings.seed_dir)
manager = Manager(store, settings)


def require_token(x_proxyarr_token: str = Header(default="")) -> None:
    if not settings.token:
        return
    if not secrets.compare_digest(x_proxyarr_token, settings.token):
        raise HTTPException(status_code=401, detail="invalid token")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(manager.start)
    try:
        yield
    finally:
        await asyncio.to_thread(manager.shutdown)


app = FastAPI(title="proxyarr core", lifespan=lifespan, dependencies=[Depends(require_token)])


class TunnelCreate(BaseModel):
    name: str
    config: str
    enabled: bool = True
    priority: int | None = None


class TunnelPatch(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=9999)


class PoolPatch(BaseModel):
    balance: str


class ProbePatch(BaseModel):
    urls: str


@app.exception_handler(ConfigError)
async def _config_error(_, exc: ConfigError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/pool")
async def get_pool():
    return await asyncio.to_thread(manager.status)


@app.put("/api/pool")
async def set_pool(patch: PoolPatch):
    if patch.balance not in BALANCE_MODES:
        raise HTTPException(status_code=400, detail=f"balance must be one of {BALANCE_MODES}")
    await asyncio.to_thread(store.set_balance, patch.balance)
    await asyncio.to_thread(manager.reconcile)
    return await asyncio.to_thread(manager.status)


@app.post("/api/tunnels", status_code=201)
async def create_tunnel(payload: TunnelCreate):
    try:
        parse(payload.config)
        await asyncio.to_thread(
            store.save, payload.name, payload.config, payload.enabled, payload.priority
        )
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await asyncio.to_thread(manager.reconcile)
    return {"name": payload.name}


@app.patch("/api/tunnels/{name}")
async def patch_tunnel(name: str, patch: TunnelPatch):
    try:
        await asyncio.to_thread(
            store.update, name, enabled=patch.enabled, priority=patch.priority
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no tunnel named {name}") from exc
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await asyncio.to_thread(manager.reconcile)
    return {"name": name}


@app.delete("/api/tunnels/{name}", status_code=204)
async def delete_tunnel(name: str):
    try:
        await asyncio.to_thread(store.delete, name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no tunnel named {name}") from exc
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await asyncio.to_thread(manager.reconcile)


@app.post("/api/tunnels/{name}/restart")
async def restart_tunnel(name: str):
    if store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"no tunnel named {name}")
    await asyncio.to_thread(manager.restart, name)
    return {"name": name}


@app.get("/api/tunnels/{name}/exit-ip")
async def tunnel_exit_ip(name: str):
    try:
        return {"name": name, "ip": await asyncio.to_thread(manager.exit_ip, name)}
    except TunnelError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/probe")
async def set_probe(patch: ProbePatch):
    try:
        await asyncio.to_thread(manager.apply_probe_urls, patch.urls)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await asyncio.to_thread(manager.status)
