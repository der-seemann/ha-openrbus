"""Offline tests for strict deployed-tree integrity checks."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import verify_deployed_tree as tree


class TreeTests(unittest.TestCase):
    def make_tree(self, root: Path) -> Path:
        component = root / "custom_components" / "openrbus"
        (component / "nested").mkdir(parents=True)
        (component / "module.py").write_text("x = 1\n")
        (component / "nested" / "data.json").write_text("{}\n")
        return component

    def test_exact_regular_tree_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.make_tree(root / "source")
            deployed = self.make_tree(root / "deployed")
            expected = {"module.py", "nested/data.json"}
            with patch.object(tree, "expected_files", return_value=expected):
                tree.verify(root, source, deployed)

    def test_cache_or_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.make_tree(root / "source")
            deployed = self.make_tree(root / "deployed")
            (deployed / "__pycache__").mkdir()
            expected = {"module.py", "nested/data.json"}
            with patch.object(tree, "expected_files", return_value=expected):
                with self.assertRaisesRegex(tree.TreeError, "deployed_tree_not_exact"):
                    tree.verify(root, source, deployed)
