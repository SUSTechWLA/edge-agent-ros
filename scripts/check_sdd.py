"""Check a Git diff and its SDD records. Run from the repository root with Python 3."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


SPEC = re.compile(r"^docs/specs/(SDD-[0-9]{3,})-[a-z0-9][a-z0-9-]*\.md$")
SECTIONS = ("背景与价值", "设计与契约", "验收与证据", "迁移与回退", "变更记录")
PR_TITLE = re.compile(r"^(feat|fix|docs|test|refactor|perf|build|ci|chore|revert)(\([\w./-]+\))?!?: \S")


def git(*args):
    return subprocess.check_output(["git", *args]).decode("utf-8")


def paths(*args):
    return set(filter(None, git(*args).split("\0")))


def read_at(ref, name):
    if ref:
        obj = subprocess.run(["git", "show", f"{ref}:{name}"], capture_output=True)
        return obj.stdout.decode("utf-8-sig") if obj.returncode == 0 else ""
    return Path(name).read_text(encoding="utf-8-sig") if Path(name).is_file() else ""


def sections(doc):
    parts = re.split(r"^## ([^\n]+)\n", doc.replace("\r\n", "\n"), flags=re.M)
    return {key.strip(): re.sub(r"<!--.*?-->", "", body, flags=re.S).strip()
            for key, body in zip(parts[1::2], parts[2::2])}


def implementation(name):
    # New runtime packages, descriptors, tests and workflows are included by default.
    return not (("/" not in name and name.endswith(".md")) or Path(name).name == "README.md"
                or name == ".github/PULL_REQUEST_TEMPLATE.md" or name.startswith(("docs/", "artifacts/", ".github/ISSUE_TEMPLATE/"))
                or name in (".gitattributes", ".gitignore", "LICENSE"))


def check(base=None, head=None, pr=None):
    files = paths("ls-tree", "-rz", "--name-only", head) if head else paths("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if base:
        changed = paths("diff", "--name-only", "-z", "--no-renames", base, *([head] if head else []), "--")
        if not head:
            changed |= paths("ls-files", "-z", "--others", "--exclude-standard")
    else:
        changed = set()

    errors, ids, updated = [], set(), set()
    for name in sorted(files):
        match = SPEC.fullmatch(name)
        if not match:
            if name.startswith("docs/specs/SDD-"):
                errors.append(f"{name}: use SDD-NNN-slug.md")
            continue
        doc = read_at(head, name)
        if not doc and not head and not Path(name).exists():
            continue
        spec_id = match.group(1)
        if spec_id in ids:
            errors.append(f"{spec_id}: duplicate identifier")
        ids.add(spec_id)
        fields = sections(doc)
        if not re.search(r"^# [^\n]*\b" + spec_id + r"\b", doc, re.M):
            errors.append(f"{name}: missing title with {spec_id}")
        for label in SECTIONS:
            if not fields.get(label):
                errors.append(f"{name}: missing content in {label}")
        if not re.search(r"\bAC-[1-9][0-9]*\b", fields.get("验收与证据", "")):
            errors.append(f"{name}: include an AC-N acceptance criterion")
        if name in changed and fields.get("变更记录") != sections(read_at(base, name)).get("变更记录"):
            updated.add(spec_id)

    if any(implementation(name) for name in changed) and not updated:
        errors.append("Implementation changed: add or update the 变更记录 of a matching docs/specs/SDD-NNN-slug.md.")
    if pr:
        if not PR_TITLE.match(pr["title"]):
            errors.append("Use a Conventional Commit PR title, e.g. feat(trace): record tool outcomes.")
        mentioned = set(re.findall(r"\bSDD-[0-9]{3,}\b", (pr.get("body") or "") + " " + pr["title"]))
        if updated - mentioned:
            errors.append("Reference each changed specification in the PR: " + ", ".join(sorted(updated - mentioned)))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="compare this ancestor with --head or with the working tree")
    parser.add_argument("--head", help="check committed files at this revision")
    parser.add_argument("--github-event", help="read pull_request/push metadata from GitHub's JSON file")
    args = parser.parse_args()
    base, head, pr = args.base, args.head, None
    if args.github_event:
        event = json.loads(Path(args.github_event).read_text(encoding="utf-8"))
        pr = event.get("pull_request")
        if pr:
            head = pr["head"]["sha"]
            base = git("merge-base", pr["base"]["sha"], head).strip()
        else:
            head = os.environ.get("GITHUB_SHA") or "HEAD"
            before = event.get("before")
            if event.get("ref", "").startswith("refs/heads/") and before and before.strip("0"):
                base = before
    errors = check(base, head, pr)
    if errors:
        sys.exit("\n".join(errors))
    print("SDD check passed.")


if __name__ == "__main__":
    main()
