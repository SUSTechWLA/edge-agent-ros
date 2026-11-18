"""Fetch the two pinned Gazebo Fuel models or verify and restore them from an offline ZIP cache."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from zipfile import ZipFile

LOCKFILE = Path(__file__).with_name("asset-lock.json")
DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "artifacts" / "sim-assets"
BLOCK = 1024 * 1024


def digest(source):
    checksum = sha256()
    for chunk in iter(lambda: source.read(BLOCK), b""):
        checksum.update(chunk)
    return checksum.hexdigest()


def cached_archive(path, spec):
    if not path.is_file() or path.stat().st_size != spec["zip_bytes"]:
        return False
    with path.open("rb") as stream:
        return digest(stream) == spec["sha256"]


def checked_path(root, relative):
    """Use only regular names on both Windows and Linux; reject redirects inside the cache."""
    path = PurePosixPath(relative)
    windows = PureWindowsPath(relative)
    parts = relative.rstrip("/").split("/")
    if (
        not parts
        or path.is_absolute()
        or windows.drive
        or "\\" in relative
        or any(
            part in ("", ".", "..")
            or part.endswith((".", " "))
            or ":" in part
            or PureWindowsPath(part).is_reserved()
            for part in parts
        )
    ):
        raise ValueError(f"unsafe asset path: {relative!r}")
    target = root
    for part in parts:
        target = target / part
        attributes = getattr(target.lstat(), "st_file_attributes", 0) if target.exists() else 0
        if target.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"redirect in asset cache: {relative!r}")
    return target


def download(spec, temp):
    if not spec.get("range_sha256"):
        with urlopen(spec["archive_url"], timeout=60) as stream, temp.open("wb") as output:
            remaining = spec["zip_bytes"]
            while remaining:
                data = stream.read(min(BLOCK, remaining))
                if not data:
                    raise ValueError(f"incomplete archive for {spec['model']}")
                output.write(data)
                remaining -= len(data)
            if stream.read(1):
                raise ValueError(f"unexpected archive length for {spec['model']}")
        return
    total, stride = spec["zip_bytes"], spec["range_bytes"]
    with tempfile.TemporaryDirectory(dir=temp.parent, prefix=".ranges-") as directory:

        def fetch_range(index):
            start = index * stride
            end = min(start + stride, total) - 1
            for attempt in range(5):
                try:
                    # Smaller ranges tolerate unreliable routes to Fuel without changing the locked bytes.
                    width = stride if attempt == 0 else 256 * 1024
                    block = bytearray()
                    for first in range(start, end + 1, width):
                        last = min(first + width - 1, end)
                        request = Request(
                            spec["archive_url"],
                            headers={"Range": f"bytes={first}-{last}", "Accept-Encoding": "identity"},
                        )
                        with urlopen(request, timeout=25) as response:
                            if (
                                response.status != 206
                                or response.headers.get("Content-Range")
                                != f"bytes {first}-{last}/{total}"
                            ):
                                raise ValueError("server did not confirm requested range")
                            part = response.read(last - first + 2)
                        if len(part) != last - first + 1:
                            raise ValueError("short asset range")
                        block.extend(part)
                    if sha256(block).hexdigest() != spec["range_sha256"][index]:
                        raise ValueError("range length or SHA-256 differs from the asset lock")
                    (Path(directory) / str(index)).write_bytes(block)
                    return
                except (OSError, ValueError):
                    if attempt == 4:
                        raise
                    time.sleep(min(8, 2**attempt))

        count = len(spec["range_sha256"])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(fetch_range, range(count)))
        with temp.open("wb") as output:
            for index in range(count):
                with (Path(directory) / str(index)).open("rb") as part:
                    shutil.copyfileobj(part, output, BLOCK)


def ensure_archive(path, spec, offline):
    if cached_archive(path, spec):
        return
    if offline:
        raise ValueError(
            f"missing or damaged cached archive {path}; run fetch_assets.py without --offline"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".download-", delete=False) as output:
        temp = Path(output.name)
    try:
        download(spec, temp)
        if not cached_archive(temp, spec):
            raise ValueError(f"SHA-256 mismatch for {spec['model']}")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def extract_verified(bundle, model, spec):
    members = bundle.infolist()
    if (
        len(members) != spec["zip_members"]
        or sum(x.file_size for x in members) != spec["expanded_bytes"]
    ):
        raise ValueError("unexpected archive manifest")
    names = set()
    for entry in members:
        relative = entry.orig_filename
        checked_path(model, relative)
        kind = stat.S_IFMT(entry.external_attr >> 16)
        if (
            relative.casefold().rstrip("/") in names
            or kind not in (0, stat.S_IFDIR, stat.S_IFREG)
            or entry.flag_bits & 1
        ):
            raise ValueError(f"unsupported archive member: {relative!r}")
        names.add(relative.casefold().rstrip("/"))
    for required in spec["entrypoints"]:
        if required not in bundle.namelist():
            raise ValueError(f"missing locked model resource: {required}")
    restored = 0
    for entry in members:
        target = checked_path(model, entry.filename)
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size == entry.file_size:
            with target.open("rb") as installed, bundle.open(entry) as original:
                if digest(installed) == digest(original):
                    continue
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".restore-", delete=False) as out:
            temp = Path(out.name)
        try:
            # ZipFile checks the member's CRC when the stream is fully consumed.
            with bundle.open(entry) as original, temp.open("wb") as out:
                shutil.copyfileobj(original, out, BLOCK)
            os.replace(temp, target)
            restored += 1
        finally:
            temp.unlink(missing_ok=True)
    return restored


def install(root, spec, offline):
    archive = checked_path(root, spec["cache_zip"])
    ensure_archive(archive, spec, offline)
    model = checked_path(root, "models/" + spec["model"])
    model.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive) as bundle:
        if not model.exists():
            # A simulator never discovers a half-installed model on the first install.
            with tempfile.TemporaryDirectory(dir=model.parent, prefix=".model-") as staging:
                stage = Path(staging) / "complete"
                stage.mkdir()
                restored = extract_verified(bundle, stage, spec)
                try:
                    stage.rename(model)
                except OSError:
                    if not model.is_dir():
                        raise
                    restored = extract_verified(bundle, model, spec)
        else:
            restored = extract_verified(bundle, model, spec)
    return {
        "model": spec["model"],
        "archive": spec["cache_zip"],
        "sha256": spec["sha256"],
        "entrypoint": f"models/{spec['model']}/model.sdf",
        "restored_files": restored,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_CACHE, help="Gazebo asset cache and models directory"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="never use the network; validate or repair from cached ZIPs",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = json.loads(LOCKFILE.read_text(encoding="utf-8"))
    print(json.dumps([install(root, model, args.offline) for model in lock["models"]], indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(str(error))
