"""Pruebas offline de las descargas paralelas reanudables."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import parallel_download as accelerated
from scripts import download_models


class FakeResponse:
    def __init__(self, data: bytes, status: int, content_range: str = ""):
        self.data = data
        self.status = status
        self.headers = {"Content-Range": content_range}

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.data)
        data, self.data = self.data[:size], self.data[size:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ParallelDownloadTests(unittest.TestCase):
    def test_downloads_ranges_and_assembles_in_order(self):
        payload = b"0123456789ABCDEFGHIJKLMN"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "model.gguf.part"

            def open_range(_url, start, end):
                if start == end == 0:
                    return FakeResponse(b"0", 206, f"bytes 0-0/{len(payload)}")
                actual_end = min(end, len(payload) - 1)
                return FakeResponse(
                    payload[start:actual_end + 1], 206,
                    f"bytes {start}-{actual_end}/{len(payload)}",
                )

            with mock.patch.object(accelerated, "CHUNK_SIZE", 8), \
                 mock.patch.object(accelerated, "MIN_PARALLEL_SIZE", 2), \
                 mock.patch.object(accelerated, "STATUS_DIR", Path(tmp) / "status"), \
                 mock.patch.object(accelerated, "_request", side_effect=open_range):
                result = accelerated.download_parallel(
                    "https://example.invalid/model", dest,
                    expected_bytes=len(payload), workers=4,
                )
            retrieved = dest.read_bytes()

        self.assertTrue(result)
        self.assertEqual(retrieved, payload)
        self.assertEqual(hashlib.sha256(retrieved).hexdigest(), hashlib.sha256(payload).hexdigest())
        self.assertFalse(dest.with_name(dest.name + ".chunks").exists())

    def test_resumes_a_partial_chunk(self):
        payload = b"0123456789ABCDEFGHIJKLMN"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "model.gguf.part"
            chunk_dir = dest.with_name(dest.name + ".chunks")
            chunk_dir.mkdir()
            (chunk_dir / "000000.part").write_bytes(payload[:3])

            def open_range(_url, start, end):
                if start == end == 0:
                    return FakeResponse(b"0", 206, f"bytes 0-0/{len(payload)}")
                actual_end = min(end, len(payload) - 1)
                return FakeResponse(
                    payload[start:actual_end + 1], 206,
                    f"bytes {start}-{actual_end}/{len(payload)}",
                )

            with mock.patch.object(accelerated, "CHUNK_SIZE", 8), \
                 mock.patch.object(accelerated, "MIN_PARALLEL_SIZE", 2), \
                 mock.patch.object(accelerated, "STATUS_DIR", Path(tmp) / "status"), \
                 mock.patch.object(accelerated, "_request", side_effect=open_range):
                result = accelerated.download_parallel(
                    "https://example.invalid/model", dest,
                    expected_bytes=len(payload), workers=4,
                )
            retrieved = dest.read_bytes()

        self.assertTrue(result)
        self.assertEqual(retrieved, payload)

    def test_returns_none_if_server_ignores_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(accelerated, "MIN_PARALLEL_SIZE", 2), \
                 mock.patch.object(accelerated, "STATUS_DIR", Path(tmp) / "status"), \
                 mock.patch.object(accelerated, "_request", return_value=FakeResponse(b"all", 200)):
                result = accelerated.download_parallel(
                    "https://example.invalid/model", Path(tmp) / "model.part",
                    expected_bytes=10, workers=4,
                )
        self.assertIsNone(result)

    def test_falls_back_if_a_range_request_is_rejected(self):
        payload = b"0123456789ABCDEFGHIJKLMN"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "model.part"

            def open_range(url, start, end):
                if start == end == 0:
                    return FakeResponse(b"0", 206, f"bytes 0-0/{len(payload)}")
                if start >= 8:
                    from urllib.error import HTTPError
                    raise HTTPError(url, 416, "Range Not Satisfiable", {}, None)
                actual_end = min(end, len(payload) - 1)
                return FakeResponse(payload[start:actual_end + 1], 206, f"bytes {start}-{actual_end}/{len(payload)}")

            with mock.patch.object(accelerated, "CHUNK_SIZE", 8), \
                 mock.patch.object(accelerated, "MIN_PARALLEL_SIZE", 2), \
                 mock.patch.object(accelerated, "STATUS_DIR", Path(tmp) / "status"), \
                 mock.patch.object(accelerated, "_request", side_effect=open_range):
                result = accelerated.download_parallel(
                    "https://example.invalid/model", dest,
                    expected_bytes=len(payload), workers=3,
                )
            chunk_dir = dest.with_name(dest.name + ".chunks")
        self.assertIsNone(result)
        self.assertFalse(chunk_dir.exists())
        self.assertFalse(dest.exists())

    def test_descarga_de_modelos_usa_acelerador_si_hay_tamano_conocido(self):
        payload = b"w" * (8 * 1024 * 1024 + 1)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "model.safetensors"

            def fake_parallel(_url, partial, *, expected_bytes, workers):
                self.assertEqual(expected_bytes, len(payload))
                self.assertGreaterEqual(workers, 1)
                Path(partial).write_bytes(payload)
                return True

            with mock.patch.object(accelerated, "STATUS_DIR", Path(tmp) / "status"), \
                 mock.patch.object(download_models, "download_parallel", side_effect=fake_parallel) as call:
                ok = download_models.download_file(
                    "curl.exe", "https://example.invalid/weights", destination,
                    expected_bytes=len(payload),
                )
            retrieved = destination.read_bytes()
        self.assertTrue(ok)
        call.assert_called_once()
        self.assertEqual(retrieved, payload)


if __name__ == "__main__":
    unittest.main()
