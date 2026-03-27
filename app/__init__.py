from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _load_flask_app():
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    spec = importlib.util.spec_from_file_location("flask_entrypoint", app_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Flask app from {app_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def __getattr__(name: str):
    if name == "app":
        return _load_flask_app()
    raise AttributeError(name)

__all__ = ["app"]
