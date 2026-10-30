"""Descarga del conjunto mínimo de Anima para ilustrar historias."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from app.routes.models import scan_disk_models
from scripts.parallel_download import _status_path


class StoryImageModelManager:
    MANIFEST_REL = Path("install/manifest/manifest.story-images.json")
    DOWNLOADER_REL = Path("scripts/download_llm.py")

    def __init__(self, root: str | Path, config: Any, registry: Any) -> None:
        self.root = Path(root)
        self.config = config
        self.registry = registry
        self._lock = threading.RLock()
        self._state = "missing"
        self._detail = "Anima para ilustraciones · 7,29 GB · licencia no comercial"
        self._progress = 0.0
        self._worker: threading.Thread | None = None
        self._discover()

    def _entries(self) -> list[dict]:
        try:
            data = json.loads((self.root / self.MANIFEST_REL).read_text(encoding="utf-8"))
            return data.get("files", []) if isinstance(data, dict) else []
        except (OSError, ValueError, TypeError):
            return []

    def _files_complete(self) -> bool:
        entries = self._entries()
        return bool(entries) and all(
            (target := self.root / entry["target"] / entry["filename"]).is_file()
            and target.stat().st_size == int(entry["bytes"])
            for entry in entries
        )

    def _discover(self) -> None:
        if self._files_complete():
            scan_disk_models(self.config, self.registry)
            if not any(
                item.profile.unet_name.replace("\\", "/").lower()
                == "anima-2.9b-preview-v1.safetensors"
                for item in self.registry.models
            ):
                with self._lock:
                    self._state = "error"
                    self._detail = "Los archivos están descargados, pero Anima no se registró en el catálogo"
                return
            with self._lock:
                self._state = "ready"
                self._progress = 100.0
                self._detail = "Anima, codificador de texto y VAE listos para ilustrar"

    def status(self) -> dict[str, Any]:
        with self._lock:
            state, detail, progress = self._state, self._detail, self._progress
        entries = self._entries()
        total = sum(int(item.get("bytes", 0)) for item in entries)
        downloaded = 0
        for entry in entries:
            target = self.root / entry["target"] / entry["filename"]
            partial = target.with_name(target.name + ".part")
            received = 0
            try:
                received = partial.stat().st_size
            except OSError:
                pass
            try:
                transfer = json.loads(_status_path(partial).read_text(encoding="utf-8"))
                received = max(received, int(transfer.get("downloaded", 0)))
            except (OSError, ValueError, TypeError):
                pass
            if target.is_file():
                received = max(received, target.stat().st_size)
            downloaded += min(received, int(entry["bytes"]))
        if total and state in ("downloading", "error", "missing"):
            progress = min(100.0, downloaded * 100.0 / total)
        return {
            "state": state,
            "available": state == "ready",
            "progress": round(progress, 1),
            "downloaded": downloaded,
            "total": total,
            "detail": detail,
            "license": "Anima License v1.2 · uso no comercial",
        }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._state == "ready":
                return self.status()
            if self._worker is not None and self._worker.is_alive():
                return self.status()
            manifest = self.root / self.MANIFEST_REL
            downloader = self.root / self.DOWNLOADER_REL
            if not manifest.is_file() or not downloader.is_file():
                self._state = "error"
                self._detail = "Falta el manifiesto o el descargador de modelos"
                return self.status()
            self._state = "downloading"
            self._progress = 0.0
            self._detail = "Preparando descarga reanudable de Anima…"
            self._worker = threading.Thread(target=self._run, name="waifu-story-image-model", daemon=True)
            self._worker.start()
        return self.status()

    def _run(self) -> None:
        command = [
            sys.executable, str(self.root / self.DOWNLOADER_REL),
            "--manifest", str(self.root / self.MANIFEST_REL),
            "--root", str(self.root),
        ]
        try:
            process = subprocess.Popen(
                command, cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                with self._lock:
                    self._detail = line[-240:]
                    match = re.search(r"(\d+(?:\.\d+)?)%", line)
                    if match:
                        self._progress = float(match.group(1))
            code = process.wait()
            if code != 0:
                raise RuntimeError(f"El descargador terminó con código {code}; revisa Descargas")
            scan_disk_models(self.config, self.registry)
            if not any(
                item.profile.unet_name.replace("\\", "/").lower()
                == "anima-2.9b-preview-v1.safetensors"
                for item in self.registry.models
            ):
                raise RuntimeError("Anima se verificó, pero no pudo registrarse para generar imágenes")
            with self._lock:
                self._state = "ready"
                self._progress = 100.0
                self._detail = "Anima quedó verificado y registrado para las ilustraciones"
        except Exception as exc:
            with self._lock:
                self._state = "error"
                self._detail = str(exc)[:300]
