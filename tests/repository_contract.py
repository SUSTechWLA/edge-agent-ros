"""Regression for local working trees and CI commit-range SDD checks."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_sdd import SECTIONS, check, git
from check_release import verify


class SpecCheck(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(temp.name)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "test@localhost")
        git("config", "user.name", "Contract test")
        self.spec = Path("docs/specs/SDD-007-example.md")
        self.spec.parent.mkdir(parents=True)
        self.spec.write_text("# SDD-007 Example\n" + "\n".join(f"## {s}\nAC-1 baseline\n" for s in SECTIONS), encoding="utf-8")
        Path("src").mkdir()
        Path("src/module.py").write_text("old = True\n")
        self.commit("baseline")

    def commit(self, msg):
        git("add", "-A")
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", msg], check=True)

    def test_code_and_prompt_changes_need_new_record(self):
        Path("src/module.py").unlink()
        self.assertTrue(check("HEAD"))
        git("restore", "src/module.py")
        Path("src/new-prompt.md").write_text("return evidence")
        self.assertTrue(check("HEAD"))

    def test_changed_log_and_pr_validate(self):
        Path("src/module.py").write_text("new = True")
        self.spec.write_text(self.spec.read_text(encoding="utf-8") + "verified changed runtime\n", encoding="utf-8")
        self.assertFalse(check("HEAD"))
        self.commit("change")
        self.assertTrue(check("HEAD^", "HEAD", {"title":"broken", "body":"missing reference"}))
        self.assertFalse(check("HEAD^", "HEAD", {"title":"fix(trace): keep cause", "body":"Spec: SDD-007"}))

    def test_unchanged_log_cannot_cover_code(self):
        Path("src/module.py").write_text("old = False")
        self.spec.write_text(self.spec.read_text(encoding="utf-8").replace("Example","Updated title"), encoding="utf-8")
        self.assertTrue(check("HEAD"))

    def test_docs_only_and_template_placeholders(self):
        Path("README.md").write_text("instructions")
        self.assertFalse(check("HEAD"))
        self.spec.write_text("# SDD-007 Example\n## 背景与价值\n<!-- fill me -->", encoding="utf-8")
        self.assertTrue(check("HEAD"))

    def test_release_requires_matching_package_tag_and_source_main(self):
        Path("src/package.xml").write_text("<package><version>0.1.1</version></package>")
        self.commit("prepare version")
        git("tag", "-a", "v0.1.1", "-m", "patch")
        Path("README.md").write_text("newer source main commit\n")
        self.commit("continue development")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        git("checkout", "-q", "--detach", "v0.1.1")
        verify("v0.1.1")
        git("tag", "v0.1.2")
        with self.assertRaisesRegex(ValueError, "annotated"):
            verify("v0.1.2")
        git("tag", "-a", "v0.1.3", "-m", "mismatched")
        with self.assertRaisesRegex(ValueError, "package version"):
            verify("v0.1.3")
        git("update-ref", "refs/remotes/origin/main", "HEAD^")
        git("update-ref", "refs/remotes/origin/release/0.1", "HEAD")
        with self.assertRaisesRegex(ValueError, "fetched refs/remotes/origin/main"):
            verify("v0.1.1")


if __name__ == "__main__":
    unittest.main()
