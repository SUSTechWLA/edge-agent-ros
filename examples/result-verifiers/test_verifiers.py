"""Language-independent receipt semantics and Linux artifact-store constraints."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from artifact_verification import Counterevidence, Inconclusive, MAX_BYTES, PLUGINS, receipt
import digest_check
import image_check


OP = "camera-check/step/1"
SCOPE = hashlib.sha256(OP.encode()).hexdigest()
FRAME = b"P5\n# fixture\n2 2\n255\n\n#\x00\xff"
SHA = hashlib.sha256(FRAME).hexdigest()


def request_for(plugin):
    return {"operation_id": OP, "task_id": "camera-check", "trace_id": "root-trace",
            "plugin_id": plugin, "plugin_version": "1", "service": PLUGINS[plugin],
            "tool": "capture_photo", "arguments": {}, "outcomes": {"image": "capture"},
            "started_at": 100, "evidence": {"capture": "artifact://" + SCOPE + "/frame.pgm", "sha256": SHA}}


class ImageContract(unittest.TestCase):
    def test_image_observation(self):
        evidence = image_check.inspect("frame.pgm", FRAME, {})
        self.assertEqual((evidence["width"], evidence["height"], evidence["pixels"]), (2, 2, 4))
        self.assertEqual((evidence["pixel_min"], evidence["pixel_max"], evidence["sha256"]), (0, 255, SHA))

    def test_corrupt_header_or_raster_is_counterevidence(self):
        for data in (b"P5", b"P50 1 255\nX", b"P5 0 1 255\n", b"P5 1 1 0\nX", b"P5 1 1 2\n\x03",
                     b"P5 1 1 255", b"P5 1 1 255\n", FRAME + b"\n", FRAME[:-1]):
            with self.subTest(data=data), self.assertRaises(Counterevidence):
                image_check.inspect("frame.pgm", data, {})

    def test_unsupported_format_is_inconclusive(self):
        for name, data in (("frame.jpeg", b""), ("frame.pgm", b"P5 1 1 65535\n\x00\x00")):
            with self.subTest(name=name), self.assertRaises(Inconclusive):
                image_check.inspect(name, data, {})

    def test_independent_digest_observation(self):
        self.assertEqual(digest_check.inspect("frame.pgm", FRAME, {"sha256": SHA})["bytes"], len(FRAME))
        with self.assertRaises(Counterevidence) as mismatch:
            digest_check.inspect("frame.pgm", FRAME, {"sha256": "0"*64})
        self.assertEqual(mismatch.exception.evidence["observed_sha256"], SHA)
        with self.assertRaises(Inconclusive):
            digest_check.inspect("frame.pgm", FRAME, {})

    def test_contract_identity(self):
        req = request_for("image-check")
        for fields in ({"service": "verify.digest"}, {"plugin_version": "2"}, {"operation_id": ""}, {"arguments": []}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                receipt("image-check", "", {**req, **fields}, image_check.inspect)


@unittest.skipUnless(os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"), "Linux artifact-store tests run in Docker")
class StoreContract(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / SCOPE).mkdir()
        self.artifact = self.root / SCOPE / "frame.pgm"

    def check(self, plugin="image-check", evidence=None):
        req = request_for(plugin)
        if evidence is not None:
            req["evidence"].update(evidence)
        inspect = image_check.inspect if plugin == "image-check" else digest_check.inspect
        return receipt(plugin, self.root, req, inspect)

    def test_replay_and_independent_binding(self):
        self.artifact.write_bytes(FRAME)
        for plugin in PLUGINS:
            with self.subTest(plugin=plugin):
                result = self.check(plugin)
                self.assertEqual(result, self.check(plugin))
                self.assertEqual(result["verdict"], "pass")
                self.assertEqual(result["evidence"]["sha256"], SHA)
                self.assertEqual(result["operation_id"], OP)
                self.assertEqual(result["plugin_id"], plugin)
        mismatch = self.check("digest-check", {"sha256": "0"*64})
        self.assertEqual((mismatch["verdict"], mismatch["evidence"]["code"]), ("fail", "digest.mismatch"))

    def test_absence_and_unavailable_storage(self):
        self.assertEqual(self.check()["evidence"]["code"], "artifact.missing")
        self.root = self.root / "missing-volume"
        self.assertEqual(self.check()["verdict"], "unknown")

    def test_reference_is_scoped_and_local(self):
        self.artifact.write_bytes(FRAME)
        for ref in ("file:///etc/passwd", "https://camera/frame.pgm", "artifact://" + "a"*64 + "/frame.pgm",
                    "artifact://" + SCOPE + "/../frame.pgm", "artifact://" + SCOPE + "/%2e%2e", None):
            with self.subTest(ref=ref):
                self.assertEqual(self.check(evidence={"capture": ref})["verdict"], "fail")

    def test_no_symlink_hardlink_or_special_file(self):
        source = self.root / "external"
        source.write_bytes(FRAME)
        for link in (lambda: self.artifact.symlink_to(source), lambda: os.link(source, self.artifact),
                     lambda: os.mkfifo(self.artifact), lambda: self.artifact.mkdir()):
            link()
            self.assertEqual(self.check()["verdict"], "fail")
            if self.artifact.is_dir():
                self.artifact.rmdir()
            else:
                self.artifact.unlink()
        self.artifact.parent.rmdir()
        self.artifact.parent.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.check()["verdict"], "fail")

    def test_oversize_is_inconclusive(self):
        with self.artifact.open("wb") as f:
            f.truncate(MAX_BYTES + 1)
        self.assertEqual(self.check()["evidence"]["code"], "artifact.exceeds_verifier_limit")


if __name__ == "__main__":
    unittest.main()
