"""Unit tests for the parts of PlatePilot that do not need a running OrcaSlicer.

Run with: python -m unittest discover -s tests
"""

import importlib.util
import json
import os
import re
import struct
import sys
import tempfile
import types
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_plugin():
    orca = types.ModuleType("orca")
    orca.pages = types.SimpleNamespace(PagesPluginCapabilityBase=object)
    orca.base = object
    orca.plugin = lambda cls: cls
    orca.register_capability = lambda cls: None
    orca.host = types.SimpleNamespace(app_info=lambda: {"version": "2.5.0-dev", "language": "en"})
    sys.modules["orca"] = orca
    spec = importlib.util.spec_from_file_location("platepilot", os.path.join(ROOT, "platepilot.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pp = _load_plugin()


def box_stl(sx, sy, sz):
    """Binary STL of a box with per-facet corners, as STL exporters write them."""
    v = [(x, y, z) for x in (0, sx) for y in (0, sy) for z in (0, sz)]
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    raw = bytearray(80) + struct.pack("<I", 12)
    for a, b, c, d in quads:
        for tri in ((v[a], v[b], v[c]), (v[a], v[c], v[d])):
            raw += struct.pack("<3f", 0, 0, 0) + struct.pack("<9f", *[q for p in tri for q in p]) + b"\0\0"
    return bytes(raw)


def part(name, raw):
    info = pp._binary_stl_info(raw)
    return {"name": name, "member": name, "filetype": "stl", "dims": info["dims"], "facets": info["facets"], "included": True}


class Translations(unittest.TestCase):
    def test_every_string_has_the_same_placeholders_in_both_languages(self):
        for key, (en, ru) in pp.STRINGS.items():
            with self.subTest(key=key):
                self.assertEqual(set(re.findall(r"\{(\w+)\}", en)), set(re.findall(r"\{(\w+)\}", ru)))

    def test_every_key_used_by_the_code_exists(self):
        with open(os.path.join(ROOT, "platepilot.py"), encoding="utf-8") as stream:
            source = stream.read()
        used = set(re.findall(r'_t\("([\w.]+)"', source)) | set(re.findall(r"""\bt\('([\w.]+)'""", pp.PAGE))
        used |= set(re.findall(r'data-i18n(?:-ph|-title)?="([\w.]+)"', pp.PAGE))
        self.assertEqual(sorted(used - set(pp.STRINGS)), [])

    def test_russian_plural_forms(self):
        pp._set_language("ru")
        try:
            self.assertEqual([pp._plural("plan.plates", n) for n in (1, 3, 5, 11, 21, 22)],
                             ["1 стол", "3 стола", "5 столов", "11 столов", "21 стол", "22 стола"])
        finally:
            pp._set_language("en")

    def test_english_plural_forms(self):
        pp._set_language("en")
        self.assertEqual([pp._plural("plan.parts", n) for n in (1, 2)], ["1 part", "2 parts"])

    def test_auto_language_follows_orca(self):
        pp.orca.host.app_info = lambda: {"language": "ru_RU"}
        try:
            self.assertEqual(pp._set_language("auto"), "ru")
        finally:
            pp.orca.host.app_info = lambda: {"language": "en"}
            pp._set_language("en")


class FolderInput(unittest.TestCase):
    def test_quotes_from_copy_as_path_are_removed(self):
        folder = tempfile.gettempdir()
        self.assertEqual(pp._clean_folder_input(f'"{folder}"'), os.path.normpath(folder))

    def test_relative_path_is_rejected(self):
        with self.assertRaises(ValueError):
            pp._clean_folder_input("models/bundles")

    def test_empty_path_is_rejected(self):
        with self.assertRaises(ValueError):
            pp._clean_folder_input('  ""  ')

    def test_nested_folder_is_detected(self):
        outer = tempfile.gettempdir()
        self.assertTrue(pp._is_inside(os.path.join(outer, "a", "b"), outer))
        self.assertFalse(pp._is_inside(outer + "x", outer))


class SingleInstanceMessage(unittest.TestCase):
    def test_values_are_escaped_like_orca(self):
        self.assertEqual(pp._escape_strings_cstyle(["orca", r"C:\a b\x;y.3mf"]), r'orca;"C:\\a b\\x;y.3mf"')


class PlateGrid(unittest.TestCase):
    def test_columns_follow_orca(self):
        # compute_colum_count() in PartPlate.hpp: 1, 2, 2, 2, 3, ... 9 -> 3, 10 -> 4
        self.assertEqual([pp._plate_columns(n) for n in range(1, 11)], [1, 2, 2, 2, 3, 3, 3, 3, 3, 4])

    def test_origins_use_whole_millimetres_and_the_gap(self):
        self.assertEqual(pp._plate_origins(4, 256.7, 256.2), [(0.0, 0.0), (307.2, 0.0), (0.0, -307.2), (307.2, -307.2)])


class Packing(unittest.TestCase):
    def test_parts_keep_the_gap_from_every_bed_edge(self):
        parts = [part(f"p{i}.stl", box_stl(110, 90, 20)) for i in range(12)]
        result = pp._pack(parts, 256, 256, 4)
        self.assertEqual(result["problems"], [])
        self.assertEqual([len(p["items"]) for p in result["plates"]], [4, 4, 4])
        for plate in result["plates"]:
            for item in plate["items"]:
                self.assertGreaterEqual(item["x"], 4 - 1e-6)
                self.assertGreaterEqual(item["y"], 4 - 1e-6)
                self.assertLessEqual(item["x"] + item["w"], 252 + 1e-6)
                self.assertLessEqual(item["y"] + item["h"], 252 + 1e-6)

    def test_part_larger_than_the_bed_is_reported(self):
        result = pp._pack([part("big.stl", box_stl(300, 50, 10))], 256, 256, 4)
        self.assertEqual(result["problems"], ["big.stl"])
        self.assertEqual(result["plates"], [])

    def test_long_part_is_rotated_to_fit(self):
        result = pp._pack([part("rail.stl", box_stl(40, 240, 10))], 256, 200, 4)
        self.assertTrue(result["plates"][0]["items"][0]["rotated"])


class ProjectFile(unittest.TestCase):
    def test_project_places_every_part_inside_its_plate_cell(self):
        parts = [part(f"p{i}.stl", box_stl(110, 90, 20)) for i in range(7)]
        blobs = {p["member"]: box_stl(110, 90, 20) for p in parts}
        profile = {"size": {"x": 256.0, "y": 256.0, "origin_x": 0.0, "origin_y": 0.0}}
        plan = pp._pack(parts, 256, 256, 4)
        settings = json.dumps({"printer_settings_id": "Test printer"})
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "layout.3mf")
            pp._write_orca_project(path, plan["plates"], blobs, profile, "2.5.0", settings)
            with zipfile.ZipFile(path) as archive:
                model = archive.read("3D/3dmodel.model").decode()
                config = archive.read("Metadata/model_settings.config").decode()
                self.assertEqual(archive.read("Metadata/project_settings.config").decode(), settings)
        self.assertIn('<metadata name="Application">OrcaSlicer-2.5.0</metadata>', model)
        self.assertEqual(config.count("<plate>"), len(plan["plates"]))
        origins = pp._plate_origins(len(plan["plates"]), 256, 256)
        translations = [tuple(map(float, t.split()[9:11])) for t in re.findall(r'transform="([^"]+)"', model)]
        index = 0
        for plate_index, plate in enumerate(plan["plates"]):
            ox, oy = origins[plate_index]
            for _ in plate["items"]:
                x, y = translations[index]
                self.assertTrue(ox <= x <= ox + 256 and oy <= y <= oy + 256, (plate_index, x, y))
                index += 1

    def test_shared_vertices_are_merged(self):
        vertices, triangles = pp._indexed_mesh(box_stl(10, 10, 10))
        self.assertEqual((len(vertices), len(triangles)), (8, 12))

    def test_cli_project_settings_are_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "cli.3mf")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("3D/3dmodel.model", "<model/>")
                archive.writestr("Metadata/project_settings.config", '{"printer_settings_id": "PlatePilot machine"}')
                archive.writestr("Metadata/slice_info.config", "<config/>")
            pp._replace_project_settings(path, '{"printer_settings_id": "Real printer"}')
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(sorted(archive.namelist()), ["3D/3dmodel.model", "Metadata/project_settings.config"])
                self.assertIn("Real printer", archive.read("Metadata/project_settings.config").decode())


if __name__ == "__main__":
    unittest.main()
