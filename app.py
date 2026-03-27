from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from threading import Lock

import ee
from flask import Flask, jsonify, render_template, request
from geopy.exc import GeocoderServiceError, GeocoderTimedOut, GeocoderUnavailable
from geopy.geocoders import Nominatim
from app.services.ndvi_service import (
    BoundaryLookupError,
    BoundaryRepository,
    EarthEngineUnavailableError,
    NdviTileService,
    initialize_earth_engine,
)

# ---------------- CONFIG ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
GEOCODER_TIMEOUT_SECONDS = 5
GEOCODER_DELAY_SECONDS = 0.2
GEOCODER_MAX_RETRIES = 2
GEOCODER_USER_AGENT = "ndvi_flask_backend"
REVERSE_GEOCODER_TIMEOUT_SECONDS = 2
NDVI_PIXEL_BUFFER_METERS = 20

GEOCODER = Nominatim(
    user_agent=GEOCODER_USER_AGENT,
    timeout=GEOCODER_TIMEOUT_SECONDS,
)
_geocoder_lock = Lock()
_last_geocoder_call = 0.0

app = Flask(__name__)
boundary_repository = BoundaryRepository(STATIC_DIR)
ndvi_tile_service = NdviTileService(boundary_repository)

STATE_ALIASES = {
    "andaman-and-nicobar-islands": {
        "andaman-and-nicobar-islands",
        "andaman-and-nicobar",
    },
    "dadra-and-nagar-haveli-and-daman-and-diu": {
        "dadra-and-nagar-haveli-and-daman-and-diu",
        "dadra-and-nagar-haveli",
        "daman-and-diu",
    },
    "delhi": {"delhi", "nct-of-delhi", "national-capital-territory-of-delhi"},
    "odisha": {"odisha", "orissa"},
    "uttarakhand": {"uttarakhand", "uttaranchal"},
}

STATE_DISPLAY_NAMES = {
    "andaman-and-nicobar-islands": "Andaman and Nicobar Islands",
    "dadra-and-nagar-haveli-and-daman-and-diu": "Dadra and Nagar Haveli and Daman and Diu",
    "delhi": "Delhi",
    "odisha": "Odisha",
    "uttarakhand": "Uttarakhand",
}

STATE_CENTER_OVERRIDES = {
    "chandigarh": {
        "address": "Chandigarh, India",
        "latitude": 30.7333,
        "longitude": 76.7794,
    },
    "delhi": {
        "address": "Delhi, India",
        "latitude": 28.6139,
        "longitude": 77.2090,
    },
}

INDIA_STATES = [
    "Andhra Pradesh",
    "Arunachal Pradesh",
    "Assam",
    "Bihar",
    "Chhattisgarh",
    "Goa",
    "Gujarat",
    "Haryana",
    "Himachal Pradesh",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Punjab",
    "Rajasthan",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "Uttar Pradesh",
    "Uttarakhand",
    "West Bengal",
    "Andaman and Nicobar Islands",
    "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi",
    "Jammu and Kashmir",
    "Ladakh",
    "Lakshadweep",
    "Puducherry",
]

EE_AVAILABLE = False


def ensure_ee_available() -> bool:
    global EE_AVAILABLE

    if EE_AVAILABLE:
        return True

    try:
        initialize_earth_engine()
    except EarthEngineUnavailableError:
        EE_AVAILABLE = False
        raise

    EE_AVAILABLE = True
    return True


def normalize_location_text(value: str | None) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    return text.lower().title()


def slugify_text(value: str | None) -> str:
    text = (value or "").strip().lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def canonical_state_key(value: str | None) -> str:
    slug = slugify_text(value)
    for canonical, aliases in STATE_ALIASES.items():
        if slug in aliases:
            return canonical
    return slug


def display_state_name(value: str | None) -> str:
    state_key = canonical_state_key(value)
    if state_key in STATE_DISPLAY_NAMES:
        return STATE_DISPLAY_NAMES[state_key]
    normalized = normalize_location_text(value)
    return normalized or state_key.replace("-", " ").title()


def build_geocode_queries(district: str, state: str) -> list[str]:
    queries = [
        f"{district}, {state}, India",
        f"{district}, India",
        district,
    ]

    deduped = []
    seen = set()
    for query in queries:
        if query not in seen:
            seen.add(query)
            deduped.append(query)
    return deduped


def location_is_in_india(location) -> bool:
    raw = getattr(location, "raw", {}) or {}
    address = raw.get("address", {}) or {}
    country = (address.get("country") or "").strip().lower()
    country_code = (address.get("country_code") or "").strip().lower()
    return country == "india" or country_code == "in"


def location_matches_state(location, state: str) -> bool:
    expected_state_key = canonical_state_key(state)
    if not expected_state_key:
        return True

    raw = getattr(location, "raw", {}) or {}
    address = raw.get("address", {}) or {}
    candidates = [
        address.get("state"),
        address.get("region"),
        address.get("union_territory"),
        location.address,
    ]

    for candidate in candidates:
        if candidate and canonical_state_key(candidate) == expected_state_key:
            return True
    return False


def serialize_location(location, query: str) -> dict:
    raw = getattr(location, "raw", {}) or {}
    address = raw.get("address", {}) or {}
    return {
        "address": location.address,
        "latitude": float(location.latitude),
        "longitude": float(location.longitude),
        "address_details": address,
        "query_used": query,
        "is_fallback": False,
    }


def first_address_value(address: dict, *keys: str) -> str | None:
    for key in keys:
        value = (address.get(key) or "").strip()
        if value:
            return value
    return None


def serialize_reverse_location(location) -> dict:
    raw = getattr(location, "raw", {}) or {}
    address = raw.get("address", {}) or {}
    village = first_address_value(
        address,
        "village",
        "hamlet",
        "isolated_dwelling",
        "suburb",
        "neighbourhood",
    )
    city = first_address_value(
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

    name = ", ".join(pieces) or first_address_value(
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


def paced_geocode(query: str):
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


def paced_reverse_geocode(lat: float, lon: float):
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


@lru_cache(maxsize=1024)
def geocode_location_cached(district: str, state: str) -> dict | None:
    if not district:
        return None

    for query in build_geocode_queries(district, state):
        for attempt in range(GEOCODER_MAX_RETRIES + 1):
            try:
                location = paced_geocode(query)
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

            if not location_is_in_india(location):
                logger.warning("Discarded non-India geocode result for '%s': %s", query, location.address)
                break

            if not location_matches_state(location, state):
                logger.warning(
                    "Discarded state-mismatched geocode result for '%s': %s",
                    query,
                    location.address,
                )
                break

            return serialize_location(location, query)

    return None


@lru_cache(maxsize=2048)
def reverse_geocode_location_cached(lat: float, lon: float) -> dict | None:
    for attempt in range(GEOCODER_MAX_RETRIES + 1):
        try:
            location = paced_reverse_geocode(lat, lon)
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

        if not location or not location_is_in_india(location):
            return None

        return serialize_reverse_location(location)

    return None


def resolve_click_location(lat: float, lon: float) -> dict | None:
    return reverse_geocode_location_cached(round(lat, 5), round(lon, 5))


@lru_cache(maxsize=1)
def load_state_centers() -> dict[str, dict]:
    centers = dict(STATE_CENTER_OVERRIDES)
    state_file = STATIC_DIR / "india_boundary.geojson"

    if state_file.exists():
        with state_file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        for feature in payload.get("features", []):
            properties = feature.get("properties", {})
            state_name = properties.get("state") or properties.get("name")
            lat = properties.get("lat")
            lon = properties.get("lon")

            if not state_name or lat is None or lon is None:
                continue

            state_key = canonical_state_key(state_name)
            centers[state_key] = {
                "address": f"{display_state_name(state_name)}, India",
                "latitude": float(lat),
                "longitude": float(lon),
            }

    return centers


def get_state_center_fallback(state: str) -> dict | None:
    state_key = canonical_state_key(state)
    center = load_state_centers().get(state_key)
    if not center:
        return None

    return {
        "address": center["address"],
        "latitude": center["latitude"],
        "longitude": center["longitude"],
        "address_details": {
            "state": display_state_name(state),
            "country": "India",
            "country_code": "in",
        },
        "query_used": None,
        "is_fallback": True,
        "fallback_reason": f"Geocoding failed for {normalize_location_text(state)} request; using state center.",
    }


def resolve_location(district: str | None, state: str | None) -> dict | None:
    normalized_district = normalize_location_text(district)
    normalized_state = normalize_location_text(state)

    if not normalized_district:
        return None

    location = geocode_location_cached(normalized_district, normalized_state)
    if location:
        location = dict(location)
        location["normalized_district"] = normalized_district
        location["normalized_state"] = display_state_name(normalized_state)
        return location

    fallback = get_state_center_fallback(normalized_state)
    if fallback:
        fallback["fallback_reason"] = (
            f"Geocoding failed for {normalized_district} in {display_state_name(normalized_state)}; "
            "using state center."
        )
        fallback["normalized_district"] = normalized_district
        fallback["normalized_state"] = display_state_name(normalized_state)
        return fallback

    return None


@lru_cache(maxsize=500)
def get_all_gee_data(lat: float, lon: float, date_value: str) -> dict:
    """
    Get NDVI, rainfall, and temperature in a single Earth Engine response.

    The function keeps one client-side getInfo() round-trip and combines the
    three ERA5 temperature bands into a single reduceRegion call.
    """
    ensure_ee_available()

    try:
        point = ee.Geometry.Point([lon, lat])
        date_obj = datetime.strptime(date_value, "%Y-%m-%d")

        ndvi_start = (date_obj - timedelta(days=5)).strftime("%Y-%m-%d")
        ndvi_end = (date_obj + timedelta(days=6)).strftime("%Y-%m-%d")
        next_day = (date_obj + timedelta(days=1)).strftime("%Y-%m-%d")

        s2 = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(point)
            .filterDate(ndvi_start, ndvi_end)
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

        temperature_stats = ee.Dictionary(
            ee.Algorithms.If(
                era5.size().gt(0),
                ee.Image(era5.first())
                .select(
                    [
                        "temperature_2m",
                        "temperature_2m_max",
                        "temperature_2m_min",
                    ]
                )
                .reduceRegion(
                    reducer=ee.Reducer.mean(),
                    geometry=point,
                    scale=1000,
                    bestEffort=True,
                ),
                ee.Dictionary({}),
            )
        )

        result = (
            ee.Dictionary(
                {
                    "ndvi": ee.Algorithms.If(
                        s2.size().gt(0),
                        s2.median()
                        .normalizedDifference(["B8", "B4"])
                        .rename("ndvi")
                        .reduceRegion(
                            reducer=ee.Reducer.mean(),
                            geometry=point,
                            scale=10,
                            bestEffort=True,
                        )
                        .get("ndvi", None),
                        None,
                    ),
                    "rainfall": ee.Algorithms.If(
                        chirps.size().gt(0),
                        ee.Image(chirps.first())
                        .reduceRegion(
                            reducer=ee.Reducer.mean(),
                            geometry=point,
                            scale=5000,
                            bestEffort=True,
                        )
                        .get("precipitation", None),
                        None,
                    ),
                    "temperature_mean": temperature_stats.get("temperature_2m", None),
                    "temperature_max": temperature_stats.get("temperature_2m_max", None),
                    "temperature_min": temperature_stats.get("temperature_2m_min", None),
                }
            ).getInfo()
            or {}
        )

        temperature_mean = result.get("temperature_mean")
        temperature_max = result.get("temperature_max")
        temperature_min = result.get("temperature_min")
        rainfall = result.get("rainfall")

        if temperature_mean is not None:
            temperature_mean = temperature_mean - 273.15
        if temperature_max is not None:
            temperature_max = temperature_max - 273.15
        if temperature_min is not None:
            temperature_min = temperature_min - 273.15

        if temperature_mean is None:
            temperature_mean = 0
        if temperature_max is None:
            temperature_max = 0
        if temperature_min is None:
            temperature_min = 0
        if rainfall is None:
            rainfall = 0

        return {
            "ndvi": result.get("ndvi"),
            "rainfall": round(rainfall, 2),
            "temperature_mean": round(temperature_mean, 2),
            "temperature_max": round(temperature_max, 2),
            "temperature_min": round(temperature_min, 2),
        }
    except Exception as exc:
        logger.error("GEE fetch error for (%s, %s) on %s: %s", lat, lon, date_value, exc)
        raise EarthEngineUnavailableError(
            f"Earth Engine data request failed for {date_value}: {exc}"
        ) from exc


def ndvi_date_window(date_value: str) -> tuple[str, str]:
    target_date = datetime.strptime(date_value, "%Y-%m-%d")
    start_date = (target_date - timedelta(days=30)).strftime("%Y-%m-%d")
    end_date = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
    return start_date, end_date


def get_ndvi_pixel_value(lat: float, lon: float, date_value: str, state_slug: str | None = None) -> float | None:
    rounded_lat = round(lat, 6)
    rounded_lon = round(lon, 6)
    normalized_state = (state_slug or "").strip() or None
    return _get_ndvi_pixel_value_cached(rounded_lat, rounded_lon, date_value, normalized_state)


@lru_cache(maxsize=1024)
def _get_ndvi_pixel_value_cached(lat: float, lon: float, date_value: str, state_slug: str | None) -> float | None:
    ensure_ee_available()

    try:
        point = ee.Geometry.Point([lon, lat])
        start_date, end_date = ndvi_date_window(date_value)

        search_geometry = point
        if state_slug:
            try:
                search_geometry = ee.Geometry(
                    json.loads(
                        json.dumps(
                            boundary_repository.state_geometry(state_slug).__geo_interface__,
                            default=float,
                        )
                    )
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
                ee.Image.constant(0).rename("ndvi").selfMask(),
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
    except Exception as exc:
        logger.error("NDVI pixel fetch error for (%s, %s) on %s: %s", lat, lon, date_value, exc)
        raise


def resolve_ndvi_date(date_value: str | None) -> str:
    resolved_date = (date_value or "").strip() or datetime.utcnow().strftime("%Y-%m-%d")

    try:
        datetime.strptime(resolved_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date must be provided as YYYY-MM-DD") from exc

    return resolved_date


@lru_cache(maxsize=2)
def get_features_by_state(level: str) -> dict[str, list[dict]]:
    geojson_path = (
        STATIC_DIR / "india_district.geojson"
        if level == "district"
        else STATIC_DIR / "india_taluk.geojson"
    )

    with geojson_path.open("r", encoding="utf-8") as handle:
        geojson = json.load(handle)

    features_by_state: dict[str, list[dict]] = {}
    for feature in geojson.get("features", []):
        state_name = feature.get("properties", {}).get("NAME_1")
        if not state_name:
            continue
        state_key = canonical_state_key(state_name)
        features_by_state.setdefault(state_key, []).append(feature)

    logger.info("Loaded %s GeoJSON index for %d states", level, len(features_by_state))
    return features_by_state


def build_weather_response(state: str, district: str, date_value: str, location: dict, weather_data: dict) -> dict:
    response = {
        "state": display_state_name(state),
        "district": normalize_location_text(district),
        "date": date_value,
        "location": location,
        "weather_data": weather_data,
    }
    if location.get("is_fallback"):
        response["message"] = location.get("fallback_reason")
    return response


def parse_bounds_param(raw_bounds: str | None) -> tuple[float, float, float, float] | None:
    if not raw_bounds:
        return None

    parts = raw_bounds.split(",")
    if len(parts) != 4:
        raise ValueError("bounds must be provided as west,south,east,north")

    try:
        return tuple(float(part) for part in parts)
    except ValueError as exc:
        raise ValueError("bounds must contain numbers") from exc


# ================== ROUTES ==================
@app.route("/api/states")
def get_states():
    return jsonify({"states": sorted(INDIA_STATES)})


@app.route("/api/ndvi/states")
def get_ndvi_states():
    return jsonify({"states": boundary_repository.list_states()})


@app.route("/api/ndvi/boundaries/<level>")
def get_ndvi_boundaries(level: str):
    state = request.args.get("state")
    raw_bounds = request.args.get("bounds")
    raw_zoom = request.args.get("zoom", "6")

    if not state:
        return jsonify({"detail": "state is required"}), 422

    try:
        zoom = int(raw_zoom)
    except ValueError:
        return jsonify({"detail": "zoom must be an integer"}), 422

    try:
        payload = boundary_repository.boundary_collection(
            level=level,
            state_slug=state,
            bounds=parse_bounds_param(raw_bounds),
            zoom=zoom,
        )
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 422
    except BoundaryLookupError as exc:
        return jsonify({"detail": str(exc)}), 404

    response = jsonify(payload)
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.route("/api/ndvi/tile-url", methods=["POST"])
def get_ndvi_tile_url():
    payload = request.get_json(silent=True) or {}
    state = payload.get("state")
    date_value = payload.get("date")

    if not state or len(str(state).strip()) < 2:
        return jsonify({"detail": "state is required"}), 422
    if not date_value:
        return jsonify({"detail": "date is required"}), 422

    try:
        tile_payload = ndvi_tile_service.tile_payload(str(state).strip(), str(date_value).strip())
    except EarthEngineUnavailableError as exc:
        return jsonify({"detail": str(exc)}), 503
    except BoundaryLookupError as exc:
        return jsonify({"detail": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 422

    response = jsonify(tile_payload)
    response.headers["Cache-Control"] = "public, max-age=43200"
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/weather/get-data", methods=["POST"])
def get_weather_data():
    try:
        data = request.get_json(silent=True) or {}
        state = data.get("state")
        district = data.get("district")
        date_value = data.get("date")

        logger.info(
            "Received request for state=%s district=%s date=%s",
            state,
            district,
            date_value,
        )

        location = resolve_location(district, state)
        if not location:
            return jsonify({"status": "error", "message": "Location not found in India"}), 404

        all_data = get_all_gee_data(location["latitude"], location["longitude"], date_value)
        weather_data = {
            "rainfall": all_data.get("rainfall"),
            "temperature_max": all_data.get("temperature_max"),
            "temperature_min": all_data.get("temperature_min"),
            "temperature_mean": all_data.get("temperature_mean"),
            "ndvi": all_data.get("ndvi"),
        }

        return jsonify(
            {
                "status": "success",
                "data": build_weather_response(state, district, date_value, location, weather_data),
            }
        )
    except EarthEngineUnavailableError as exc:
        logger.error("Earth Engine unavailable for weather request: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 503
    except Exception as exc:
        logger.error("Error processing request: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/weather/get-data-range", methods=["POST"])
def get_weather_data_range():
    try:
        data = request.get_json(silent=True) or {}
        state = data.get("state")
        district = data.get("district")
        from_date = data.get("from_date")
        to_date = data.get("to_date")

        logger.info(
            "Received date range request for state=%s district=%s from=%s to=%s",
            state,
            district,
            from_date,
            to_date,
        )

        from_date_obj = datetime.strptime(from_date, "%Y-%m-%d")
        to_date_obj = datetime.strptime(to_date, "%Y-%m-%d")

        min_date = datetime(2023, 1, 1).date()
        max_date = datetime.now().date()

        if from_date_obj.date() < min_date or to_date_obj.date() > max_date:
            return jsonify(
                {
                    "status": "error",
                    "message": f"Dates must be between {min_date.isoformat()} and {max_date.isoformat()}",
                }
            ), 400
        if from_date_obj > to_date_obj:
            return jsonify({"status": "error", "message": "From date must be before or equal to To date"}), 400

        location = resolve_location(district, state)
        if not location:
            return jsonify({"status": "error", "message": "Location not found in India"}), 404

        current_date = from_date_obj
        date_results = []
        while current_date <= to_date_obj:
            date_str = current_date.strftime("%Y-%m-%d")
            all_data = get_all_gee_data(location["latitude"], location["longitude"], date_str)
            date_results.append(
                {
                    "date": date_str,
                    "weather_data": {
                        "rainfall": all_data.get("rainfall"),
                        "temperature_max": all_data.get("temperature_max"),
                        "temperature_min": all_data.get("temperature_min"),
                        "temperature_mean": all_data.get("temperature_mean"),
                        "ndvi": all_data.get("ndvi"),
                    },
                }
            )
            current_date += timedelta(days=1)

        response_data = {
            "state": display_state_name(state),
            "district": normalize_location_text(district),
            "from_date": from_date,
            "to_date": to_date,
            "location": location,
            "date_results": date_results,
        }
        if location.get("is_fallback"):
            response_data["message"] = location.get("fallback_reason")

        return jsonify({"status": "success", "data": response_data})
    except EarthEngineUnavailableError as exc:
        logger.error("Earth Engine unavailable for date range request: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 503
    except Exception as exc:
        logger.error("Error processing date range request: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/ndvi-map")
def ndvi_map():
    return render_template("ndvi_map.html")


@app.route("/ndvi-value")
def ndvi_value():
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"message": "lat and lon must be valid numbers"}), 400

    try:
        date_value = resolve_ndvi_date(request.args.get("date"))
        ndvi_value = get_ndvi_pixel_value(lat, lon, date_value, request.args.get("state"))
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except EarthEngineUnavailableError as exc:
        logger.error("Earth Engine unavailable for NDVI value request: %s", exc)
        return jsonify({"message": str(exc)}), 503
    except Exception as exc:
        logger.error("Error fetching NDVI value for map click: %s", exc, exc_info=True)
        return jsonify({"message": str(exc)}), 500

    location = resolve_click_location(lat, lon)
    response = jsonify(
        {
            "latitude": round(lat, 6),
            "longitude": round(lon, 6),
            "date": date_value,
            "ndvi": round(float(ndvi_value), 4) if ndvi_value is not None else None,
            "location": location,
        }
    )
    response.headers["Cache-Control"] = "public, max-age=600"
    return response


@app.route("/api/ndvi/regional-analysis", methods=["POST"])
def regional_ndvi():
    """
    Regional NDVI analysis for districts or taluks using one cached GeoJSON index
    and one Earth Engine reduceRegions batch call.
    """
    try:
        payload = request.get_json(silent=True) or {}
        level = payload["level"]
        state = payload.get("state")
        date_value = payload["date"]

        if level not in {"district", "tehsil"}:
            return jsonify({"status": "error", "message": "Invalid level"}), 400
        if not state:
            return jsonify({"status": "error", "message": "Please select a state first"}), 400
        ensure_ee_available()

        features = get_features_by_state(level).get(canonical_state_key(state), [])
        if not features:
            logger.warning("No regions found for state: %s", state)
            return jsonify({"status": "success", "data": {"regions": []}})

        feature_collection = ee.FeatureCollection(features)
        date_obj = datetime.strptime(date_value, "%Y-%m-%d")
        start_date = (date_obj - timedelta(days=5)).strftime("%Y-%m-%d")
        end_date = (date_obj + timedelta(days=6)).strftime("%Y-%m-%d")

        ndvi_image = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
            .median()
            .normalizedDifference(["B8", "B4"])
        )

        logger.info(
            "Computing NDVI for %d %s regions in %s",
            len(features),
            level,
            state,
        )

        result = ndvi_image.reduceRegions(
            collection=feature_collection,
            reducer=ee.Reducer.mean(),
            scale=100,
            tileScale=4,
        ).getInfo()

        regions = []
        for feature in result.get("features", []):
            properties = feature.get("properties", {})
            name = properties.get("NAME_2") if level == "district" else properties.get("NAME_3")
            ndvi_value = properties.get("mean")
            regions.append(
                {
                    "name": name,
                    "state": properties.get("NAME_1"),
                    "ndvi": round(ndvi_value, 3) if ndvi_value is not None else None,
                }
            )

        return jsonify({"status": "success", "data": {"regions": regions}})
    except EarthEngineUnavailableError as exc:
        logger.error("Earth Engine unavailable for regional NDVI analysis: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 503
    except Exception as exc:
        logger.error("Error in regional NDVI analysis: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/ndvi/available-dates")
def available_dates():
    return jsonify(
        {
            "status": "success",
            "message": "All dates from 2000-01-01 onwards are available via Earth Engine",
            "ranges": [],
        }
    )


def get_major_districts_for_state(state):
    major_districts = {
        "Maharashtra": ["Mumbai", "Pune", "Nagpur", "Nashik", "Aurangabad", "Solapur", "Kolhapur", "Sangli", "Satara", "Ahmednagar"],
        "Karnataka": ["Bangalore", "Mysore", "Hubli", "Mangalore", "Belgaum", "Gulbarga", "Davangere", "Bellary", "Bijapur", "Shimoga"],
        "Tamil Nadu": ["Chennai", "Coimbatore", "Madurai", "Tiruchirappalli", "Salem", "Tirunelveli", "Erode", "Vellore", "Thoothukudi", "Dindigul"],
        "Gujarat": ["Ahmedabad", "Surat", "Vadodara", "Rajkot", "Bhavnagar", "Jamnagar", "Junagadh", "Gandhinagar", "Anand", "Bharuch"],
        "Rajasthan": ["Jaipur", "Jodhpur", "Kota", "Bikaner", "Ajmer", "Udaipur", "Bhilwara", "Alwar", "Bharatpur", "Sikar"],
        "Uttar Pradesh": ["Lucknow", "Kanpur", "Ghaziabad", "Agra", "Meerut", "Varanasi", "Allahabad", "Bareilly", "Aligarh", "Moradabad"],
        "West Bengal": ["Kolkata", "Howrah", "Durgapur", "Asansol", "Siliguri", "Malda", "Bardhaman", "Kharagpur", "Haldia", "Krishnanagar"],
        "Madhya Pradesh": ["Bhopal", "Indore", "Gwalior", "Jabalpur", "Ujjain", "Sagar", "Dewas", "Satna", "Ratlam", "Rewa"],
        "Andhra Pradesh": ["Hyderabad", "Visakhapatnam", "Vijayawada", "Guntur", "Nellore", "Kurnool", "Rajahmundry", "Tirupati", "Kakinada", "Anantapur"],
        "Telangana": ["Hyderabad", "Warangal", "Nizamabad", "Khammam", "Karimnagar", "Ramagundam", "Mahbubnagar", "Nalgonda", "Adilabad", "Suryapet"],
        "Kerala": ["Thiruvananthapuram", "Kochi", "Kozhikode", "Thrissur", "Kollam", "Palakkad", "Alappuzha", "Malappuram", "Kannur", "Kasaragod"],
        "Punjab": ["Ludhiana", "Amritsar", "Jalandhar", "Patiala", "Bathinda", "Mohali", "Firozpur", "Hoshiarpur", "Batala", "Pathankot"],
        "Haryana": ["Gurgaon", "Faridabad", "Panipat", "Ambala", "Yamunanagar", "Rohtak", "Hisar", "Karnal", "Sonipat", "Panchkula"],
        "Bihar": ["Patna", "Gaya", "Bhagalpur", "Muzaffarpur", "Purnia", "Darbhanga", "Bihar Sharif", "Arrah", "Begusarai", "Katihar"],
        "Odisha": ["Bhubaneswar", "Cuttack", "Rourkela", "Brahmapur", "Sambalpur", "Puri", "Balasore", "Bhadrak", "Baripada", "Jharsuguda"],
        "Assam": ["Guwahati", "Silchar", "Dibrugarh", "Jorhat", "Nagaon", "Tinsukia", "Tezpur", "Bongaigaon", "Karimganj", "Sivasagar"],
    }
    return major_districts.get(state, [state.split()[0]])


def get_districts_for_state(state):
    major_districts = get_major_districts_for_state(state)
    additional_districts = {
        "Maharashtra": ["Thane", "Raigad", "Ratnagiri", "Sindhudurg", "Dhule", "Jalgaon", "Buldhana", "Akola", "Washim", "Amravati"],
        "Karnataka": ["Tumkur", "Hassan", "Mandya", "Chitradurga", "Kolar", "Chikmagalur", "Kodagu", "Dakshina Kannada", "Udupi", "Uttara Kannada"],
        "Tamil Nadu": ["Kanchipuram", "Tiruvallur", "Cuddalore", "Villupuram", "Dharmapuri", "Krishnagiri", "Namakkal", "Karur", "Perambalur", "Ariyalur"],
        "Gujarat": ["Mehsana", "Patan", "Banaskantha", "Sabarkantha", "Kheda", "Panchmahals", "Dahod", "Valsad", "Navsari", "Tapi"],
        "Rajasthan": ["Tonk", "Bundi", "Jhalawar", "Banswara", "Dungarpur", "Chittorgarh", "Rajsamand", "Pali", "Sirohi", "Jalore"],
    }
    extended_list = major_districts + additional_districts.get(state, [])
    return list(set(extended_list))


@app.route("/api/rainfall/get-stats", methods=["POST"])
def get_rainfall_stats():
    try:
        data = request.get_json(silent=True) or {}
        locality = data.get("locality") or data.get("district")
        state = data.get("state", "Maharashtra")
        date_value = data.get("date")

        logger.info("Received legacy request for locality=%s state=%s date=%s", locality, state, date_value)

        location = resolve_location(locality, state)
        if not location:
            return jsonify({"status": "error", "message": "Location not found in India"}), 404

        all_data = get_all_gee_data(location["latitude"], location["longitude"], date_value)
        response_payload = {
            "locality": normalize_location_text(locality),
            "date": date_value,
            "location": location,
            "rainfall": all_data.get("rainfall"),
        }
        if location.get("is_fallback"):
            response_payload["message"] = location.get("fallback_reason")

        return jsonify({"status": "success", "data_received": response_payload})
    except EarthEngineUnavailableError as exc:
        logger.error("Earth Engine unavailable for legacy rainfall request: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 503
    except Exception as exc:
        logger.error("Error processing legacy request: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": str(exc)}), 500


if __name__ == "__main__":
    initialize_earth_engine()
    app.run(debug=True, host="0.0.0.0", port=8000)
