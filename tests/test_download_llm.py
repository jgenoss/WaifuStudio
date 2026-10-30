"""Tests offline de scripts/download_llm.py (descarga verificada del LLM M11-2).

El modulo se carga por ruta: no vive en `app/` y solo usa stdlib. Ningun test
usa red: `--check` trabaja sobre archivos de un tmp.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from scripts import parallel_download as download_accel

ROOT = Path(__file__).resolve().parents[1]


def _load_download_llm():
    source = ROOT / "scripts" / "download_llm.py"
    spec = importlib.util.spec_from_file_location("waifu_download_llm", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


download_llm = _load_download_llm()


def _entry(path: Path, **extra) -> dict:
    payload = path.read_bytes()
    entry = {
        "id": "item",
        "filename": path.name,
        "target": "dest",
        "url": "https://example.invalid/item",
        "sha256": hashlib.sha256(payload).hexdigest().upper(),
        "bytes": len(payload),
    }
    entry.update(extra)
    return entry


class Sha256Tests(unittest.TestCase):
    def test_hash_de_archivo_conocido(self):
        payload = b"WAIFU" * 1000
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dato.bin"
            path.write_bytes(payload)
            self.assertEqual(
                download_llm.sha256_file(path),
                hashlib.sha256(payload).hexdigest().upper(),
            )


class LoadManifestTests(unittest.TestCase):
    def _write(self, tmp, data):
        path = Path(tmp) / "manifest.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_manifiesto_valido(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                {
                    "version": 1,
                    "files": [
                        {
                            "id": "a",
                            "filename": "a.bin",
                            "target": "d",
                            "url": "https://example.invalid/a",
                            "sha256": "AA",
                        }
                    ],
                },
            )
            data = download_llm.load_manifest(path)
        self.assertEqual(data["files"][0]["id"], "a")

    def test_rechaza_sin_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"version": 1, "files": []})
            with self.assertRaises(SystemExit):
                download_llm.load_manifest(path)

    def test_rechaza_entrada_incompleta(self):
        complete = {
            "id": "a",
            "filename": "a.bin",
            "target": "d",
            "url": "https://example.invalid/a",
            "sha256": "AA",
        }
        for missing in ("id", "filename", "target", "url", "sha256"):
            entry = dict(complete)
            del entry[missing]
            with self.subTest(missing=missing):
                with tempfile.TemporaryDirectory() as tmp:
                    path = self._write(tmp, {"files": [entry]})
                    with self.assertRaises(SystemExit):
                        download_llm.load_manifest(path)


class EntryPathsTests(unittest.TestCase):
    def test_target_y_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = {"target": "models/llm", "filename": "model.gguf"}
            target, part = download_llm.entry_paths(entry, root)
            self.assertEqual(target, root / "models" / "llm" / "model.gguf")
            self.assertEqual(part, target.with_name("model.gguf.part"))


class VerifyTests(unittest.TestCase):
    def _tree(self, tmp, payload=b"contenido"):
        target = Path(tmp) / "dest" / "item.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(payload)
        return target

    def test_true_con_tamano_y_hash_correctos(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._tree(tmp)
            self.assertTrue(download_llm.verify(target, _entry(target)))

    def test_false_si_no_existe(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-existe.bin"
            self.assertFalse(download_llm.verify(missing, {"sha256": "AA"}))

    def test_false_por_tamano(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._tree(tmp)
            entry = _entry(target, bytes=target.stat().st_size + 1)
            self.assertFalse(download_llm.verify(target, entry))

    def test_false_por_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._tree(tmp)
            entry = _entry(target, sha256="0" * 64)
            self.assertFalse(download_llm.verify(target, entry))


class DownloadRetryTests(unittest.TestCase):
    def setUp(self):
        self.status_tmp = tempfile.TemporaryDirectory()
        self.status_patch = mock.patch.object(download_accel, "STATUS_DIR", Path(self.status_tmp.name))
        self.status_patch.start()

    def tearDown(self):
        self.status_patch.stop()
        self.status_tmp.cleanup()

    def test_usa_acelerador_para_archivo_grande_y_verifica_hash(self):
        payload = b"M" * (download_llm.MIN_PARALLEL_SIZE + 1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = {
                "id": "large-model", "filename": "large.gguf", "target": "dest",
                "url": "https://example.invalid/large",
                "sha256": hashlib.sha256(payload).hexdigest().upper(), "bytes": len(payload),
            }

            def fake_parallel(_url, partial, *, expected_bytes, workers):
                self.assertEqual(expected_bytes, len(payload))
                self.assertEqual(workers, 4)
                Path(partial).parent.mkdir(parents=True, exist_ok=True)
                Path(partial).write_bytes(payload)
                return True

            with mock.patch.object(download_accel, "STATUS_DIR", root / "status"), \
                 mock.patch.object(download_llm, "download_parallel", side_effect=fake_parallel) as accelerated:
                ok = download_llm.download(entry, root, workers=4)

            self.assertTrue(ok)
            accelerated.assert_called_once()
            self.assertEqual((root / "dest" / "large.gguf").read_bytes(), payload)

    def test_416_al_reanudar_reintenta_completo_y_verifica(self):
        payload = b"pesos correctos"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "dest" / "item.bin"
            part = target.with_name("item.bin.part")
            part.parent.mkdir(parents=True)
            part.write_bytes(b"X" * len(payload))
            entry = {
                "id": "item",
                "filename": "item.bin",
                "target": "dest",
                "url": "https://example.invalid/item",
                "sha256": hashlib.sha256(payload).hexdigest().upper(),
                "bytes": len(payload),
            }
            calls = []

            def fake_open(url, *, start=None):
                calls.append(start)
                if len(calls) == 1:
                    raise urllib.error.HTTPError(
                        url, 416, "Range Not Satisfiable", {}, None
                    )
                return io.BytesIO(payload)

            with mock.patch.object(download_llm, "_open", fake_open):
                ok = download_llm.download(entry, root)
            self.assertTrue(ok)
            self.assertEqual(calls, [len(payload), 0])
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(part.exists())

    def test_corte_actualiza_url_y_reanuda_desde_el_byte_guardado(self):
        payload = b"modelo de prueba que se reanuda"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = {
                "id": "item", "filename": "item.bin", "target": "dest",
                "url": "https://example.invalid/model",
                "sha256": hashlib.sha256(payload).hexdigest().upper(), "bytes": len(payload),
            }
            requests = []

            class Response:
                def __init__(self, status, pieces):
                    self.status = status
                    self.pieces = iter(pieces)

                def __enter__(self): return self
                def __exit__(self, *_args): return False
                def read(self, _size):
                    piece = next(self.pieces)
                    if isinstance(piece, Exception): raise piece
                    return piece

            def fake_open(url, *, start=None):
                requests.append((url, start))
                # La URL canónica se vuelve a consultar y el servidor entrega
                # un destino renovado, conservando el Range solicitado.
                if len(requests) == 1:
                    return Response(200, [payload[:8], b""])
                return Response(206, [payload[start:], b""])

            with mock.patch.object(download_llm, "_open", fake_open), \
                 mock.patch.object(download_llm.time, "sleep"):
                self.assertTrue(download_llm.download(entry, root))
            self.assertEqual([offset for _url, offset in requests], [0, 8])
            self.assertEqual(requests[0][0], requests[1][0])
            self.assertEqual((root / "dest" / "item.bin").read_bytes(), payload)

    def test_mismatch_de_hash_conserva_part(self):
        payload = b"datos"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = {
                "id": "item",
                "filename": "item.bin",
                "target": "dest",
                "url": "https://example.invalid/item",
                "sha256": hashlib.sha256(b"otro").hexdigest().upper(),
                "bytes": len(payload),
            }
            with mock.patch.object(
                download_llm, "_open", lambda url, *, start=None: io.BytesIO(payload)
            ):
                ok = download_llm.download(entry, root)
            part = root / "dest" / "item.bin.part"
            self.assertFalse(ok)
            self.assertTrue(part.is_file())
            self.assertEqual(part.read_bytes(), payload)
            self.assertFalse((root / "dest" / "item.bin").exists())


class ExtractRuntimeTests(unittest.TestCase):
    def test_extrae_zip_pequeno_en_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            dist.mkdir()
            with zipfile.ZipFile(dist / "runtime.zip", "w") as archive:
                archive.writestr("bin/llama-server.exe", b"MZ falso")
            entry = {
                "id": "rt",
                "target": "dist",
                "filename": "runtime.zip",
                "extract_to": "tools/runtime",
            }
            self.assertTrue(download_llm.extract_runtime(entry, root))
            extracted = root / "tools" / "runtime" / "bin" / "llama-server.exe"
            self.assertEqual(extracted.read_bytes(), b"MZ falso")

    def test_sin_extract_to_no_hace_nada(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {"id": "rt", "target": "dist", "filename": "runtime.zip"}
            self.assertTrue(download_llm.extract_runtime(entry, Path(tmp)))


class MainCheckTests(unittest.TestCase):
    def _manifest(self, root: Path, *, present: bool):
        target_dir = root / "dest"
        target_dir.mkdir(parents=True, exist_ok=True)
        payload = b"pesos"
        if present:
            (target_dir / "item.bin").write_bytes(payload)
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "id": "item",
                            "filename": "item.bin",
                            "target": "dest",
                            "url": "https://example.invalid/item",
                            "sha256": hashlib.sha256(payload).hexdigest().upper(),
                            "bytes": len(payload),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return manifest

    def _run(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = download_llm.main(argv)
        return code, buffer.getvalue()

    def test_check_devuelve_1_con_archivo_ausente(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root, present=False)
            code, output = self._run(
                ["--check", "--manifest", str(manifest), "--root", str(root)]
            )
        self.assertEqual(code, 1)
        self.assertIn("FALTA", output)

    def test_check_devuelve_0_con_archivo_correcto(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root, present=True)
            code, output = self._run(
                ["--check", "--manifest", str(manifest), "--root", str(root)]
            )
        self.assertEqual(code, 0)
        self.assertIn("RESULTADO: OK", output)


if __name__ == "__main__":
    unittest.main()
