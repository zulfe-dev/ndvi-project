from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.ndvi_routes import router
from app.core.config import FASTAPI_APP_TITLE, FASTAPI_APP_VERSION, STATIC_DIR

logging.basicConfig(level=logging.INFO)

app = FastAPI(title=FASTAPI_APP_TITLE, version=FASTAPI_APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(router)

__all__ = ["app"]
