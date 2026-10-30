"""Descargas reanudables por rangos HTTP para archivos de modelos grandes."""

from __future__ import annotations

import concurrent.futures
import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CHUNK_SIZE = 16 * 1024 * 1024
MIN_PARALLEL_SIZE = 8 * 1024 * 1024
USER_AGENT = "WAIFU-parallel-download/1.0"
STATUS_DIR = Path(__file__).resolve().parents[1] / "data" / "model-downloads"


def _status_path(partial_path: str | Path) -> Path:
    key = hashlib.sha256(str(Path(partial_path).resolve()).encode("utf-8")).hexdigest()[:24]
    return STATUS_DIR / f"{key}.json"


def update_download_status(
    partial_path: str | Path, *, state: str, downloaded: int = 0,
    total: int | None = None, workers: int = 0, speed_mbps: float = 0.0,
    detail: str = "",
) -> None:
    path = _status_path(partial_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": path.stem,
        "file": Path(partial_path).name.removesuffix(".part"),
        "state": state,
        "downloaded": max(0, int(downloaded)),
        "total": max(0, int(total)) if total is not None else None,
        "percent": min(100.0, max(0.0, downloaded * 100.0 / total)) if total else None,
        "workers": max(0, int(workers)),
        "speed_mbps": round(max(0.0, float(speed_mbps)), 2),
        "detail": detail,
        "updated_at": time.time(),
    }
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def _request(url: str, start: int, end: int):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"},
    )
    return urllib.request.urlopen(request, timeout=30)


def _content_range(response) -> tuple[int, int, int] | None:
    value = response.headers.get("Content-Range", "")
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", value.strip(), re.I)
    return tuple(map(int, match.groups())) if match else None


def download_parallel(
    url: str,
    partial_path: str | Path,
    *,
    expected_bytes: int | None = None,
    workers: int = 8,
) -> bool | None:
    """Descarga en fragmentos; True éxito, False error, None si Range no sirve.

    Los fragmentos quedan en `.chunks` al fallar para reanudar la siguiente vez.
    El archivo parcial normal solo se reemplaza cuando todos los fragmentos están
    completos. Así se conserva la ruta de reanudación secuencial existente.
    """
    partial = Path(partial_path)
    chunk_dir = partial.with_name(partial.name + ".chunks")
    resuming_chunks = chunk_dir.is_dir()
    if workers <= 1 or (not resuming_chunks and partial.exists() and partial.stat().st_size):
        return None
    try:
        with _request(url, 0, 0) as probe:
            if getattr(probe, "status", 200) != 206:
                update_download_status(partial, state="fallback", total=expected_bytes, workers=workers,
                                       detail="El servidor no admite rangos; se usará el modo normal")
                return None
            content_range = _content_range(probe)
            if content_range is None or content_range[:2] != (0, 0):
                update_download_status(partial, state="fallback", total=expected_bytes, workers=workers,
                                       detail="El servidor no admite rangos; se usará el modo normal")
                return None
            total = content_range[2]
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"  acelerador: no se pudo consultar rangos HTTP ({exc}); se usará descarga normal")
        update_download_status(partial, state="fallback", total=expected_bytes, workers=workers,
                               detail="No se pudieron consultar rangos; se usará el modo normal")
        return None
    if expected_bytes is not None and total != expected_bytes:
        print(f"  acelerador: tamaño remoto {total} != esperado {expected_bytes}")
        return False
    if total < MIN_PARALLEL_SIZE:
        return None

    partial.parent.mkdir(parents=True, exist_ok=True)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    count = (total + CHUNK_SIZE - 1) // CHUNK_SIZE
    if count < 2:
        return None
    worker_count = max(2, min(int(workers), 16, count))
    print(f"  descarga acelerada: {worker_count} conexiones · {count} partes")
    update_download_status(partial, state="downloading", downloaded=0, total=total,
                           workers=worker_count, detail="Descarga paralela en curso")

    progress_lock = threading.Lock()
    downloaded = 0
    for index in range(count):
        chunk = chunk_dir / f"{index:06d}.part"
        if chunk.is_file():
            expected_length = min(CHUNK_SIZE, total - index * CHUNK_SIZE)
            if chunk.stat().st_size <= expected_length:
                downloaded += chunk.stat().st_size
    progress_last = time.monotonic()
    started = time.monotonic()

    def fetch(index: int) -> bool | None:
        nonlocal downloaded, progress_last
        start = index * CHUNK_SIZE
        end = min(start + CHUNK_SIZE, total) - 1
        target = chunk_dir / f"{index:06d}.part"
        length = end - start + 1
        current = target.stat().st_size if target.is_file() else 0
        if current == length:
            return True
        if current > length:
            target.unlink()
            current = 0
        for attempt in range(5):
            try:
                with _request(url, start + current, end) as response:
                    expected_range = (start + current, end, total)
                    if getattr(response, "status", 200) != 206 or _content_range(response) != expected_range:
                        return None
                    mode = "ab" if current else "wb"
                    with target.open(mode) as output:
                        while True:
                            data = response.read(1024 * 1024)
                            if not data:
                                break
                            if current + len(data) > length:
                                raise ValueError("el rango recibió más bytes de lo esperado")
                            output.write(data)
                            current += len(data)
                            with progress_lock:
                                downloaded += len(data)
                                now = time.monotonic()
                                if now - progress_last >= 1.5:
                                    pct = min(100.0, downloaded * 100 / total)
                                    elapsed = max(now - started, 0.1)
                                    speed = downloaded / elapsed / (1024 * 1024)
                                    print(f"  {pct:5.1f}% {downloaded / (1024**3):.2f} GiB ({speed:.1f} MB/s)", flush=True)
                                    update_download_status(partial, state="downloading", downloaded=downloaded,
                                                           total=total, workers=worker_count, speed_mbps=speed,
                                                           detail="Descarga paralela en curso")
                                    progress_last = now
                if target.stat().st_size == length:
                    return True
                raise OSError(f"parte incompleta {index}: {target.stat().st_size}/{length}")
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 405, 416, 501):
                    return None
                if attempt == 4:
                    print(f"  acelerador: falló la parte {index + 1}/{count}: {exc}")
                    return False
                delay = min(1.5 * (2 ** attempt), 10)
                update_download_status(partial, state="retrying", downloaded=downloaded, total=total,
                    workers=worker_count, speed_mbps=0,
                    detail=f"Conexión detenida en parte {index + 1}/{count}; reintento {attempt + 1}/5")
                time.sleep(delay)
            except (urllib.error.URLError, OSError, TimeoutError, ValueError, http.client.HTTPException) as exc:
                if isinstance(exc, ValueError) and "servidor no respetó" in str(exc):
                    return False
                if attempt == 4:
                    print(f"  acelerador: falló la parte {index + 1}/{count}: {exc}")
                    return False
                delay = min(1.5 * (2 ** attempt), 10)
                update_download_status(partial, state="retrying", downloaded=downloaded, total=total,
                    workers=worker_count, speed_mbps=0,
                    detail=f"Conexión detenida en parte {index + 1}/{count}; reintento {attempt + 1}/5")
                time.sleep(delay)
        return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        results = list(pool.map(fetch, range(count)))
    if any(result is None for result in results):
        shutil.rmtree(chunk_dir, ignore_errors=True)
        update_download_status(partial, state="fallback", total=total, workers=worker_count,
                               detail="El servidor rechazó un rango; se usará el modo normal")
        return None
    if not all(results):
        update_download_status(partial, state="error", downloaded=downloaded, total=total,
                               workers=worker_count, detail="Falló una parte; se conservaron fragmentos para reanudar")
        return False

    try:
        with partial.open("wb") as output:
            for index in range(count):
                chunk = chunk_dir / f"{index:06d}.part"
                with chunk.open("rb") as source:
                    shutil.copyfileobj(source, output, length=8 * 1024 * 1024)
        if partial.stat().st_size != total:
            print(f"  acelerador: ensamblado incompleto ({partial.stat().st_size}/{total})")
            update_download_status(partial, state="error", downloaded=partial.stat().st_size,
                                   total=total, workers=worker_count, detail="Ensamblado incompleto")
            return False
    except OSError as exc:
        print(f"  acelerador: no se pudieron ensamblar las partes: {exc}")
        update_download_status(partial, state="error", downloaded=downloaded, total=total,
                               workers=worker_count, detail="No se pudieron ensamblar las partes")
        return False

    shutil.rmtree(chunk_dir, ignore_errors=True)
    elapsed = max(time.monotonic() - started, 0.1)
    speed = total / elapsed / (1024 * 1024)
    update_download_status(partial, state="downloaded", downloaded=total, total=total,
                           workers=worker_count, speed_mbps=speed,
                           detail="Transferencia completa; el descargador verificará el archivo")
    print(f"  descarga acelerada completa: {total / (1024**3):.2f} GiB ({speed:.1f} MB/s)", flush=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Descarga acelerada por rangos HTTP")
    parser.add_argument("--url")
    parser.add_argument("--partial", required=True, type=Path)
    parser.add_argument("--bytes", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--mark", choices=("verified", "error"), default=None)
    args = parser.parse_args(argv)
    if args.mark:
        previous_path = _status_path(args.partial)
        previous = {}
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        update_download_status(
            args.partial, state=args.mark,
            downloaded=previous.get("downloaded", args.bytes or 0),
            total=previous.get("total", args.bytes),
            workers=previous.get("workers", args.workers or 0),
            speed_mbps=previous.get("speed_mbps", 0),
            detail="SHA-256 y tamaño verificados" if args.mark == "verified" else "Falló la verificación",
        )
        return 0
    if not args.url:
        parser.error("--url es obligatorio al descargar")
    if args.workers is None:
        try:
            args.workers = int(os.environ.get("WAIFU_DOWNLOAD_WORKERS", "8"))
        except ValueError:
            args.workers = 8
    result = download_parallel(
        args.url, args.partial, expected_bytes=args.bytes,
        workers=max(1, min(16, args.workers)),
    )
    return 0 if result is True else 2 if result is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
