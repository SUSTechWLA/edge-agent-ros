"""Check cache integrity, recovery and extraction rules without starting Gazebo or using the network."""

import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

SOURCE = Path(__file__).resolve().parents[1] / "scene" / "campus_large" / "fetch_assets.py"
module_spec = importlib.util.spec_from_file_location("fetch_campus_assets", SOURCE)
assets = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(assets)


class AssetCacheTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "upstream" / "asset.zip"
        self.path.parent.mkdir()
        with ZipFile(self.path, "w", ZIP_DEFLATED) as z:
            z.writestr("model.sdf", b"<sdf/>")
            z.writestr("meshes/test.obj", b"v 0 0 0")
        self.spec = self.lock(self.path)

    def lock(self, path):
        with ZipFile(path) as z:
            return {
                "model": "Fixture",
                "cache_zip": "upstream/asset.zip",
                "zip_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "zip_members": len(z.infolist()),
                "expanded_bytes": sum(i.file_size for i in z.infolist()),
                "entrypoints": ["model.sdf"],
            }

    def test_first_install_repeat_and_repair_without_network(self):
        with patch.object(
            assets, "urlopen", side_effect=AssertionError("offline mode accessed network")
        ):
            self.assertEqual(assets.install(self.root, self.spec, True)["restored_files"], 2)
            self.assertEqual(assets.install(self.root, self.spec, True)["restored_files"], 0)
            target = self.root / "models" / "Fixture" / "meshes" / "test.obj"
            target.write_bytes(b"v 9 9 9")
            self.assertEqual(assets.install(self.root, self.spec, True)["restored_files"], 1)
            self.assertEqual(target.read_bytes(), b"v 0 0 0")

    def test_corrupted_archive_preserves_installed_model(self):
        assets.install(self.root, self.spec, True)
        with self.path.open("r+b") as f:
            f.write(b"X")
        with self.assertRaisesRegex(ValueError, "missing or damaged"):
            assets.install(self.root, self.spec, True)
        self.assertEqual((self.root / "models/Fixture/model.sdf").read_bytes(), b"<sdf/>")

    def test_cold_range_download_retries_and_locks_raw_zip(self):
        raw = self.path.read_bytes()
        size = 64
        self.path.unlink()
        self.spec.update(
            archive_url="https://fuel.example/fixture.zip",
            range_bytes=size,
            range_sha256=[
                hashlib.sha256(raw[i : i + size]).hexdigest() for i in range(0, len(raw), size)
            ],
        )
        attempts = []

        def upstream(request, timeout):
            lo, hi = (int(n) for n in request.get_header("Range")[6:].split("-"))
            attempts.append((lo, hi))
            response = io.BytesIO(raw[lo : hi + 1])
            response.status = 206
            response.headers = {"Content-Range": f"bytes {lo}-{hi}/{len(raw)}"}
            if lo == 0 and attempts.count((lo, hi)) == 1:
                response.headers["Content-Range"] = "bytes 0-1/2"
            return response

        with patch.object(assets, "urlopen", side_effect=upstream), patch.object(assets.time, "sleep"):
            result = assets.install(self.root, self.spec, False)
        self.assertEqual(attempts.count((0, size - 1)), 2)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(result["restored_files"], 2)
        self.assertEqual(list(self.path.parent.glob(".*")), [])

    def test_corrupt_download_never_replaces_existing_cache(self):
        self.spec.update(archive_url="https://fuel.example/fixture.zip")
        damaged = bytes(self.spec["zip_bytes"])
        self.path.write_bytes(b"old cache")
        with patch.object(assets, "urlopen", side_effect=lambda *a, **kw: io.BytesIO(damaged)):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                assets.install(self.root, self.spec, False)
        self.assertEqual(self.path.read_bytes(), b"old cache")
        self.assertFalse((self.root / "models/Fixture").exists())

    def test_unsafe_names_and_duplicate_do_not_publish_partial_model(self):
        for bad in ("../escape", "/abc", "a\\escape", "C:/escape", "CON.txt", "x:", "a//b", "MODEL.SDF"):
            with self.subTest(path=bad):
                with ZipFile(self.path, "w") as z:
                    z.writestr("model.sdf", "<sdf/>")
                    # ZipFile itself normalizes backslashes when authoring on Windows.
                    z.writestr(bad.replace("\\", "!"), "unsafe")
                if "\\" in bad:
                    self.path.write_bytes(self.path.read_bytes().replace(b"a!escape", b"a\\escape"))
                with self.assertRaises(ValueError):
                    assets.install(self.root, self.lock(self.path), True)
                self.assertFalse((self.root / "models/Fixture").exists())

    def test_unexpected_upstream_manifest_fails_before_install(self):
        self.spec["zip_members"] += 1
        with self.assertRaisesRegex(ValueError, "manifest"):
            assets.install(self.root, self.spec, True)
        self.assertFalse((self.root / "models/Fixture").exists())


if __name__ == "__main__":
    unittest.main()
