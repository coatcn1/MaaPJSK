import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from project_sekai.song_identity import write_image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cooperative_template_builder', ROOT / 'scripts/build-cooperative-templates.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class CooperativeTemplateTests(unittest.TestCase):
    def build_case(self, root, optional=True, *, image=None, box=None):
        captures, output = root / 'captures', root / 'output'
        captures.mkdir()
        write_image(captures / 'required.png', np.zeros((720, 1280, 3), np.uint8))
        if image is not None:
            write_image(captures / 'optional.png', image)
        manifest = root / 'manifest.json'
        manifest.write_text(json.dumps({'threshold': .83, 'regions': {
            'required': {'capture': 'required.png', 'box': [0, 0, 10, 10]},
            'optional': {'capture': 'optional.png', 'box': box or [0, 0, 10, 10], 'optional': optional}}}), encoding='utf-8')
        builder.build(captures, output, manifest)
        return json.loads((output / 'config.json').read_text(encoding='utf-8'))

    def test_missing_explicit_optional_capture_preserves_required_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.build_case(Path(directory))
        self.assertEqual(set(result['templates']), {'required'})

    def test_missing_non_boolean_optional_or_required_capture_raises(self):
        for optional in (False, 'true', 1):
            with self.subTest(optional=optional), tempfile.TemporaryDirectory() as directory, self.assertRaises(FileNotFoundError):
                self.build_case(Path(directory), optional)

    def test_present_optional_bad_dimensions_or_crop_still_raises(self):
        for image, box in [(np.zeros((20, 20, 3), np.uint8), None),
                           (np.zeros((720, 1280, 3), np.uint8), [0, 0, 1281, 10])]:
            with self.subTest(box=box), tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                self.build_case(Path(directory), image=image, box=box)

    def test_present_optional_capture_is_generated(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.build_case(Path(directory), image=np.zeros((720, 1280, 3), np.uint8))
        self.assertEqual(set(result['templates']), {'required', 'optional'})

    def test_public_manifest_keeps_twelve_required_and_two_optional(self):
        regions = json.loads((ROOT / 'examples/cooperative-template-manifest.json').read_text(encoding='utf-8'))['regions']
        self.assertEqual(sum(region.get('optional') is not True for region in regions.values()), 12)
        self.assertIs(regions['cooperative_member_decided']['optional'], True)
        self.assertIs(regions['cooperative_shuffle']['optional'], True)
