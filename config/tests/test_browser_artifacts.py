import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from config.tests.browser_artifacts import artifact_directory


class BrowserArtifactTests(TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[2] / "data/validation"
        self.root.mkdir(parents=True, exist_ok=True)

    def test_default_and_nested_override_stay_in_validation(self):
        root = self.root
        with TemporaryDirectory(dir=root) as temporary:
            with patch.dict(os.environ, {"PANDECT_BROWSER_ARTIFACTS": temporary}):
                self.assertEqual(
                    artifact_directory("unused"), Path(temporary).resolve()
                )
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    artifact_directory(Path(temporary).name), Path(temporary)
                )

    def test_absolute_traversal_sibling_and_symlink_escapes_are_rejected(self):
        root = self.root
        with TemporaryDirectory(dir=root) as temporary, TemporaryDirectory() as outside:
            link = Path(temporary) / "outside"
            link.symlink_to(outside, target_is_directory=True)
            for path in (
                outside,
                root / "../escape",
                str(root) + "-sibling",
                link / "new",
            ):
                with (
                    self.subTest(path=str(path)),
                    patch.dict(os.environ, {"PANDECT_BROWSER_ARTIFACTS": str(path)}),
                    self.assertRaises(ValueError),
                ):
                    artifact_directory("unused")
            self.assertFalse((Path(outside) / "new").exists())
