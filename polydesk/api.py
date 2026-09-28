"""FastAPI: serves the terminal and a JSON snapshot; runs the engine in-process."""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from .engine import Engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
DASH = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
app = FastAPI(title="polydesk")
engine = Engine()


@app.on_event("startup")
async def _start():
    asyncio.create_task(engine.run())


@app.get("/")
def index():
    return FileResponse(DASH)


@app.get("/api/state")
def state():
    return JSONResponse(engine.snapshot())
