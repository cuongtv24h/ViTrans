"""ViSynth API (M1) — FastAPI + PostgreSQL."""

from __future__ import annotations

__version__ = "0.3.0"

from visynth_api.app import create_app  # noqa: E402  (định nghĩa __version__ trước để tránh vòng import)

__all__ = ["create_app", "__version__"]
