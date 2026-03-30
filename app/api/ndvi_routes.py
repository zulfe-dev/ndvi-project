from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from functools import lru_cache
from threading import Lock

import ee
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from geopy.exc import GeocoderServiceError, GeocoderTimedOut, GeocoderUnavailable
from geopy.geocoders import Nominatim
from pydantic import BaseModel, Field

from app.core.config import (
    GEOCODER_DELAY_SECONDS,
    GEOCODER_MAX_RETRIES,
    GEOCODER_TIMEOUT_SECONDS,
    GEOCODER_USER_AGENT,
    NDVI_PIXEL_BUFFER_METERS,
    REVERSE_GEOCODER_TIMEOUT_SECONDS,
    STATIC_DIR,
    TEMPLATE_DIR,
)
from app.services.ndvi_service import (
    BoundaryLookupError,
    BoundaryRepository,
    EarthEngineUnavailableError,
    NdviTileService,
)

logger = logging.getLogger(__name__)
router = APIRouter()
GEOCODER = Nominatim(user_agent=GEOCODER_USER_AGENT, timeout=GEOCODER_TIMEOUT_SECONDS)
_geocoder_lock = Lock()
_last_geocoder_call = 0.0
EE_READY = False

boundary_repository = BoundaryRepository(STATIC_DIR)
ndvi_tile_service = NdviTileService(boundary_repository)


class WeatherRequest(BaseModel):
    locality: str
    date: str
    state: str | None = None
    data_type: str = "rainfall"


class RainFallRequest(BaseModel):
    locality: str
    date: str
    state: str | None = None


class NdviTileRequest(BaseModel):
    state: str = Field(..., min_length=2)
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class LegacyWeatherRequest(BaseModel):
    date: str
    state: str | None = None
    district: str | None = None
    locality: str | None = None


class LegacyWeatherRangeRequest(BaseModel):
    state: str | None = None
    district: str
    from_date: str
    to_date: str


class LegacyRainFallRequest(BaseModel):
    date: str
    state: str | None = None
    district: str | None = None
    locality: str | None = None


@router.on_event("startup")
async def warm_application() -> None:
    global EE_READY

    try:
        boundary_repository.warm()
    except Exception as exc:  # pragma: no cover - startup depends on data files
        logger.warning("Boundary cache warmup failed: %s", exc)

    try:
        EE_READY = ndvi_tile_service.initialize()
    except EarthEngineUnavailableError as exc:
        EE_READY = False
        logger.warning("Earth Engine startup unavailable: %s", exc)

    if not EE_READY:
        logger.warning("Earth Engine is not ready at startup; NDVI and weather endpoints will return 503 until it initializes.")


def _parse_bounds(raw_bounds: str | None) -> tuple[float, float, float, float] | None:
    if not raw_bounds:
        return None

    parts = raw_bounds.split(",")
    if len(parts) != 4:
        raise HTTPException(
            status_code=422,
            detail="bounds must be provided as west,south,east,north",
        )

    try:
        west, south, east, north = (float(part) for part in parts)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="bounds must contain numbers") from exc

    return (west, south, east, north)


def _normalize_location_text(value: str | None) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    return text.lower().title()


def _location_is_in_india(location) -> bool:
    raw = getattr(location, "raw", {}) or {}
    address = raw.get("address", {}) or {}
    country = (address.get("country") or "").strip().lower()
    country_code = (address.get("country_code") or "").strip().lower()
    return country == "india" or country_code == "in"


def _location_matches_state(location, state: str | None) -> bool:
    if not state:
        return True

    expected_state = _normalize_location_text(state).lower()
    raw = getattr(location, "raw", {}) or {}
    address = raw.get("address", {}) or {}
    candidates = [
        address.get("state"),
        address.get("region"),
        address.get("union_territory"),
        location.address,
    ]
    for candidate in candidates:
        if candidate and expected_state in _normalize_location_text(candidate).lower():
            return True
    return False


def _paced_geocode(query: str):
    global _last_geocoder_call

    with _geocoder_lock:
        elapsed = time.monotonic() - _last_geocoder_call
        if elapsed < GEOCODER_DELAY_SECONDS:
            time.sleep(GEOCODER_DELAY_SECONDS - elapsed)

        location = GEOCODER.geocode(
            query,
            addressdetails=True,
            exactly_one=True,
            country_codes="in",
        )
        _last_geocoder_call = time.monotonic()
        return location


def _paced_reverse_geocode(lat: float, lon: float):
    global _last_geocoder_call

    with _geocoder_lock:
        elapsed = time.monotonic() - _last_geocoder_call
        if elapsed < GEOCODER_DELAY_SECONDS:
            time.sleep(GEOCODER_DELAY_SECONDS - elapsed)

        location = GEOCODER.reverse(
            (lat, lon),
            language="en",
            addressdetails=True,
            exactly_one=True,
            zoom=14,
            timeout=REVERSE_GEOCODER_TIMEOUT_SECONDS,
        )
        _last_geocoder_call = time.monotonic()
        return location


def _build_geocode_queries(locality: str, state: str | None) -> list[str]:
    queries = []
    if state:
        queries.append(f"{locality}, {state}, India")
    queries.extend([f"{locality}, India", locality])

    deduped = []
    seen = set()
    for query in queries:
        if query not in seen:
            seen.add(query)
            deduped.append(query)
    return deduped


@lru_cache(maxsize=1024)
def _geocode_location_cached(locality: str, state: str | None) -> dict | None:
    if not locality:
        return None

    for query in _build_geocode_queries(locality, state):
        for attempt in range(GEOCODER_MAX_RETRIES + 1):
            try:
                location = _paced_geocode(query)
            except (GeocoderTimedOut, GeocoderUnavailable, GeocoderServiceError) as exc:
                logger.warning(
                    "Geocoding attempt %d failed for '%s': %s",
                    attempt + 1,
                    query,
                    exc,
                )
                if attempt < GEOCODER_MAX_RETRIES:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                location = None

            if not location:
                break

            if not _location_is_in_india(location):
                logger.warning("Discarded non-India geocode result for '%s': %s", query, location.address)
                break

            if not _location_matches_state(location, state):
                logger.warning(
                    "Discarded state-mismatched geocode result for '%s': %s",
                    query,
                    location.address,
                )
                break

            raw = getattr(location, "raw", {}) or {}
            return {
                "address": location.address,
                "latitude": float(location.latitude),
                "longitude": float(location.longitude),
                "address_details": raw.get("address", {}) or {},
                "query_used": query,
                "is_fallback": False,
            }

    return None


def _first_address_value(address: dict, *keys: str) -> str | None:
    for key in keys:
        value = (address.get(key) or "").strip()
        if value:
            return value
    return None


def _serialize_reverse_location(location) -> dict:
    raw = getattr(location, "raw", {}) or {}
    address = raw.get("address", {}) or {}
    village = _first_address_value(
        address,
        "village",
        "hamlet",
        "isolated_dwelling",
        "suburb",
        "neighbourhood",
    )
    city = _first_address_value(
        address,
        "city",
        "town",
        "municipality",
        "county",
        "city_district",
        "state_district",
    )

    pieces = []
    if village:
        pieces.append(village)
    if city and city.lower() not in {piece.lower() for piece in pieces}:
        pieces.append(city)

    name = ", ".join(pieces) or _first_address_value(
        address,
        "locality",
        "region",
        "state_district",
        "state",
    ) or location.address

    return {
        "name": name,
        "city": city,
        "village": village,
        "address": location.address,
    }


@lru_cache(maxsize=2048)
def _reverse_geocode_cached(lat: float, lon: float) -> dict | None:
    for attempt in range(GEOCODER_MAX_RETRIES + 1):
        try:
            location = _paced_reverse_geocode(lat, lon)
        except (GeocoderTimedOut, GeocoderUnavailable, GeocoderServiceError) as exc:
            logger.warning(
                "Reverse geocoding attempt %d failed for (%s, %s): %s",
                attempt + 1,
                lat,
                lon,
                exc,
            )
            if attempt < GEOCODER_MAX_RETRIES:
                time.sleep(0.2 * (attempt + 1))
                continue
            return None
        except Exception as exc:
            logger.warning("Reverse geocoding error for (%s, %s): %s", lat, lon, exc)
            return None

        if not location or not _location_is_in_india(location):
            return None

        return _serialize_reverse_location(location)

    return None


def _resolve_click_location(lat: float, lon: float) -> dict | None:
    return _reverse_geocode_cached(round(lat, 5), round(lon, 5))


def _get_state_center_fallback(state: str | None) -> dict | None:
    if not state:
        return None

    try:
        bounds = boundary_repository.state_bounds(state)
        state_name = boundary_repository.state_name(state)
    except BoundaryLookupError:
        return None

    south_west, north_east = bounds
    latitude = round((south_west[0] + north_east[0]) / 2, 4)
    longitude = round((south_west[1] + north_east[1]) / 2, 4)

    return {
        "address": f"{state_name}, India",
        "latitude": latitude,
        "longitude": longitude,
        "address_details": {
            "state": state_name,
            "country": "India",
            "country_code": "in",
        },
        "query_used": None,
        "is_fallback": True,
    }


def _resolve_weather_location(locality: str, state: str | None = None) -> dict | None:
    normalized_locality = _normalize_location_text(locality)
    normalized_state = _normalize_location_text(state)

    location = _geocode_location_cached(normalized_locality, normalized_state or None)
    if location:
        location = dict(location)
        location["normalized_locality"] = normalized_locality
        location["normalized_state"] = normalized_state or None
        return location

    fallback = _get_state_center_fallback(normalized_state or state)
    if fallback:
        fallback["normalized_locality"] = normalized_locality
        fallback["normalized_state"] = normalized_state or None
        fallback["fallback_reason"] = (
            f"Geocoding failed for {normalized_locality}"
            + (f" in {normalized_state}" if normalized_state else "")
            + "; using state center."
        )
        return fallback

    return None


def _masked_image(band_names: list[str]) -> ee.Image:
    return ee.Image.constant([0] * len(band_names)).rename(band_names).selfMask()


def _require_ee_ready() -> None:
    global EE_READY

    if EE_READY:
        return

    try:
        EE_READY = ndvi_tile_service.initialize()
    except EarthEngineUnavailableError as exc:
        EE_READY = False
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not EE_READY:
        raise HTTPException(status_code=503, detail="Google Earth Engine is not initialized on this server.")


def get_all_gee_data(lat: float, lon: float, date_value: str) -> dict:
    _require_ee_ready()
    rounded_lat = round(lat, 4)
    rounded_lon = round(lon, 4)
    return _get_all_gee_data_cached(rounded_lat, rounded_lon, date_value)


@lru_cache(maxsize=500)
def _get_all_gee_data_cached(lat: float, lon: float, date_value: str) -> dict:
    _require_ee_ready()

    point = ee.Geometry.Point([lon, lat])
    date_obj = datetime.strptime(date_value, "%Y-%m-%d")
    start_ndvi = (date_obj - timedelta(days=5)).strftime("%Y-%m-%d")
    end_ndvi = (date_obj + timedelta(days=6)).strftime("%Y-%m-%d")
    next_day = (date_obj + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        s2 = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(point)
            .filterDate(start_ndvi, end_ndvi)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
        )
        chirps = (
            ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
            .filterBounds(point)
            .filterDate(date_value, next_day)
        )
        era5 = (
            ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR")
            .filterBounds(point)
            .filterDate(date_value, next_day)
        )

        combined = (
            ee.Image(
                ee.Algorithms.If(
                    s2.size().gt(0),
                    s2.median().normalizedDifference(["B8", "B4"]).rename("ndvi"),
                    _masked_image(["ndvi"]),
                )
            )
            .addBands(
                ee.Image(
                    ee.Algorithms.If(
                        chirps.size().gt(0),
                        ee.Image(chirps.first()).select(["precipitation"], ["rain"]),
                        _masked_image(["rain"]),
                    )
                )
            )
            .addBands(
                ee.Image(
                    ee.Algorithms.If(
                        era5.size().gt(0),
                        ee.Image(era5.first()).select(
                            [
                                "temperature_2m",
                                "temperature_2m_max",
                                "temperature_2m_min",
                            ]
                        ),
                        _masked_image(
                            [
                                "temperature_2m",
                                "temperature_2m_max",
                                "temperature_2m_min",
                            ]
                        ),
                    )
                )
            )
        )

        result = combined.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=point,
            scale=2000,
            bestEffort=True,
            tileScale=8,
        ).getInfo() or {}

        temperature_mean = result.get("temperature_2m")
        temperature_max = result.get("temperature_2m_max")
        temperature_min = result.get("temperature_2m_min")

        return {
            "ndvi": result.get("ndvi"),
            "rainfall": result.get("rain"),
            "temperature_mean": temperature_mean - 273.15 if temperature_mean is not None else None,
            "temperature_max": temperature_max - 273.15 if temperature_max is not None else None,
            "temperature_min": temperature_min - 273.15 if temperature_min is not None else None,
        }
    except Exception as exc:  # pragma: no cover - depends on GEE runtime
        logger.warning("Combined GEE fetch error for (%s, %s): %s", lat, lon, exc)
        raise HTTPException(status_code=503, detail=f"Earth Engine data request failed: {exc}") from exc


def get_ndvi_pixel_value(lat: float, lon: float, date_value: str, state_slug: str | None = None) -> float | None:
    _require_ee_ready()
    rounded_lat = round(lat, 6)
    rounded_lon = round(lon, 6)
    normalized_state = (state_slug or "").strip() or None
    return _get_ndvi_pixel_value_cached(rounded_lat, rounded_lon, date_value, normalized_state)


def _ndvi_date_window(date_value: str) -> tuple[str, str]:
    target_date = datetime.strptime(date_value, "%Y-%m-%d")
    start_date = (target_date - timedelta(days=30)).strftime("%Y-%m-%d")
    end_date = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
    return start_date, end_date


@lru_cache(maxsize=1024)
def _get_ndvi_pixel_value_cached(lat: float, lon: float, date_value: str, state_slug: str | None) -> float | None:
    _require_ee_ready()

    point = ee.Geometry.Point([lon, lat])
    start_date, end_date = _ndvi_date_window(date_value)

    try:
        search_geometry = point
        if state_slug:
            try:
                search_geometry = ee.Geometry(
                    json.loads(json.dumps(boundary_repository.state_geometry(state_slug).__geo_interface__))
                )
            except BoundaryLookupError:
                search_geometry = point

        s2 = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(search_geometry)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
        )

        ndvi_image = ee.Image(
            ee.Algorithms.If(
                s2.size().gt(0),
                s2.median().normalizedDifference(["B8", "B4"]).rename("ndvi").clip(search_geometry),
                _masked_image(["ndvi"]),
            )
        )

        sample_geometry = point.buffer(NDVI_PIXEL_BUFFER_METERS)
        pixel_sample = ndvi_image.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=sample_geometry,
            scale=10,
            bestEffort=True,
            maxPixels=1_000_000,
            tileScale=4,
        )

        ndvi_value = ee.Dictionary(pixel_sample).get("ndvi").getInfo()

        return float(ndvi_value) if ndvi_value is not None else None
    except Exception as exc:  # pragma: no cover - depends on GEE runtime
        logger.warning("NDVI pixel fetch error for (%s, %s): %s", lat, lon, exc)
        raise HTTPException(status_code=503, detail=f"NDVI pixel request failed: {exc}") from exc


def _resolve_ndvi_date(date_value: str | None) -> str:
    resolved_date = date_value or datetime.utcnow().strftime("%Y-%m-%d")

    try:
        datetime.strptime(resolved_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date must be provided as YYYY-MM-DD") from exc

    return resolved_date


def _weather_payload(location: dict, date_value: str) -> dict:
    all_data = get_all_gee_data(location["latitude"], location["longitude"], date_value)
    return {
        "rainfall": all_data.get("rainfall"),
        "temperature_max": all_data.get("temperature_max"),
        "temperature_min": all_data.get("temperature_min"),
        "temperature_mean": all_data.get("temperature_mean"),
        "ndvi": all_data.get("ndvi"),
    }


def _legacy_weather_response(
    locality: str,
    state: str | None,
    date_value: str,
    location: dict,
) -> dict:
    response = {
        "state": _normalize_location_text(state) if state else None,
        "district": _normalize_location_text(locality),
        "date": date_value,
        "location": location,
        "weather_data": _weather_payload(location, date_value),
    }
    if location.get("is_fallback"):
        response["message"] = location.get("fallback_reason")
    return response


@router.get("/", response_class=FileResponse)
async def index_page() -> FileResponse:
    return FileResponse(TEMPLATE_DIR / "index.html", media_type="text/html")


@router.get("/ndvi", response_class=FileResponse)
async def ndvi_map_page() -> FileResponse:
    return FileResponse(TEMPLATE_DIR / "ndvi_map.html", media_type="text/html")


@router.get("/ndvi-map", include_in_schema=False)
async def legacy_ndvi_map_page() -> RedirectResponse:
    return RedirectResponse(url="/ndvi", status_code=307)


@router.get("/api/states")
async def list_dashboard_states() -> JSONResponse:
    names = sorted(state["name"] for state in boundary_repository.list_states())
    return JSONResponse({"states": names}, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/api/ndvi/states")
async def list_states() -> JSONResponse:
    payload = {"states": boundary_repository.list_states()}
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/api/ndvi/boundaries/{level}")
async def get_boundaries(
    level: str,
    state: str = Query(..., min_length=2),
    bounds: str | None = Query(default=None),
    zoom: int = Query(default=6, ge=4, le=18),
) -> JSONResponse:
    try:
        payload = boundary_repository.boundary_collection(
            level=level,
            state_slug=state,
            bounds=_parse_bounds(bounds),
            zoom=zoom,
        )
    except BoundaryLookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=3600"})


def _ndvi_tile_json_response(state: str, date: str) -> JSONResponse:
    _require_ee_ready()

    try:
        payload = ndvi_tile_service.tile_payload(state, date)
    except EarthEngineUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except BoundaryLookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=43200"})


@router.get("/ndvi/{state}/{date}")
async def get_ndvi_tile(state: str, date: str) -> JSONResponse:
    return _ndvi_tile_json_response(state, date)


@router.post("/api/ndvi/tile-url")
async def get_ndvi_tile_url(request: NdviTileRequest) -> JSONResponse:
    return _ndvi_tile_json_response(request.state, request.date)


@router.get("/ndvi-value")
async def get_ndvi_value(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    state: str | None = Query(default=None),
 ) -> JSONResponse:
    date_value = _resolve_ndvi_date(date)
    ndvi_value = get_ndvi_pixel_value(lat, lon, date_value, state)
    location = _resolve_click_location(lat, lon)

    return JSONResponse({
        "latitude": round(lat, 6),
        "longitude": round(lon, 6),
        "date": date_value,
        "ndvi": round(float(ndvi_value), 4) if ndvi_value is not None else None,
        "location": location,
    }, headers={"Cache-Control": "public, max-age=600"})


@router.post("/api/v1/weather/get-data")
async def get_weather_data(data: WeatherRequest) -> dict:
    location = _resolve_weather_location(data.locality, data.state)
    if not location:
        raise HTTPException(status_code=404, detail="Location not found in India")

    all_data = get_all_gee_data(location["latitude"], location["longitude"], data.date)
    response = {
        "status": "success",
        "data": {
            "locality": _normalize_location_text(data.locality),
            "state": _normalize_location_text(data.state) if data.state else None,
            "date": data.date,
            "data_type": data.data_type,
            "location": location,
            "value": all_data.get(data.data_type),
        },
    }
    if location.get("is_fallback"):
        response["data"]["message"] = location.get("fallback_reason")
    return response


@router.post("/api/weather/get-data")
async def get_legacy_weather_data(data: LegacyWeatherRequest) -> JSONResponse:
    locality = data.locality or data.district
    if not locality:
        return JSONResponse({"status": "error", "message": "district is required"}, status_code=422)

    try:
        date_value = _resolve_ndvi_date(data.date)
        location = _resolve_weather_location(locality, data.state)
        if not location:
            return JSONResponse({"status": "error", "message": "Location not found in India"}, status_code=404)

        return JSONResponse({"status": "success", "data": _legacy_weather_response(locality, data.state, date_value, location)})
    except EarthEngineUnavailableError as exc:
        logger.error("Earth Engine unavailable for weather request: %s", exc)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=503)
    except HTTPException as exc:
        return JSONResponse({"status": "error", "message": str(exc.detail)}, status_code=exc.status_code)
    except Exception as exc:  # pragma: no cover - defensive compatibility path
        logger.error("Error processing legacy weather request: %s", exc, exc_info=True)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@router.post("/api/weather/get-data-range")
async def get_weather_data_range(data: LegacyWeatherRangeRequest) -> JSONResponse:
    try:
        from_date_obj = datetime.strptime(data.from_date, "%Y-%m-%d")
        to_date_obj = datetime.strptime(data.to_date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(
            {"status": "error", "message": "Dates must be provided as YYYY-MM-DD"},
            status_code=400,
        )

    min_date = datetime(2023, 1, 1).date()
    max_date = datetime.now().date()
    if from_date_obj.date() < min_date or to_date_obj.date() > max_date:
        return JSONResponse(
            {
                "status": "error",
                "message": f"Dates must be between {min_date.isoformat()} and {max_date.isoformat()}",
            },
            status_code=400,
        )
    if from_date_obj > to_date_obj:
        return JSONResponse(
            {"status": "error", "message": "From date must be before or equal to To date"},
            status_code=400,
        )

    try:
        location = _resolve_weather_location(data.district, data.state)
        if not location:
            return JSONResponse({"status": "error", "message": "Location not found in India"}, status_code=404)

        current_date = from_date_obj
        date_results = []
        while current_date <= to_date_obj:
            date_value = current_date.strftime("%Y-%m-%d")
            date_results.append(
                {
                    "date": date_value,
                    "weather_data": _weather_payload(location, date_value),
                }
            )
            current_date += timedelta(days=1)

        response_data = {
            "state": _normalize_location_text(data.state) if data.state else None,
            "district": _normalize_location_text(data.district),
            "from_date": data.from_date,
            "to_date": data.to_date,
            "location": location,
            "date_results": date_results,
        }
        if location.get("is_fallback"):
            response_data["message"] = location.get("fallback_reason")

        return JSONResponse({"status": "success", "data": response_data})
    except EarthEngineUnavailableError as exc:
        logger.error("Earth Engine unavailable for date range request: %s", exc)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=503)
    except HTTPException as exc:
        return JSONResponse({"status": "error", "message": str(exc.detail)}, status_code=exc.status_code)
    except Exception as exc:  # pragma: no cover - defensive compatibility path
        logger.error("Error processing date range request: %s", exc, exc_info=True)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@router.post("/api/v1/ndvi/get-rain-fall-stats")
async def get_rain_fall_stats(data: RainFallRequest) -> dict:
    location = _resolve_weather_location(data.locality, data.state)
    if not location:
        raise HTTPException(status_code=404, detail="Location not found in India")

    all_data = get_all_gee_data(location["latitude"], location["longitude"], data.date)
    response = {
        "status": "success",
        "data_received": {
            "locality": _normalize_location_text(data.locality),
            "state": _normalize_location_text(data.state) if data.state else None,
            "date": data.date,
            "location": location,
            "rainfall": all_data.get("rainfall"),
        },
    }
    if location.get("is_fallback"):
        response["data_received"]["message"] = location.get("fallback_reason")
    return response


@router.post("/api/rainfall/get-stats")
async def get_legacy_rainfall_stats(data: LegacyRainFallRequest) -> JSONResponse:
    try:
        locality = data.locality or data.district
        if not locality:
            return JSONResponse({"status": "error", "message": "district is required"}, status_code=422)

        location = _resolve_weather_location(locality, data.state)
        if not location:
            return JSONResponse({"status": "error", "message": "Location not found in India"}, status_code=404)

        response = {
            "status": "success",
            "data_received": {
                "locality": _normalize_location_text(locality),
                "date": data.date,
                "location": location,
                "rainfall": get_all_gee_data(location["latitude"], location["longitude"], data.date).get("rainfall"),
            },
        }
        if location.get("is_fallback"):
            response["data_received"]["message"] = location.get("fallback_reason")
        return JSONResponse(response)
    except EarthEngineUnavailableError as exc:
        logger.error("Earth Engine unavailable for legacy rainfall request: %s", exc)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=503)
    except HTTPException as exc:
        return JSONResponse({"status": "error", "message": str(exc.detail)}, status_code=exc.status_code)
    except Exception as exc:  # pragma: no cover - defensive compatibility path
        logger.error("Error processing legacy rainfall request: %s", exc, exc_info=True)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@router.get("/api/ndvi/available-dates")
async def available_dates() -> JSONResponse:
    return JSONResponse(
        {
            "status": "success",
            "message": "All dates from 2000-01-01 onwards are available via Earth Engine",
            "ranges": [],
        }
    )
