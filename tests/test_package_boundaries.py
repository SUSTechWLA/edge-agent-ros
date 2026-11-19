"""SDD-049. Check the repository's Python import edges without loading ROS or plugins."""
import ast
from importlib.util import resolve_name
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOTS = {"calibration", "evidence", "exploration", "model_provider",
                "scene", "sessions_plugin", "task_recorder"}
GROUPS = {"examples", "perception", "plugins"}
LAYERS = {"agent", "console", "edge_contracts", "analysis"}


def owner(parts):
    if not parts:
        return None
    if parts[0] in GROUPS:
        # The parent directory also contains shared simulation utilities.
        if len(parts) >= 2 and ROOT.joinpath(*parts[:2]).is_dir():
            return ".".join(parts[:2])
        return None
    if parts[0] in PLUGIN_ROOTS | LAYERS:
        return parts[0]
    return None


def forbidden(source, target):
    if not target or source == target:
        return False
    if source in {"edge_contracts", "analysis"}:
        return True
    if source in {"agent", "console"}:
        return target != "edge_contracts"
    return target not in {"agent", "edge_contracts"}


def path_value(expr, names):
    """Evaluate the repository's literal Path(__file__).parents[n] / 'dir' idiom."""
    if isinstance(expr, ast.Name):
        return names.get(expr.id)
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return Path(expr.value)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        base, suffix = path_value(expr.left, names), path_value(expr.right, names)
        if base is not None and suffix is not None:
            return base / suffix
    if isinstance(expr, ast.Call):
        if ast.unparse(expr.func) in ("str", "Path") and len(expr.args) == 1:
            return path_value(expr.args[0], names)
        if isinstance(expr.func, ast.Attribute) and expr.func.attr in ("resolve", "absolute"):
            return path_value(expr.func.value, names)
    if isinstance(expr, ast.Attribute) and expr.attr == "parent":
        base = path_value(expr.value, names)
        return base.parent if base else None
    if (isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Attribute)
            and expr.value.attr == "parents" and isinstance(expr.slice, ast.Constant)
            and type(expr.slice.value) is int):
        base = path_value(expr.value.value, names)
        if base and 0 <= expr.slice.value < len(base.parents):
            return base.parents[expr.slice.value]
    return None


def violations(relative, source):
    path = ROOT / relative
    package = ".".join(relative.parent.parts)
    layer = owner(relative.parts)
    paths = [path.parent, ROOT]
    names = {"__file__": path}
    aliases = {}
    errors = []

    def check(node, target):
        if forbidden(layer, target):
            errors.append(f"{relative}:{node.lineno} {layer} imports {target}")

    def module(node, name):
        if name.startswith("."):
            name = resolve_name(name, package)
        parts = name.split(".")
        check(node, owner(parts))
        for directory in paths:
            candidate = directory.joinpath(*parts)
            if candidate.is_dir() or candidate.with_suffix(".py").is_file():
                target = owner(candidate.relative_to(ROOT).parts)
                if target != owner(parts):
                    check(node, target)
                if layer == "edge_contracts" and target != layer:
                    errors.append(f"{relative}:{node.lineno} non-contract repository import {name}")
                return
        if layer == "edge_contracts" and parts[0] not in sys.stdlib_module_names:
            errors.append(f"{relative}:{node.lineno} external dependency {name}")

    # Source order captures the imports that follow legacy sys.path adjustments.
    for node in sorted(ast.walk(ast.parse(source)), key=lambda n: getattr(n, "lineno", 0)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = path_value(node.value, names)
        elif isinstance(node, ast.Import):
            for entry in node.names:
                module(node, entry.name)
                if entry.asname:
                    aliases[entry.asname] = entry.name
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            module(node, base)
            for entry in node.names:
                child = base + ("" if base.endswith(".") else ".") + entry.name
                aliases[entry.asname or entry.name] = child
                # 'from parent import child' can import a sibling plugin package.
                if node.level:
                    child = resolve_name(child, package)
                check(node, owner(child.split(".")))
        elif isinstance(node, ast.Call):
            first, dot, tail = ast.unparse(node.func).partition(".")
            func = aliases.get(first, first) + dot + tail
            if func in {"sys.path.append", "sys.path.insert"} and node.args:
                value = path_value(node.args[-1], names)
                if value and value.resolve().is_relative_to(ROOT):
                    directory = value.resolve()
                    check(node, owner(directory.relative_to(ROOT).parts))
                    paths.insert(0, directory)
            elif (func in {"importlib.import_module", "__import__"} and node.args
                  and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                module(node, node.args[0].value)
    return errors


class PackageBoundaries(unittest.TestCase):
    def test_repository_imports(self):
        errors = []
        for folder in sorted(PLUGIN_ROOTS | GROUPS | LAYERS):
            for path in sorted((ROOT / folder).rglob("*.py")):
                if not path.name.startswith("test_") and "tests" not in path.relative_to(ROOT).parts:
                    errors.extend(violations(path.relative_to(ROOT), path.read_text(encoding="utf-8-sig")))
        self.assertEqual([], errors)

    def test_legacy_and_package_import_spellings(self):
        for directory, source in [
            ("analysis", "from agent.kernel import ledger"),
            ("analysis", "from console.server import history"),
            ("agent", "import console.server.observer as observer"),
            ("console/server", "from agent import planning as planner"),
            ("sessions_plugin", "from evidence import public_source"),
            ("perception/rgbd", "from ..camera.frames import raw_frame as decode"),
            ("perception/rgbd", "from .. import camera as cam"),
            ("perception/rgbd", "from perception import camera"),
            ("sessions_plugin", "from importlib import import_module as load\nload('evidence.query')"),
            ("console/server", "import sys as system\nroot = Path(__file__).resolve().parents[2]\n"
                               "system.path.insert(0, str(root / 'agent'))\nfrom tracing import clean_text"),
        ]:
            with self.subTest(source=source):
                self.assertTrue(violations(Path(directory, "fixture.py"), source))


if __name__ == "__main__":
    unittest.main()
