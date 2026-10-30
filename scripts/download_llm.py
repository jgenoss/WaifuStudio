"""Descarga verificada del LLM unico (M11-2) segun `install/manifest/manifest.llm.json`.

Solo stdlib. Descarga con reanudacion (`.part` + Range), verifica sha256 completo
y extrae los zips de runtime si el manifiesto lo pide. Uso:

    python scripts/download_llm.py [--manifest PATH] [--root PATH] [--only id,id]
                                   [--check] [--force] [--workers 8]

`--check` solo verifica los archivos ya presentes (no descarga).
`--force` re-descarga aunque el sha256 ya cuadre.
"""

from __future__ import annotations

import argparse
import http.client
import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

try:
    from scripts.parallel_download import MIN_PARALLEL_SIZE, download_parallel, update_download_status
except ModuleNotFoundError:
    from parallel_download import MIN_PARALLEL_SIZE, download_parallel, update_download_status

APP_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = APP_ROOT / "install" / "manifest" / "manifest.llm.json"
CHUNK = 1024 * 1024
USER_AGENT = "WAIFU-download-llm/1.0 (+local)"


def sha256_file(path: Path, *, progress: bool = False) -> str:
    """sha256 completo en hex MAYUSCULAS leyendo por bloques grandes."""
    digest = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(8 * CHUNK)
            if not block:
                break
            digest.update(block)
            done += len(block)
            if progress and total:
                pct = done * 100.0 / total
                print(f"\r  hash: {pct:5.1f}%", end="", flush=True)
    if progress and total:
        print("\r  hash: 100.0%")
    return digest.hexdigest().upper()


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise SystemExit(f"manifiesto sin 'files': {path}")
    for entry in files:
        for key in ("id", "filename", "target", "url", "sha256"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise SystemExit(f"entrada de manifiesto incompleta ({key}): {entry!r}")
    return data


def entry_paths(entry: dict, root: Path) -> tuple[Path, Path]:
    target = root / entry["target"] / entry["filename"]
    return target, target.with_name(target.name + ".part")


def verify(path: Path, entry: dict) -> bool:
    if not path.is_file():
        return False
    expected_size = entry.get("bytes")
    if isinstance(expected_size, int) and path.stat().st_size != expected_size:
        return False
    return sha256_file(path) == entry["sha256"].upper()


def _open(url: str, *, start: int | None = None):
    headers = {"User-Agent": USER_AGENT}
    if start:
        headers["Range"] = f"bytes={start}-"
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(request, timeout=30)


def download(entry: dict, root: Path, *, force: bool = False, workers: int | None = None) -> bool:
    """Descarga una entrada; True si quedo verificada. Reanuda `.part`.

    Si el servidor responde HTTP 416 a la reanudacion (el `.part` tiene el
    tamano esperado pero su contenido no cuadra), borra el `.part` y
    reintenta la descarga completa UNA vez.
    """
    target, part = entry_paths(entry, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not force and verify(target, entry):
        print(f"[{entry['id']}] ya presente y verificado: {target}")
        return True
    if force and part.is_file():
        part.unlink()
    if force and part.with_name(part.name + ".chunks").is_dir():
        import shutil
        shutil.rmtree(part.with_name(part.name + ".chunks"), ignore_errors=True)
    start = part.stat().st_size if part.is_file() else 0
    expected = entry.get("bytes") if isinstance(entry.get("bytes"), int) else None
    if expected is not None and start > expected:
        print(f"[{entry['id']}] .part mayor que el tamano esperado; se reinicia")
        part.unlink()
        start = 0
    if workers is None:
        try:
            workers = int(os.environ.get("WAIFU_DOWNLOAD_WORKERS", "8"))
        except ValueError:
            workers = 8
    workers = max(1, min(16, workers))
    chunk_dir = part.with_name(part.name + ".chunks")
    accelerated = None
    if (expected is not None and expected >= MIN_PARALLEL_SIZE) or chunk_dir.is_dir():
        accelerated = download_parallel(
            entry["url"], part, expected_bytes=expected, workers=workers
        )
        if accelerated is False:
            return False
        if accelerated is True:
            start = done = part.stat().st_size
    if accelerated is not True:
        max_retries = 5
        attempt = 0
        while True:
            start = part.stat().st_size if part.is_file() else 0
            mode, done = ("ab" if start else "wb"), start
            began = time.perf_counter()
            try:
                response = _open(entry["url"], start=start)
                with response:
                    status = getattr(response, "status", 200)
                    if start and status != 206:
                        print(f"[{entry['id']}] el servidor no reanuda (HTTP {status}); reinicio desde 0")
                        mode, done, start = "wb", 0, 0
                    with part.open(mode) as handle:
                        last = 0.0
                        while True:
                            block = response.read(CHUNK)
                            if not block:
                                break
                            handle.write(block)
                            done += len(block)
                            now = time.perf_counter()
                            if now - last >= 2.0:
                                last = now
                                speed = (done - start) / max(now - began, 0.001) / (1024 * 1024)
                                update_download_status(part, state="downloading", downloaded=done,
                                    total=expected, workers=1, speed_mbps=speed,
                                    detail="Descarga normal reanudable")
                                if expected:
                                    pct = f"{done * 100.0 / expected:5.1f}%"
                                    eta_s = (expected - done) / max(speed * 1024 * 1024, 1.0)
                                    rest = f" ~{eta_s / 60:4.1f} min"
                                else:
                                    pct, rest = "", ""
                                print(f"  {pct} {done / (1024 ** 3):.2f} GiB ({speed:.1f} MB/s){rest}", flush=True)
                    if expected is not None and done < expected:
                        raise OSError(f"el servidor cerró la conexión antes de completar ({done}/{expected})")
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 416 and start > 0:
                    print(f"[{entry['id']}] HTTP 416 al reanudar; reinicio desde el principio")
                    part.unlink(missing_ok=True)
                    if attempt == 0:
                        continue
                error = exc
            except (urllib.error.URLError, OSError, TimeoutError, socket.timeout, http.client.HTTPException) as exc:
                error = exc
            attempt += 1
            if attempt >= max_retries:
                print(f"[{entry['id']}] ERROR de red tras {max_retries} intentos: {error}")
                update_download_status(part, state="error", downloaded=part.stat().st_size if part.exists() else 0,
                    total=expected, workers=1, detail=f"Se agotaron {max_retries} reintentos: {error}")
                return False
            saved = part.stat().st_size if part.is_file() else 0
            delay = min(1.5 * (2 ** (attempt - 1)), 10)
            update_download_status(part, state="retrying", downloaded=saved, total=expected,
                workers=1, speed_mbps=0, detail=f"Conexión detenida; reintento {attempt}/{max_retries} en {delay:g} s")
            print(f"[{entry['id']}] conexión detenida ({error}); reintento {attempt}/{max_retries} en {delay:g} s")
            time.sleep(delay)
    if expected is not None and done != expected:
        print(f"[{entry['id']}] tamano inesperado: {done} != {expected}; .part conservado")
        update_download_status(part, state="error", downloaded=done, total=expected,
                               detail="Tamaño recibido no coincide")
        return False
    digest = sha256_file(part, progress=True)
    if digest != entry["sha256"].upper():
        print(f"[{entry['id']}] SHA256 NO COINCIDE ({digest}); .part conservado")
        update_download_status(part, state="error", downloaded=part.stat().st_size, total=expected,
                               detail="SHA-256 no coincide")
        return False
    update_download_status(part, state="verified", downloaded=part.stat().st_size, total=expected,
                           workers=workers or 1, detail="SHA-256 y tamaño verificados")
    os.replace(part, target)
    if chunk_dir.is_dir():
        import shutil
        shutil.rmtree(chunk_dir, ignore_errors=True)
    print(f"[{entry['id']}] OK sha256 y tamano: {target}")
    return True


def extract_runtime(entry: dict, root: Path) -> bool:
    """Extrae el zip verificado a `extract_to` (si el manifiesto lo define)."""
    extract_to = entry.get("extract_to")
    if not extract_to:
        return True
    zip_path = root / entry["target"] / entry["filename"]
    destination = root / extract_to
    destination.mkdir(parents=True, exist_ok=True)
    print(f"[{entry['id']}] extrayendo en {destination}")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(destination)
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"[{entry['id']}] ERROR al extraer: {exc}")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Descarga verificada del LLM M11-2")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--root", type=Path, default=APP_ROOT)
    parser.add_argument("--only", default="")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--workers", type=int, default=None, help="conexiones paralelas (1–16; predeterminado 8)")
    args = parser.parse_args(argv)

    data = load_manifest(args.manifest)
    wanted = {item.strip() for item in args.only.split(",") if item.strip()}
    entries = [entry for entry in data["files"] if not wanted or entry["id"] in wanted]
    if not entries:
        print("nada que hacer (--only no casa con el manifiesto)")
        return 1

    failed: list[str] = []
    for entry in entries:
        target, _part = entry_paths(entry, args.root)
        if args.check:
            ok = verify(target, entry)
            print(f"[{entry['id']}] {'OK' if ok else 'FALTA/OJO'}: {target}")
        else:
            ok = download(entry, args.root, force=args.force, workers=args.workers)
            if ok and entry.get("kind") == "runtime":
                ok = extract_runtime(entry, args.root)
        if not ok:
            failed.append(entry["id"])

    if failed:
        print(f"RESULTADO: FALLO ({', '.join(failed)})")
        return 1
    print("RESULTADO: OK (todo verificado)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
