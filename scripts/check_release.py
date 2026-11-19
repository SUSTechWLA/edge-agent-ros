"""Verify a proposed release tag against the fetched source main and ROS packages."""
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


def verify(tag, remote="origin"):
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        raise ValueError("expected vMAJOR.MINOR.PATCH")
    ref = f"refs/tags/{tag}"
    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    if git("cat-file", "-t", ref) != "tag":
        raise ValueError("the release tag must be annotated")
    if git("rev-parse", f"{ref}^{{commit}}") != git("rev-parse", "HEAD"):
        raise ValueError("check out the exact tagged commit")
    branch = f"refs/remotes/{remote}/main"
    if subprocess.run(["git", "merge-base", "--is-ancestor", ref, branch]).returncode:
        raise ValueError(f"tagged commit is not on fetched {branch}")
    packages = sorted(Path("src").rglob("package.xml"))
    if not packages:
        raise ValueError("no ROS packages in the release")
    for package in packages:
        if ET.parse(package).getroot().findtext("version") != tag[1:]:
            raise ValueError(f"{package}: ROS package version must equal {tag[1:]}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 scripts/check_release.py vMAJOR.MINOR.PATCH")
    try:
        verify(sys.argv[1])
    except (ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
    print("Release provenance verified.")
