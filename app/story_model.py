"""Descarga verificada y servidor local del modelo conversacional de Historias."""

from __future__ import annotations

import re
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from app.engine import EngineError
from app.enhancer import load_server_llm


class StoryModelManager:
    MODEL_ID = "waifu-story-qwen35-4b"
    PORT = 8291
    MODEL_REL = Path("ComfyUI/models/llm/story-qwen35-4b/Qwen3.5-4B-Q4_K_M.gguf")
    SERVER_DIR_REL = Path("tools/llama.cpp")
    MANIFEST_REL = Path("install/manifest/manifest.story.json")

    def __init__(self, root: str | Path, *, threads: int = 4) -> None:
        self.root = Path(root)
        self.threads = max(1, min(12, int(threads)))
        try:
            download_workers = int(os.environ.get("WAIFU_DOWNLOAD_WORKERS", "8"))
        except ValueError:
            download_workers = 8
        self.download_workers = max(1, min(16, download_workers))
        self.base_url = f"http://127.0.0.1:{self.PORT}"
        self._lock = threading.RLock()
        self._state = "stopped"
        self._detail = (
            "Modelo listo para iniciar" if self._files_ready()
            else f"Descarga requerida · 2,71 GB · acelerador: {self.download_workers} conexiones"
        )
        self._progress = 0.0
        self._process: subprocess.Popen | None = None
        self._download_process: subprocess.Popen | None = None
        self._worker: threading.Thread | None = None
        self._client = load_server_llm(
            self.base_url, model=self.MODEL_ID, max_tokens=500, temperature=0.85
        )

    def _files_ready(self) -> bool:
        return (self.root / self.MODEL_REL).is_file() and self._server_path() is not None

    def _server_path(self) -> Path | None:
        server_dir = self.root / self.SERVER_DIR_REL
        direct = server_dir / "llama-server.exe"
        if direct.is_file():
            return direct
        if server_dir.is_dir():
            return next(server_dir.rglob("llama-server.exe"), None)
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            state = self._state
            if state == "stopped" and not self._files_ready():
                state = "missing"
            if state == "running" and process is not None and process.poll() is not None:
                self._state = state = "error"
                self._detail = "El servidor del modelo se cerró; revisa data/story-llm.log"
            return {
                "state": state,
                "available": state == "running",
                "files_ready": self._files_ready(),
                "progress": self._progress,
                "detail": self._detail,
                "model": self.MODEL_ID,
                "size": "2,71 GB",
                "threads": self.threads,
                "download_workers": self.download_workers,
                "context": 4096,
            }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._state in ("downloading", "starting", "running"):
                return self.status()
            self._state = "starting" if self._files_ready() else "downloading"
            self._progress = 0.0
            self._detail = "Iniciando llama.cpp…" if self._files_ready() else "Preparando descarga verificada…"
            self._worker = threading.Thread(target=self._run, name="waifu-story-model", daemon=True)
            self._worker.start()
            return self.status()

    def _run(self) -> None:
        try:
            if not self._files_ready():
                self._download()
            with self._lock:
                if self._state == "stopped":
                    return
            self._start_server()
        except Exception as exc:
            with self._lock:
                if self._state != "stopped":
                    self._state = "error"
                    self._detail = str(exc)[:500]

    def _download(self) -> None:
        script = self.root / "scripts/download_llm.py"
        manifest = self.root / self.MANIFEST_REL
        if not script.is_file() or not manifest.is_file():
            raise EngineError("No se encontró el descargador o el manifiesto del modelo")
        command = [sys.executable, str(script), "--manifest", str(manifest), "--root", str(self.root), "--workers", str(self.download_workers)]
        process = subprocess.Popen(
            command, cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self._lock:
            self._download_process = process
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            with self._lock:
                self._detail = line[-240:]
                if "descargando" in line.lower():
                    self._progress = 0.0
                match = re.search(r"(\d+(?:\.\d+)?)%", line)
                if match:
                    self._progress = float(match.group(1))
        code = process.wait()
        with self._lock:
            if self._download_process is process:
                self._download_process = None
            if self._state == "stopped":
                return
        if code != 0:
            raise EngineError(f"Descarga o SHA-256 falló (código {code}); revisa el estado del modelo")
        with self._lock:
            self._progress = 100.0
            self._state = "starting"
            self._detail = "Descarga verificada; iniciando el modelo…"

    def _start_server(self) -> None:
        server = self._server_path()
        model = self.root / self.MODEL_REL
        if server is None or not model.is_file():
            raise EngineError("Faltan archivos del servidor o modelo después de la descarga")
        with self._lock:
            if self._state == "stopped":
                return
            self._stop_port_owner_if_ours()
            log_path = self.root / "data/story-llm.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            command = [
                str(server), "-m", str(model), "--alias", self.MODEL_ID,
                "-ngl", "0", "-c", "4096", "-t", str(self.threads),
                "--jinja", "--reasoning", "off", "--no-webui",
                "--host", "127.0.0.1", "--port", str(self.PORT),
            ]
            log = log_path.open("a", encoding="utf-8")
            try:
                process = subprocess.Popen(
                    command, cwd=str(self.root), stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            finally:
                log.close()
            self._process = process
            self._state = "starting"
            self._detail = "Cargando Qwen3.5-4B en CPU…"
        for _ in range(120):
            with self._lock:
                if self._state == "stopped":
                    return
            if process.poll() is not None:
                raise EngineError("llama-server se cerró; revisa data/story-llm.log")
            try:
                with urllib.request.urlopen(f"{self.base_url}/v1/models", timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    model_ids = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict)]
                    if response.status == 200 and self.MODEL_ID in model_ids:
                        with self._lock:
                            if self._state == "stopped":
                                return
                            self._state = "running"
                            self._detail = "Modelo listo · CPU · 4K de contexto"
                        return
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, AttributeError):
                pass
            time.sleep(1)
        raise EngineError("El modelo tardó demasiado en arrancar; revisa data/story-llm.log")

    def _stop_port_owner_if_ours(self) -> None:
        # No intenta terminar un servicio que no creó esta instancia.
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    def stop(self) -> None:
        with self._lock:
            process = self._process
            download_process = self._download_process
            self._process = None
            self._download_process = None
            self._state = "stopped"
            self._detail = "Modelo detenido"
        if download_process is not None and download_process.poll() is None:
            download_process.terminate()
            try:
                download_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                download_process.kill()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    def chat(self, system: str, user: str, *, temperature: float = 0.85, max_tokens: int = 500) -> str:
        with self._lock:
            if self._state != "running":
                raise EngineError("Inicia Qwen desde la pestaña Historias para comenzar a conversar")
        return self._client(system, user, temperature=temperature, max_tokens=max_tokens)
