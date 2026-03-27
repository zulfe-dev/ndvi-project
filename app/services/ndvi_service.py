from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from threading import Lock

import ee

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "india_boundary.geojson"
LEVEL_FILE_MAP = {
    "district": "india_district.geojson",
    "tehsil": "india_taluk.geojson",
}
LEVEL_NAME_FIELDS = {
    "district": "NAME_2",
    "tehsil": "NAME_3",
}
LEVEL_PARENT_FIELDS = {
    "district": None,
    "tehsil": "NAME_2",
}
STATE_NAME_FIELD = "NAME_1"
NDVI_VIS_PARAMS = {
    "min": 0.0,
    "max": 0.9,
    "palette": [
        "7f0000",
        "b30000",
        "d7301f",
        "fc8d59",
        "fee08b",
        "d9ef8b",
        "91cf60",
        "1a9850",
        "005a32",
    ],
}
STATE_ALIASES = {
    "andaman-and-nicobar-islands": {
        "andaman-and-nicobar-islands",
        "andaman-and-nicobar",
    },
    "chandigarh": {"chandigarh"},
    "dadra-and-nagar-haveli-and-daman-and-diu": {
        "dadra-and-nagar-haveli-and-daman-and-diu",
        "dadra-and-nagar-haveli",
        "daman-and-diu",
    },
    "delhi": {"delhi"},
    "odisha": {"odisha", "orissa"},
    "uttarakhand": {"uttarakhand", "uttaranchal"},
}
STATE_DISPLAY_NAMES = {
    "andaman-and-nicobar-islands": "Andaman and Nicobar Islands",
    "chandigarh": "Chandigarh",
    "dadra-and-nagar-haveli-and-daman-and-diu": "Dadra and Nagar Haveli and Daman and Diu",
    "delhi": "Delhi",
    "odisha": "Odisha",
    "uttarakhand": "Uttarakhand",
}
EE_PROJECT_ENV_VARS = ("EE_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT")
EE_PROJECT_PLACEHOLDERS = {
    "",
    "YOUR_GCP_PROJECT_ID",
    "YOUR_PROJECT_ID",
    "GCP_PROJECT_ID",
}


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


STATE_ALIAS_LOOKUP = {}
for canonical_key, aliases in STATE_ALIASES.items():
    for alias in aliases:
        STATE_ALIAS_LOOKUP[alias] = canonical_key


class BoundaryLookupError(ValueError):
    pass


class EarthEngineUnavailableError(RuntimeError):
    pass


def _validated_ee_project(candidate: str | None) -> str | None:
    value = (candidate or "").strip()
    if not value or value in EE_PROJECT_PLACEHOLDERS:
        return None
    return value


def configured_ee_project() -> str | None:
    for env_var in EE_PROJECT_ENV_VARS:
        project = _validated_ee_project(os.getenv(env_var))
        if project:
            return project

    oauth = getattr(ee, "oauth", None)
    if oauth and hasattr(oauth, "get_credentials_arguments"):
        try:
            args = oauth.get_credentials_arguments()
            project = _validated_ee_project(args.get("quota_project_id"))
            if project:
                return project
        except Exception:
            pass

    if oauth and hasattr(oauth, "get_appdefault_project"):
        try:
            project = _validated_ee_project(oauth.get_appdefault_project())
            if project:
                return project
        except Exception:
            pass

    return None


def earth_engine_credentials_path() -> str | None:
    oauth = getattr(ee, "oauth", None)
    if not oauth or not hasattr(oauth, "get_credentials_path"):
        return None

    try:
        return oauth.get_credentials_path()
    except Exception:  # pragma: no cover - depends on ee runtime internals
        return None


def earth_engine_credentials_access_issue(credentials_path: str | None) -> str | None:
    if not credentials_path:
        return None

    try:
        os.stat(credentials_path)
        return None
    except PermissionError as exc:
        return f" Credentials path is not readable: {credentials_path}. {exc}"
    except FileNotFoundError:
        return f" Credentials file was not found at {credentials_path}."
    except OSError as exc:
        return f" Credentials path access failed: {credentials_path}. {exc}"


def initialize_earth_engine() -> bool:
    credentials_path = earth_engine_credentials_path()
    credentials_issue = earth_engine_credentials_access_issue(credentials_path)

    try:
        logger.info(
            "Initializing Earth Engine without project (credentials_path=%s)",
            credentials_path,
        )
        ee.Initialize()
        logger.info("Earth Engine initialized successfully (no project)")
        return True
    except Exception as exc:  # pragma: no cover - depends on runtime auth
        credentials_note = (
            f" Credentials path: {credentials_path}."
            if credentials_path
            else ""
        )
        credentials_issue_note = credentials_issue or ""
        logger.exception("Earth Engine initialization failed: %s", exc)
        raise EarthEngineUnavailableError(
            "Earth Engine initialization failed."
            + credentials_note
            + credentials_issue_note
            + " Run `earthengine authenticate --force` and verify with "
            + "`python -c \"import ee; ee.Initialize(); print('OK')\"`."
            + f" Original error: {exc}"
        ) from exc


@dataclass(frozen=True)
class GeoJsonGeometry:
    geojson: dict[str, object]
    bounds: tuple[float, float, float, float]

    @property
    def __geo_interface__(self) -> dict[str, object]:
        return self.geojson


@dataclass(frozen=True)
class BoundaryFeature:
    name: str
    state_key: str
    parent_name: str | None
    geometry: GeoJsonGeometry
    bounds: tuple[float, float, float, float]


def _canonical_state_key(value: str) -> str:
    normalized = _slugify(value)
    return STATE_ALIAS_LOOKUP.get(normalized, normalized)


def _display_name_for_state_key(state_key: str) -> str:
    if state_key in STATE_DISPLAY_NAMES:
        return STATE_DISPLAY_NAMES[state_key]
    return state_key.replace("-", " ").title()


def _clone_json_value(value: object) -> object:
    return json.loads(json.dumps(value))


def _iter_coordinate_pairs(value: object):
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
            yield float(value[0]), float(value[1])
            return

        for item in value:
            yield from _iter_coordinate_pairs(item)


def _geometry_bounds(geometry: dict[str, object]) -> tuple[float, float, float, float]:
    points = list(_iter_coordinate_pairs(geometry.get("coordinates", [])))
    if not points:
        raise BoundaryLookupError("Boundary geometry is missing coordinates")

    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    return (
        min(longitudes),
        min(latitudes),
        max(longitudes),
        max(latitudes),
    )


def _geojson_geometry(raw_geometry: dict[str, object]) -> GeoJsonGeometry:
    geometry = _clone_json_value(raw_geometry)
    if not isinstance(geometry, dict):
        raise BoundaryLookupError("Boundary geometry is invalid")
    return GeoJsonGeometry(
        geojson=geometry,
        bounds=_geometry_bounds(geometry),
    )


def _round_nested(value: object, precision: int = 5) -> object:
    if isinstance(value, float):
        return round(value, precision)
    if isinstance(value, list):
        return [_round_nested(item, precision) for item in value]
    if isinstance(value, tuple):
        return [_round_nested(item, precision) for item in value]
    if isinstance(value, dict):
        return {key: _round_nested(item, precision) for key, item in value.items()}
    return value


def _bounds_intersect(
    feature_bounds: tuple[float, float, float, float],
    query_bounds: tuple[float, float, float, float],
) -> bool:
    min_x, min_y, max_x, max_y = feature_bounds
    q_min_x, q_min_y, q_max_x, q_max_y = query_bounds
    return not (
        max_x < q_min_x
        or min_x > q_max_x
        or max_y < q_min_y
        or min_y > q_max_y
    )


def _state_bounds_to_leaflet(bounds: tuple[float, float, float, float]) -> list[list[float]]:
    min_x, min_y, max_x, max_y = bounds
    return [[round(min_y, 5), round(min_x, 5)], [round(max_y, 5), round(max_x, 5)]]


class BoundaryRepository:
    def __init__(self, static_dir: Path):
        self.static_dir = Path(static_dir)
        self._lock = Lock()
        self._state_geometries: dict[str, GeoJsonGeometry] = {}
        self._state_names: dict[str, str] = {}
        self._state_slugs: dict[str, str] = {}
        self._level_features: dict[str, dict[str, list[BoundaryFeature]]] = {}
        self._state_seed_loaded = False

    def _read_geojson(self, file_name: str) -> dict:
        path = self.static_dir / file_name
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _load_state_seed_data(self) -> None:
        if self._state_seed_loaded:
            return

        with self._lock:
            if self._state_seed_loaded:
                return

            state_data = self._read_geojson(STATE_FILE_NAME)
            for feature in state_data.get("features", []):
                properties = feature.get("properties", {})
                state_name = properties.get("state") or properties.get("name")
                if not state_name:
                    continue

                state_key = _canonical_state_key(state_name)
                geometry_data = feature.get("geometry")
                if not geometry_data:
                    continue

                geometry = _geojson_geometry(geometry_data)
                self._state_names[state_key] = _display_name_for_state_key(state_key)
                self._state_slugs[state_key] = state_key
                self._state_geometries[state_key] = geometry

            self._state_seed_loaded = True

    def _load_level(self, level: str) -> None:
        if level in self._level_features:
            return

        if level not in LEVEL_FILE_MAP:
            raise BoundaryLookupError(f"Unsupported boundary level: {level}")

        self._load_state_seed_data()

        with self._lock:
            if level in self._level_features:
                return

            data = self._read_geojson(LEVEL_FILE_MAP[level])
            features_by_state: dict[str, list[BoundaryFeature]] = {}

            name_field = LEVEL_NAME_FIELDS[level]
            parent_field = LEVEL_PARENT_FIELDS[level]

            for feature in data.get("features", []):
                properties = feature.get("properties", {})
                raw_state_name = properties.get(STATE_NAME_FIELD)
                name = properties.get(name_field)
                if not raw_state_name or not name:
                    continue

                state_key = _canonical_state_key(raw_state_name)
                parent_name = properties.get(parent_field) if parent_field else None
                geometry_data = feature.get("geometry")
                if not geometry_data:
                    continue

                geometry = _geojson_geometry(geometry_data)
                boundary_feature = BoundaryFeature(
                    name=str(name),
                    state_key=state_key,
                    parent_name=str(parent_name) if parent_name else None,
                    geometry=geometry,
                    bounds=geometry.bounds,
                )

                features_by_state.setdefault(state_key, []).append(boundary_feature)
                self._state_names.setdefault(state_key, _display_name_for_state_key(state_key))
                self._state_slugs.setdefault(state_key, state_key)

            self._level_features[level] = features_by_state
            logger.info("Loaded %s boundary index for %d states", level, len(features_by_state))

    def warm(self) -> None:
        self._load_level("district")

    def _resolve_state_key(self, state_slug: str) -> str:
        self._load_level("district")
        state_key = _canonical_state_key(state_slug)
        if state_key not in self._state_geometries:
            raise BoundaryLookupError(f"Unknown state: {state_slug}")
        return state_key

    def list_states(self) -> list[dict]:
        self._load_level("district")
        states = []
        for state_key in sorted(self._state_geometries, key=lambda key: self._state_names[key]):
            geometry = self._state_geometries[state_key]
            states.append(
                {
                    "name": self._state_names[state_key],
                    "slug": self._state_slugs[state_key],
                    "bounds": _state_bounds_to_leaflet(geometry.bounds),
                }
            )
        return states

    def state_geometry(self, state_slug: str) -> GeoJsonGeometry:
        state_key = self._resolve_state_key(state_slug)
        return self._state_geometries[state_key]

    def state_name(self, state_slug: str) -> str:
        state_key = self._resolve_state_key(state_slug)
        return self._state_names[state_key]

    def state_bounds(self, state_slug: str) -> list[list[float]]:
        geometry = self.state_geometry(state_slug)
        return _state_bounds_to_leaflet(geometry.bounds)

    @staticmethod
    def _simplify_tolerance(level: str, zoom: int) -> float:
        zoom = max(4, min(14, int(zoom)))
        if level == "district":
            if zoom <= 5:
                return 0.03
            if zoom <= 7:
                return 0.012
            if zoom <= 9:
                return 0.006
            return 0.002

        if zoom <= 6:
            return 0.02
        if zoom <= 8:
            return 0.01
        if zoom <= 10:
            return 0.004
        return 0.0015

    @staticmethod
    def _bbox_cache_key(bounds: tuple[float, float, float, float] | None) -> str:
        if not bounds:
            return "full"
        return ",".join(f"{value:.2f}" for value in bounds)

    @staticmethod
    def _bbox_from_key(bounds_key: str) -> tuple[float, float, float, float] | None:
        if bounds_key == "full":
            return None
        values = tuple(float(value) for value in bounds_key.split(","))
        if len(values) != 4:
            raise BoundaryLookupError("Invalid bounds key")
        return values

    def boundary_collection(
        self,
        level: str,
        state_slug: str,
        bounds: tuple[float, float, float, float] | None = None,
        zoom: int = 6,
    ) -> dict:
        state_key = self._resolve_state_key(state_slug)
        bounds_key = self._bbox_cache_key(bounds)
        zoom_bucket = max(4, min(14, int(zoom)))
        return self._boundary_collection_cached(level, state_key, bounds_key, zoom_bucket)

    @lru_cache(maxsize=512)
    def _boundary_collection_cached(
        self,
        level: str,
        state_key: str,
        bounds_key: str,
        zoom: int,
    ) -> dict:
        self._load_level(level)
        features = self._level_features[level].get(state_key, [])
        query_bounds = self._bbox_from_key(bounds_key)

        filtered_features = features
        if query_bounds:
            filtered_features = [
                feature
                for feature in features
                if _bounds_intersect(feature.bounds, query_bounds)
            ]

        collection_features = []

        for feature in filtered_features:
            collection_features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "name": feature.name,
                        "state": self._state_names[state_key],
                        "parent": feature.parent_name,
                    },
                    "geometry": _round_nested(feature.geometry.__geo_interface__, precision=5),
                }
            )

        return {
            "type": "FeatureCollection",
            "features": collection_features,
            "metadata": {
                "state": self._state_names[state_key],
                "state_slug": self._state_slugs[state_key],
                "level": level,
                "feature_count": len(collection_features),
                "state_bounds": _state_bounds_to_leaflet(self._state_geometries[state_key].bounds),
            },
        }


class NdviTileService:
    def __init__(self, boundary_repository: BoundaryRepository):
        self.boundary_repository = boundary_repository
        self._ee_ready = False
        self._ee_lock = Lock()

    def initialize(self) -> bool:
        if self._ee_ready:
            return True

        with self._ee_lock:
            if self._ee_ready:
                return True

            self._ee_ready = initialize_earth_engine()

        return self._ee_ready

    def _require_ee(self) -> None:
        if not self.initialize():
            raise EarthEngineUnavailableError(
                "Google Earth Engine is not initialized on this server."
            )

    @staticmethod
    def _date_window(date_value: str) -> tuple[str, str]:
        target_date = datetime.strptime(date_value, "%Y-%m-%d")
        start_date = (target_date - timedelta(days=30)).strftime("%Y-%m-%d")
        end_date = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
        return start_date, end_date

    @lru_cache(maxsize=256)
    def tile_payload(self, state_slug: str, date_value: str) -> dict:
        self._require_ee()
        state_name = self.boundary_repository.state_name(state_slug)
        state_geometry = self.boundary_repository.state_geometry(state_slug)
        start_date, end_date = self._date_window(date_value)

        ee_geometry = ee.Geometry(_round_nested(state_geometry.__geo_interface__, precision=6))
        collection = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(ee_geometry)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
        )

        image_count = collection.limit(1).size().getInfo()
        if image_count == 0:
            raise BoundaryLookupError(
                f"No Sentinel-2 imagery found for {state_name} around {date_value}."
            )

        ndvi_image = (
            collection.median()
            .normalizedDifference(["B8", "B4"])
            .rename("NDVI")
            .clip(ee_geometry)
        )

        map_id = ndvi_image.getMapId(NDVI_VIS_PARAMS)
        tile_url = map_id["tile_fetcher"].url_format
        summary = (
            ndvi_image.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=ee_geometry,
                scale=2000,
                bestEffort=True,
                tileScale=8,
            ).getInfo()
            or {}
        )
        mean_ndvi = summary.get("NDVI")

        return {
            "state": state_name,
            "state_slug": _canonical_state_key(state_slug),
            "date": date_value,
            "composite_start": start_date,
            "composite_end": end_date,
            "tile_url": tile_url,
            "mean_ndvi": round(mean_ndvi, 4) if mean_ndvi is not None else None,
            "vis_params": NDVI_VIS_PARAMS,
            "state_bounds": self.boundary_repository.state_bounds(state_slug),
        }
