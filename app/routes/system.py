"""Rutas del sistema y UI web (GET /).

Qué hace: sirve la plantilla principal HTML de la interfaz (templates/index.html).
Qué no hace: no procesa generación de medios ni lógica de catálogos (la salud del sistema opera vía CLI python -m app.health).
Dependencias: Jinja2Templates, app.config.APP_ROOT.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.request import urlopen

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import APP_ROOT

TEMPLATES_DIR = APP_ROOT / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Any:
    return templates.TemplateResponse(request, "index.html", {"title": "WAIFU"})


@router.get("/api/system/readiness")
async def system_readiness(request: Request) -> dict[str, Any]:
    """Return a compact readiness summary for the web UI."""
    config = request.app.state.config
    engine = False
    try:
        with urlopen(f"{str(config.comfy_url).rstrip('/')}/queue", timeout=1.5) as response:
            engine = response.status == 200 and isinstance(json.loads(response.read()), dict)
    except Exception:
        pass
    external_story = bool(os.environ.get("WAIFU_STORY_LLM_URL", "").strip())
    try:
        conversation = external_story or bool(request.app.state.story_model.status().get("available"))
    except Exception:
        conversation = False
    try:
        illustration_model = request.app.state.story_image_model.status().get("state") == "ready"
        image_registry = any(entry.id == "anima-2.9b-preview-v1" for entry in request.app.state.registry.models)
    except Exception:
        illustration_model = image_registry = False
    components = {
        "web": True,
        "engine": engine,
        "conversation": conversation,
        "illustration_model": illustration_model and image_registry,
    }
    return {"ready": all(components.values()), "components": components}
