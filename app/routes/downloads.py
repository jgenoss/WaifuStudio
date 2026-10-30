"""Estado reciente de descargas de modelos y runtime."""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, Request

from app.config import APP_ROOT

router = APIRouter()
STATUS_DIR = APP_ROOT / "data" / "model-downloads"


@router.get("/api/model-downloads")
async def model_download_status(request: Request) -> dict:
    items: list[dict] = []
    if STATUS_DIR.is_dir():
        for path in STATUS_DIR.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(item, dict) or time.time() - float(item.get("updated_at", 0)) > 7 * 86400:
                    continue
                if item.get("state") in ("downloading", "retrying") and time.time() - float(item.get("updated_at", 0)) > 45:
                    item["state"] = "interrupted"
                    item["detail"] = "Sin actualizaciones recientes; la descarga pudo detenerse"
                items.append(item)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue

    manager = getattr(request.app.state, "story_model", None)
    if manager is not None:
        model = manager.status()
        model_file = "Qwen3.5-4B-Q4_K_M.gguf"
        if model.get("state") in ("downloading", "starting") and not any(item.get("file") == model_file for item in items):
            items.append({
                "id": "story-model", "file": model_file,
                "state": model["state"], "downloaded": None, "total": 2707513696,
                "percent": model.get("progress"), "workers": model.get("download_workers", 0),
                "speed_mbps": 0, "detail": model.get("detail", "Modelo local"),
                "updated_at": time.time(),
            })

    items.sort(key=lambda item: float(item.get("updated_at", 0)), reverse=True)
    return {"items": items[:30], "active": sum(item.get("state") in ("downloading", "starting", "retrying") for item in items)}
