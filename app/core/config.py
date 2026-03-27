from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"

FASTAPI_APP_TITLE = "High-Performance NDVI Map API"
FASTAPI_APP_VERSION = "1.0.0"

GEOCODER_TIMEOUT_SECONDS = 5
GEOCODER_DELAY_SECONDS = 0.2
GEOCODER_MAX_RETRIES = 2
GEOCODER_USER_AGENT = "ndvi_fastapi_backend"
REVERSE_GEOCODER_TIMEOUT_SECONDS = 2
NDVI_PIXEL_BUFFER_METERS = 20
