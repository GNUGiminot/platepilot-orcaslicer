"""Unit tests for the parts of PlatePilot that do not need a running OrcaSlicer.

Run with: python -m unittest discover -s tests
"""

import base64
import importlib.util
import json
import math
import os
import re
import struct
import sys
import tempfile
import time
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


class PlateCount(unittest.TestCase):
    """A wanted number of plates: auto by default, never fewer than needed, at most one per part."""

    def layout_ok(self, result, parts, bed, margin, bed_y=None):
        bed_y = bed if bed_y is None else bed_y
        names = sorted(it["name"] for plate in result["plates"] for it in plate["items"])
        self.assertEqual(names, sorted(p["name"] for p in parts))
        dims = {p["name"]: p["dims"] for p in parts}
        for plate in result["plates"]:
            items = plate["items"]
            self.assertTrue(items)
            for it in items:
                w, h = dims[it["name"]][:2]
                self.assertEqual((it["w"], it["h"]), (h, w) if it["rotated"] else (w, h))
                self.assertGreaterEqual(min(it["x"], it["y"]), margin - 1e-6)
                self.assertLessEqual(it["x"] + it["w"], bed - margin + 1e-6)
                self.assertLessEqual(it["y"] + it["h"], bed_y - margin + 1e-6)
            for i, a in enumerate(items):
                for b in items[i + 1:]:
                    apart = (a["x"] + a["w"] + margin <= b["x"] + 1e-6 or b["x"] + b["w"] + margin <= a["x"] + 1e-6 or
                             a["y"] + a["h"] + margin <= b["y"] + 1e-6 or b["y"] + b["h"] + margin <= a["y"] + 1e-6)
                    self.assertTrue(apart, (a["name"], b["name"]))

    def test_auto_is_as_few_plates_as_before(self):
        parts = [part(f"p{i}.stl", box_stl(20, 20, 5)) for i in range(5)]
        for pack in (pp._pack_into, pp._pack_smart):
            result = pack(parts, 245, 245, 4)
            self.assertEqual((len(result["plates"]), result["min_plates"], result["count_note"]), (1, 1, None))

    def test_more_plates_spread_the_parts_evenly(self):
        parts = [part(f"p{i}.stl", box_stl(20 + 5 * i, 20, 5)) for i in range(7)]
        for pack in (pp._pack_into, pp._pack_smart):
            result = pack(parts, 245, 245, 4, 3)
            self.assertEqual(len(result["plates"]), 3, pack.__name__)
            self.assertEqual(sorted(len(p["items"]) for p in result["plates"]), [2, 2, 3], pack.__name__)
            self.assertIsNone(result["count_note"])
            self.layout_ok(result, parts, 245, 4)

    def test_fewer_plates_than_needed_keeps_the_needed_number(self):
        parts = [part(f"p{i}.stl", box_stl(110, 90, 20)) for i in range(12)]
        for pack in (pp._pack_into, pp._pack_smart):
            result = pack(parts, 256, 256, 4, 1)
            self.assertEqual((len(result["plates"]), result["min_plates"], result["count_note"]), (3, 3, "at_least"))

    def test_no_more_plates_than_parts(self):
        parts = [part(f"p{i}.stl", box_stl(30, 30, 5)) for i in range(2)]
        for pack in (pp._pack_into, pp._pack_smart):
            result = pack(parts, 245, 245, 4, 5)
            self.assertEqual((len(result["plates"]), result["count_note"]), (2, "at_most"))
            self.layout_ok(result, parts, 245, 4)

    def test_turned_parts_stay_turned_on_their_new_plate(self):
        parts = [part("rail.stl", box_stl(40, 230, 10)), part("block.stl", box_stl(30, 30, 10))]
        result = pp._pack_into(parts, 245, 180, 4, 2)
        self.assertEqual(len(result["plates"]), 2)
        self.layout_ok(result, parts, 245, 4, 180)
        self.assertTrue(any(it["rotated"] for plate in result["plates"] for it in plate["items"] if it["name"] == "rail.stl"))


class SmartPacking(unittest.TestCase):
    def check(self, parts, bed_x, bed_y, margin):
        """Smart layout keeps every rule of the plain one: same parts, no more plates, inside
        the bed with the gap, the gap between parts, and sizes that match the part."""
        plain = pp._pack(parts, bed_x, bed_y, margin)
        smart = pp._pack_smart(parts, bed_x, bed_y, margin)
        self.assertEqual(smart["smart"], True)
        self.assertLessEqual(len(smart["plates"]), len(plain["plates"]))
        names = sorted(item["name"] for plate in smart["plates"] for item in plate["items"])
        self.assertEqual(names, sorted(p["name"] for p in parts))
        dims = {p["name"]: p["dims"] for p in parts}
        for plate in smart["plates"]:
            items = plate["items"]
            for item in items:
                w, h = dims[item["name"]][:2]
                self.assertEqual((item["w"], item["h"]), (h, w) if item["rotated"] else (w, h))
                self.assertGreaterEqual(min(item["x"], item["y"]), margin - 1e-6)
                self.assertLessEqual(item["x"] + item["w"], bed_x - margin + 1e-6)
                self.assertLessEqual(item["y"] + item["h"], bed_y - margin + 1e-6)
            for i, a in enumerate(items):
                for b in items[i + 1:]:
                    apart = max(b["x"] - a["x"] - a["w"], a["x"] - b["x"] - b["w"], b["y"] - a["y"] - a["h"], a["y"] - b["y"] - b["h"])
                    self.assertGreaterEqual(apart, margin - 1e-6)
        return plain, smart

    def test_parts_are_centred_and_away_from_the_edges(self):
        parts = [part(f"p{i}.stl", box_stl(30 + i * 3, 60 - i * 2, 10)) for i in range(7)]
        plain, smart = self.check(parts, 245, 245, 4)
        items = smart["plates"][0]["items"]
        left, right = min(i["x"] for i in items), max(i["x"] + i["w"] for i in items)
        front, back = min(i["y"] for i in items), max(i["y"] + i["h"] for i in items)
        self.assertAlmostEqual(left + right, 245, places=6)
        self.assertAlmostEqual(front + back, 245, places=6)
        self.assertGreaterEqual(min(left, front), 10)
        self.assertEqual(min(min(i["x"], i["y"]) for i in plain["plates"][0]["items"]), 4)

    def test_single_part_goes_to_the_middle(self):
        _, smart = self.check([part("a.stl", box_stl(20, 30, 5))], 220, 220, 4)
        item = smart["plates"][0]["items"][0]
        self.assertAlmostEqual(item["x"] + item["w"] / 2, 110, places=6)
        self.assertAlmostEqual(item["y"] + item["h"] / 2, 110, places=6)

    def test_small_parts_sit_nearer_the_centre_than_large_ones(self):
        parts = [part(f"big{i}.stl", box_stl(70, 50, 10)) for i in range(3)] + [part(f"small{i}.stl", box_stl(12, 12, 5)) for i in range(3)]
        _, smart = self.check(parts, 245, 245, 4)
        distance = {i["name"]: math.hypot(i["x"] + i["w"] / 2 - 122.5, i["y"] + i["h"] / 2 - 122.5) for i in smart["plates"][0]["items"]}
        self.assertLess(max(v for k, v in distance.items() if k.startswith("small")), max(v for k, v in distance.items() if k.startswith("big")))

    def test_tall_part_takes_the_middle(self):
        parts = [part(f"low{i}.stl", box_stl(30, 30, 8)) for i in range(4)] + [part("tower.stl", box_stl(30, 30, 150))]
        _, smart = self.check(parts, 245, 245, 4)
        distance = {i["name"]: math.hypot(i["x"] + i["w"] / 2 - 122.5, i["y"] + i["h"] / 2 - 122.5) for i in smart["plates"][0]["items"]}
        self.assertEqual(min(distance, key=distance.get), "tower.stl")

    def test_full_plates_and_many_parts_keep_the_rules(self):
        self.check([part(f"p{i}.stl", box_stl(110, 90, 20)) for i in range(12)], 256, 256, 4)
        self.check([part(f"p{i}.stl", box_stl(15 + i % 7 * 9, 12 + i % 5 * 11, 5)) for i in range(40)], 220, 220, 2)
        self.check([part(f"p{i}.stl", box_stl(50, 40, 5)) for i in range(9)], 180, 180, 0)

    def test_parts_that_do_not_fit_return_the_plain_result(self):
        smart = pp._pack_smart([part("big.stl", box_stl(300, 50, 10))], 256, 256, 4)
        self.assertEqual((smart["problems"], smart["smart"]), (["big.stl"], "fallback"))


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

    def test_part_longer_than_the_bed_is_turned_diagonally(self):
        raw = box_stl(300, 30, 10)
        long_part = dict(part("rail.stl", raw), source_dims=pp._binary_stl_info(raw)["dims"])
        turned = pp._turn_to_fit([long_part], {"rail.stl": raw}, 256, 256, 4)[0]
        self.assertTrue(0 < turned["angle"] < 180)
        self.assertTrue(max(turned["dims"][:2]) <= 248 + 1e-6, turned["dims"])
        result = pp._pack([turned], 256, 256, 4)
        self.assertEqual((len(result["plates"]), result["problems"]), (1, []))
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "p.3mf")
            profile = {"size": {"x": 256, "y": 256, "origin_x": 0, "origin_y": 0}}
            pp._write_orca_project(path, result["plates"], {"rail.stl": raw}, profile, "2.5.0", "{}")
            with zipfile.ZipFile(path) as archive:
                model = archive.read("3D/3dmodel.model").decode()
        xs = [float(v) for v in re.findall(r'<vertex x="([-\d.]+)"', model)]
        ys = [float(v) for v in re.findall(r'<vertex x="[-\d.]+" y="([-\d.]+)"', model)]
        tx, ty = (float(v) for v in re.search(r'transform="1 0 0 0 1 0 0 0 1 ([-\d.]+) ([-\d.]+) 0"', model).groups())
        self.assertTrue(4 - 1e-3 <= tx + min(xs) and tx + max(xs) <= 252 + 1e-3, (tx, min(xs), max(xs)))
        self.assertTrue(4 - 1e-3 <= ty + min(ys) and ty + max(ys) <= 252 + 1e-3, (ty, min(ys), max(ys)))

    def test_part_that_fits_is_not_turned(self):
        raw = box_stl(100, 30, 10)
        self.assertNotIn("angle", pp._turn_to_fit([part("a.stl", raw)], {"a.stl": raw}, 256, 256, 4)[0])

    def test_parts_orca_could_not_place_are_counted(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "00000.log"), "w") as stream:
                stream.write("x\nPartPlateList::postprocess_bed_index_for_selected: can not be arranged inside plate!\n" * 2)
            with open(os.path.join(folder, "orca-cli.log"), "w") as stream:
                stream.write("can not be arranged inside plate")
            self.assertEqual(pp._cli_unplaced(folder), 2)

    def test_shared_vertices_are_merged(self):
        vertices, triangles = pp._indexed_mesh(box_stl(10, 10, 10))
        self.assertEqual((len(vertices), len(triangles)), (8, 12))

    def test_non_finite_corners_are_refused(self):
        raw = bytearray(box_stl(10, 10, 10))
        struct.pack_into("<f", raw, 84 + 12, float("nan"))
        with self.assertRaises(ValueError):
            pp._indexed_mesh(bytes(raw))
        with self.assertRaises(ValueError):
            pp._binary_stl_info(bytes(raw))

    def test_dimensions_count_facets_at_even_and_odd_positions(self):
        # Corners are read as two strided views, one for even records and one for odd ones.
        far = struct.pack("<3f", 0, 0, 0) + struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 40) + b"\0\0"
        near = struct.pack("<3f", 0, 0, 0) + struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0) + b"\0\0"
        box = box_stl(10, 20, 30)
        for extra in ([far], [near, far]):
            raw = box[:80] + struct.pack("<I", 12 + len(extra)) + box[84:] + b"".join(extra)
            self.assertEqual(pp._binary_stl_info(raw)["dims"], [10, 20, 40])

    def test_small_part_preview_has_every_facet(self):
        raw = box_stl(10, 20, 30)
        info = pp._binary_stl_info(raw, include_preview=True)
        corners = [v for t in pp._iter_stl_triangles(raw) for v in t]
        self.assertEqual(base64.b64decode(info["vertices"]), struct.pack("<%sf" % len(corners), *corners))

    def test_large_part_preview_is_simplified_as_a_whole(self):
        # A wavy 200 × 50 mm sheet of 2·130·130 facets, written from left to right: the first
        # 10 000 facets of the file cover only the left part of it.
        n = 130
        point = lambda i, j: (i * 200 / n, j * 50 / n, 3 * math.sin(i / 7) * math.cos(j / 5))
        raw = bytearray(80) + struct.pack("<I", 2 * n * n)
        for i in range(n):
            for j in range(n):
                for tri in ((point(i, j), point(i + 1, j), point(i + 1, j + 1)), (point(i, j), point(i + 1, j + 1), point(i, j + 1))):
                    raw += struct.pack("<3f", 0, 0, 0) + struct.pack("<9f", *[q for p in tri for q in p]) + b"\0\0"
        info = pp._binary_stl_info(bytes(raw), include_preview=True, max_facets=10000)
        self.assertEqual(info["facets"], 2 * n * n)
        corners = struct.unpack("<%sf" % (len(base64.b64decode(info["vertices"])) // 4), base64.b64decode(info["vertices"]))
        self.assertLessEqual(len(corners) // 9, 10000)
        self.assertGreater(len(corners) // 9, 10000 // 4)
        xs = corners[0::3]
        self.assertLess(min(xs), 2)
        self.assertGreater(max(xs), 198)
        # Within the limit every facet is sent as it is.
        full = pp._binary_stl_info(bytes(raw), include_preview=True)
        self.assertEqual(len(base64.b64decode(full["vertices"])), 2 * n * n * 36)

    def test_cli_project_settings_are_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "cli.3mf")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("3D/3dmodel.model", "<model/>")
                archive.writestr("Metadata/project_settings.config", '{"printer_settings_id": "PlatePilot machine"}')
                archive.writestr("Metadata/slice_info.config", "<config/>")
            pp._finish_cli_project(path, '{"printer_settings_id": "Real printer"}')
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(sorted(archive.namelist()), ["3D/3dmodel.model", "Metadata/project_settings.config"])
                self.assertIn("Real printer", archive.read("Metadata/project_settings.config").decode())

    def test_cli_objects_get_their_part_names_back(self):
        config = ('<config><object id="2"><metadata key="name" value="001.stl"/>'
                  '<part id="1" subtype="normal_part"><metadata key="name" value="001.stl"/></part></object>'
                  '<object id="4"><metadata key="name" value="002.stl"/></object></config>')
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "cli.3mf")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Metadata/model_settings.config", config)
            pp._finish_cli_project(path, "{}", {"001.stl": 'left wall "A" & B.stl', "002.stl": "деталь 2.stl"})
            with zipfile.ZipFile(path) as archive:
                text = archive.read("Metadata/model_settings.config").decode()
        self.assertEqual(text.count("left wall &quot;A&quot; &amp; B.stl"), 2)
        self.assertIn('value="деталь 2.stl"', text)
        self.assertNotIn("001.stl", text)

    def test_cli_input_paths_stay_short(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pp.PlatePilotPage._write_native_input(folder, 9, b"solid")
        self.assertEqual(os.path.basename(path), "009.stl")


# Orca asks before a plugin opens a file outside its own data folder (Python's "open" audit
# event), so the tests record which files the plugin opens.
OPENED = []
sys.addaudithook(lambda event, args: OPENED.append(os.fspath(args[0])) if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)) else None)


def write_tree(root, files):
    for rel, data in files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as stream:
            stream.write(data)


class Library(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        with zipfile.ZipFile(os.path.join(self.root, "bundle.zip"), "w") as archive:
            archive.writestr("part.stl", box_stl(10, 10, 10))
        write_tree(self.root, {"loose/a.stl": box_stl(5, 5, 5), "loose/b.step": b"step", "other/readme.txt": b"x",
                               "deep/inner/c.stl": box_stl(3, 3, 3)})

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_finds_sources_without_opening_any_file(self):
        del OPENED[:]
        rows, truncated, _ = pp._scan_folder(self.root)
        opened = [p for p in OPENED if pp._is_inside(p, self.root)]
        self.assertEqual(opened, [])
        self.assertFalse(truncated)
        self.assertEqual(sorted((r["kind"], r["name"]) for r in rows),
                         [("folder", os.path.join("deep", "inner")), ("folder", "loose"), ("zip", "bundle.zip")])
        loose = next(r for r in rows if r["name"] == "loose")
        self.assertEqual((loose["count"], loose["type_counts"]["step"], loose["files"]), (2, 1, ["a.stl", "b.step"]))
        self.assertIsNone(next(r for r in rows if r["kind"] == "zip")["type_counts"])

    def test_scan_without_subfolders(self):
        rows, _, _ = pp._scan_folder(self.root, recursive=False)
        self.assertEqual([r["name"] for r in rows], ["bundle.zip"])

    def test_first_scan_marks_nothing_new_and_later_additions_are_new(self):
        state = {}
        rows, _, _ = pp._scan_folder(self.root)
        summary = pp._update_library(state, self.root, rows, False, first_scan=True, now=1)
        # bundle.zip was never opened and its name says nothing about models: listed apart.
        self.assertEqual((summary["sources"], summary["other"], summary["new"]), (2, 1, 0))
        write_tree(self.root, {"fresh/d.stl": box_stl(4, 4, 4)})
        rows, _, _ = pp._scan_folder(self.root)
        summary = pp._update_library(state, self.root, rows, False, first_scan=False, now=2)
        self.assertEqual([r["name"] for r in rows if r["new"]], ["fresh"])
        self.assertEqual(summary["new"], 1)

    def test_freshness_is_the_later_of_modified_and_created(self):
        self.assertEqual(pp._freshness(types.SimpleNamespace(st_mtime=100.0, st_ctime=250.0)), 250.0)
        self.assertEqual(pp._freshness(types.SimpleNamespace(st_mtime=300.0, st_ctime=250.0)), 300.0)

    def test_a_pasted_copy_counts_as_fresh_though_it_keeps_an_old_date(self):
        started = time.time() - 2
        write_tree(self.root, {"pasted/e.stl": box_stl(4, 4, 4)})
        old = time.mktime((2001, 1, 1, 0, 0, 0, 0, 0, -1))
        os.utime(os.path.join(self.root, "pasted", "e.stl"), (old, old))   # what a copy in Explorer keeps
        rows, _, _ = pp._scan_folder(self.root)
        pasted = next(r for r in rows if r["name"] == "pasted")
        self.assertGreater(pasted["time"], started)

    def test_opened_archive_counts_are_reused_and_empty_archives_counted(self):
        zip_path = os.path.join(self.root, "bundle.zip")
        rows, _, _ = pp._scan_folder(self.root)
        fingerprint = next(r for r in rows if r["kind"] == "zip")["fingerprint"]
        state = {"zips": {zip_path: {"fingerprint": fingerprint, "counts": {"stl": 0, "step": 0, "3mf": 0}}}}
        summary = pp._update_library(state, self.root, rows, False, first_scan=True)
        self.assertEqual(next(r for r in rows if r["kind"] == "zip")["count"], 0)
        self.assertEqual((summary["empty"], summary["sources"]), (1, 2))

    def test_sources_gone_from_disk_are_forgotten(self):
        gone = os.path.join(self.root, "gone.zip")
        state = {"known": {gone: "1:1"}, "zips": {gone: {"fingerprint": "1:1", "counts": {}}}}
        rows, _, _ = pp._scan_folder(self.root)
        pp._update_library(state, self.root, rows, False, first_scan=False)
        self.assertNotIn(gone, state["known"])
        self.assertNotIn(gone, state["zips"])

    def test_folder_copy_is_made_once_and_reused(self):
        source = os.path.join(self.root, "loose")
        cache = os.path.join(self.root, "cache")
        os.makedirs(cache)
        copy = pp._snapshot_folder(source, ["a.stl"], cache)
        self.assertEqual(sorted(os.listdir(copy)), ["a.stl", "manifest.json"])
        del OPENED[:]
        self.assertEqual(pp._snapshot_folder(source, ["a.stl"], cache), copy)
        self.assertNotIn(os.path.join(source, "a.stl"), OPENED)
        parts = pp._read_loose_folder(copy, copy, (("b.step", ""),))
        self.assertEqual([(p["name"], p["filetype"]) for p in parts], [("a.stl", "stl"), ("b.step", "step")])


class ArchiveNames(unittest.TestCase):
    def test_model_downloads_are_recognized_by_name(self):
        for name in ("Balcony+stopper+v2_stls.zip", "Shopping Bag Handle - 597043.zip", "minecraft-inventory-model_files.zip",
                     "Complete+Bed+Shroud+Upgrade+-+QIDI+Q1+Pro.zip", "LED+Board+Rahmen+v2.1_stls.zip", "Filament+Spool+Holder-25.4_stls.zip"):
            self.assertEqual(pp._guess_archive(name), "models", name)

    def test_programs_and_drivers_are_not_taken_for_models(self):
        for name in ("CH340_drivers-Linux-Mac-Windows-master.zip", "QIDISlicer_1.2.3_Win64.zip", "OrcaSlicer_Windows_x64_nightly_portable.zip",
                     "v2rayN-windows-64-desktop.zip", "cmake-4.1.2.zip", "ch341.driver-main.zip"):
            self.assertEqual(pp._guess_archive(name), "other", name)

    def test_opened_archive_is_judged_by_its_contents(self):
        row = {"kind": "zip", "name": "00apt.ZIP", "type_counts": None}
        self.assertFalse(pp._likely_models(row))
        self.assertTrue(pp._likely_models(dict(row, type_counts={"stl": 2, "step": 0, "3mf": 0})))
        self.assertFalse(pp._likely_models(dict(row, name="part_stls.zip", type_counts={"stl": 0, "step": 0, "3mf": 0})))


class DroppedNames(unittest.TestCase):
    def test_names_orca_or_windows_refuse_are_changed(self):
        self.assertEqual(pp._safe_name("config"), "config_")
        self.assertEqual(pp._safe_name("CON.stl"), "CON_.stl")
        self.assertEqual(pp._safe_name('a<b>:c?.STL'), "a_b__c_.stl")
        self.assertEqual(pp._safe_name(".."), "file")

    def test_non_latin_names_become_latin(self):
        self.assertEqual(pp._safe_name("Новая папка"), "Novaya papka")
        self.assertEqual(pp._safe_name("Деталь №2.STL"), "Detal _2.stl")
        self.assertEqual(pp._safe_name("蛋架子.stl"), "___.stl")
        for name in ("Новая папка", "Шестерня.stl", "ёжик", "蛋架子"):
            self.assertTrue(pp._orca_can_read(os.path.join("C:\\data", pp._safe_name(name))), name)


class OrcaPathCheck(unittest.TestCase):
    """Orca 2.5's permission check fails on non-Latin path components and refuses some names."""

    def test_paths_orca_can_check(self):
        self.assertTrue(pp._orca_can_read(r"G:\Download\models\part.stl"))
        for path in (r"G:\Download\Новая папка", r"G:\Download\old\config", r"G:\a\Certs\x.zip", r"G:\a\b.ini", "G:\\Download\\蛋.stl"):
            self.assertFalse(pp._orca_can_read(path), path)

    def test_which_orca_handles_non_latin_names(self):
        for version, fixed in (("2.5.0", True), ("2.5.1", True), ("2.6.0-dev", True), ("2.5.0-dev", False),
                               ("2.5.0-beta", False), ("2.4.2", False), ("", False), (None, False)):
            self.assertEqual(pp._version_handles_non_latin(version), fixed, version)

    def test_orca_2_5_0_lists_non_latin_paths_and_still_refuses_secret_names(self):
        saved = getattr(pp.orca.host, "app_info", None)
        pp.orca.host.app_info = lambda: {"version": "2.5.0"}
        pp._NON_LATIN_OK.clear()
        try:
            for path in (r"G:\Download\Новая папка", "G:\\Download\\蛋.stl"):
                self.assertTrue(pp._orca_can_read(path), path)
            for path in (r"G:\Download\Новая папка\config", r"G:\a\Certs\x.zip", r"G:\a\b.ini"):
                self.assertFalse(pp._orca_can_read(path), path)
            with tempfile.TemporaryDirectory() as root:
                root = os.path.realpath(root)
                write_tree(root, {"Новая папка/b.stl": b"x"})
                rows, _, skipped = pp._scan_folder(root)
            self.assertEqual((skipped, [r["name"] for r in rows]), (0, ["Новая папка"]))
        finally:
            pp._NON_LATIN_OK.clear()
            if saved is None:
                del pp.orca.host.app_info
            else:
                pp.orca.host.app_info = saved

    def test_scan_does_not_enter_folders_orca_cannot_check(self):
        with tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            write_tree(root, {"ok/a.stl": b"x", "Новая папка/b.stl": b"x", "old/config/c.stl": b"x", "ok/деталь.stl": b"x"})
            with zipfile.ZipFile(os.path.join(root, "архив.zip"), "w") as archive:
                archive.writestr("p.stl", b"x")
            rows, _, skipped = pp._scan_folder(root)
        self.assertEqual(skipped, 2)
        self.assertEqual(sorted(r["name"] for r in rows), ["ok", "архив.zip"])
        self.assertTrue(next(r for r in rows if r["kind"] == "zip")["warning"])

    def test_unsafe_sources_are_not_opened(self):
        with tempfile.TemporaryDirectory() as storage, tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            write_tree(root, {"ok/a.stl": box_stl(5, 5, 5), "ok/деталь.stl": box_stl(5, 5, 5)})
            with zipfile.ZipFile(os.path.join(root, "архив.zip"), "w") as archive:
                archive.writestr("p.stl", box_stl(5, 5, 5))
            page = FakePage(storage)
            page.config_text = json.dumps({"folders": [root]})
            page.send({"type": "scan"})
            del OPENED[:]
            with self.assertRaises(AssertionError):
                page.send({"type": "open_model", "path": os.path.join(root, "архив.zip"), "kind": "zip"})
            page.messages.clear()
            page.send({"type": "open_model", "path": os.path.join(root, "ok"), "kind": "folder"})
            opened = [p for p in OPENED if pp._is_inside(p, root)]
            self.assertFalse(any(not pp._orca_can_read(p) for p in opened), opened)
            parts = next(m for m in page.messages if m.get("type") == "archive")["parts"]
            self.assertEqual([(p["name"], bool(p.get("blocked"))) for p in parts], [("a.stl", False), ("деталь.stl", True)])

    def test_library_lists_every_source_and_only_hides_hidden_ones(self):
        with tempfile.TemporaryDirectory() as storage, tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            write_tree(root, {"loose/a.stl": box_stl(5, 5, 5), "old/b.stl": box_stl(5, 5, 5)})
            for name in ("one.zip", "two.zip"):
                with zipfile.ZipFile(os.path.join(root, name), "w") as archive:
                    archive.writestr("p.stl", box_stl(5, 5, 5))
            page = FakePage(storage)
            page.config_text = json.dumps({"folders": [root], "hidden": {os.path.join(root, "old"): ""}})
            page.send({"type": "scan"})
            library = [m for m in page.messages if m.get("type") == "archives"][-1]
            self.assertEqual(sorted(a["name"] for a in library["archives"]), ["loose", "one.zip", "two.zip"])
            page.send({"type": "hide_source", "path": os.path.join(root, "one.zip")})
            library = [m for m in page.messages if m.get("type") == "archives"][-1]
            self.assertEqual(sorted(a["name"] for a in library["archives"]), ["loose", "two.zip"])

    def test_settings_of_0_4_1_carry_over(self):
        """An Orca Cloud update keeps the plugin's settings; 0.4.1 stored more keys than 0.5 uses."""
        with tempfile.TemporaryDirectory() as storage, tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            write_tree(root, {"kept/a.stl": box_stl(5, 5, 5), "hidden/b.stl": box_stl(5, 5, 5)})
            with zipfile.ZipFile(os.path.join(root, "part_stls.zip"), "w") as archive:
                archive.writestr("p.stl", box_stl(5, 5, 5))
            old = {"folders": [root + os.sep], "folder_grants": [root], "hidden": {os.path.join(root, "hidden"): ""},
                   "auto_refresh": True, "poll_seconds": 60, "recursive": True, "batch_size": 10, "margin_mm": 3.0,
                   "model_types": ["stl", "step", "3mf"], "arrange_mode": "orca", "language": "ru"}
            page = FakePage(storage)
            page.config_text = json.dumps(old)
            config = page._config()
            for key in ("folder_grants", "auto_refresh", "poll_seconds", "batch_size"):
                self.assertNotIn(key, config)
            self.assertEqual((config["folders"], config["margin_mm"], config["arrange_mode"], config["language"], config["check_on_open"]),
                             ([root], 3.0, "orca", "ru", True))
            page.send({"type": "scan"})
            library = [m for m in page.messages if m.get("type") == "archives"][-1]
            self.assertEqual(sorted(a["name"] for a in library["archives"]), ["kept", "part_stls.zip"])
            self.assertFalse(any(a["new"] for a in library["archives"]))

    def test_unsafe_folder_is_not_added(self):
        with tempfile.TemporaryDirectory() as storage, tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "Модели")
            os.makedirs(folder)
            page = FakePage(storage)
            with self.assertRaises(AssertionError):
                page.send({"type": "request_folder_add", "path": folder})
            self.assertEqual(json.loads(page.config_text).get("folders", []), [])

    def test_taken_names_get_a_number(self):
        with tempfile.TemporaryDirectory() as folder:
            write_tree(folder, {"part.stl": b"x"})
            self.assertEqual(os.path.basename(pp._unique_path(folder, "part.stl")), "part (2).stl")


class FakePage(pp.PlatePilotPage):
    def __init__(self, storage):
        self.messages = []
        self.config_text = "{}"
        pp.orca.host.plugin = types.SimpleNamespace(storage=lambda: storage)

    def get_config(self):
        return self.config_text

    def save_config(self, text):
        self.config_text = text

    def post_message(self, message):
        self.messages.append(message)

    def send(self, message):
        self.on_message(message)
        errors = [m["message"] for m in self.messages if m.get("type") == "error"]
        if errors:
            raise AssertionError(errors)
        return self.messages[-1] if self.messages else None


def box_model(sx, sy, sz, object_id="1", name="box"):
    """A 3MF <object> holding a box mesh."""
    v = [(x, y, z) for x in (0, sx) for y in (0, sy) for z in (0, sz)]
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    tris = [t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))]
    return (f'<object id="{object_id}" name="{name}" type="model"><mesh><vertices>'
            + "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in v)
            + "</vertices><triangles>" + "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
            + "</triangles></mesh></object>")


CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


def threemf(model, extra=None, unit="millimeter"):
    """Bytes of a 3MF whose root model has `model` inside <model> (resources and build)."""
    buffer = __import__("io").BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
                                        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        archive.writestr("3D/3dmodel.model", f'<model unit="{unit}" xmlns="{CORE}" xmlns:p="{PROD}">{model}</model>')
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
    return buffer.getvalue()


class ThreeMF(unittest.TestCase):
    def dims(self, stl):
        return pp._binary_stl_info(stl)["dims"]

    def test_one_object_placed_and_scaled(self):
        raw = threemf("<resources>" + box_model(10, 20, 5) + "</resources>"
                      '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 100 50 0"/></build>')
        items = pp._threemf_parts(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][0], "box")
        self.assertEqual(self.dims(items[0][1]), [10.0, 20.0, 5.0])
        inch = pp._threemf_parts(threemf("<resources>" + box_model(1, 2, 1) + '</resources><build><item objectid="1"/></build>', unit="inch"))
        self.assertEqual([round(v, 3) for v in self.dims(inch[0][1])], [25.4, 50.8, 25.4])

    def test_rotation_is_applied(self):
        # A quarter turn about Z: 10 x 20 becomes 20 x 10.
        raw = threemf("<resources>" + box_model(10, 20, 5) + '</resources><build><item objectid="1" transform="0 1 0 -1 0 0 0 0 1 0 0 0"/></build>')
        self.assertEqual([round(v, 4) for v in self.dims(pp._threemf_parts(raw)[0][1])], [20.0, 10.0, 5.0])

    def test_components_from_another_model_file_like_bambu_and_orca(self):
        part_file = f'<model unit="millimeter" xmlns="{CORE}"><resources>' + box_model(10, 10, 10, "7") + "</resources><build/></model>"
        raw = threemf('<resources><object id="2" name="pair" type="model"><components>'
                      '<component p:path="/3D/Objects/object_1.model" objectid="7" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
                      '<component p:path="/3D/Objects/object_1.model" objectid="7" transform="1 0 0 0 1 0 0 0 1 30 0 0"/>'
                      '</components></object></resources><build><item objectid="2"/></build>',
                      {"3D/Objects/object_1.model": part_file})
        items = pp._threemf_parts(raw)
        self.assertEqual([(n, self.dims(s)) for n, s in items], [("pair", [40.0, 10.0, 10.0])])

    def test_several_objects_become_several_parts(self):
        raw = threemf("<resources>" + box_model(10, 10, 10, "1", "left") + box_model(20, 5, 5, "2", "right") + "</resources>"
                      '<build><item objectid="1"/><item objectid="2" transform="1 0 0 0 1 0 0 0 1 200 0 0"/></build>')
        rows = pp._threemf_part_rows("kit.3mf", "kit.3mf", pp._threemf_parts(raw))
        self.assertEqual([(r["name"], r["member"], r["dims"]) for r in rows],
                         [("kit.3mf · left", "kit.3mf#0", [10.0, 10.0, 10.0]), ("kit.3mf · right", "kit.3mf#1", [20.0, 5.0, 5.0])])
        self.assertEqual(pp._split_member("kit.3mf#1"), ("kit.3mf", 1))
        self.assertEqual(pp._split_member("part#2.stl"), ("part#2.stl", None))

    def test_a_broken_3mf_is_listed_with_its_error(self):
        rows = pp._threemf_rows_or_warning("bad.3mf", "bad.3mf", ("t", "bad"), lambda: b"not a zip")
        self.assertTrue(rows[0]["blocked"])
        self.assertTrue(rows[0]["warning"])

    def test_an_archive_with_a_3mf_previews_lays_out_and_exports(self):
        raw = threemf("<resources>" + box_model(30, 20, 10, "1", "a") + box_model(15, 15, 15, "2", "b") + "</resources>"
                      '<build><item objectid="1"/><item objectid="2"/></build>')
        with tempfile.TemporaryDirectory() as storage, tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            with zipfile.ZipFile(os.path.join(root, "kit.zip"), "w") as archive:
                archive.writestr("kit.3mf", raw)
                archive.writestr("bracket.step", b"ISO-10303-21;")
            page = FakePage(storage)
            page.config_text = json.dumps({"folders": [root]})
            page.send({"type": "scan"})
            page.send({"type": "open_model", "path": os.path.join(root, "kit.zip"), "kind": "zip"})
            parts = next(m for m in page.messages if m.get("type") == "archive")["parts"]
            by_name = {p["name"]: p for p in parts}
            self.assertEqual(by_name["kit.3mf · a"]["filetype"], "3mf")
            self.assertTrue(by_name["kit.3mf · a"]["included"])
            self.assertTrue(by_name["bracket.step"]["open_in_orca"])
            index = [p["name"] for p in parts].index("kit.3mf · b")
            preview = page.send({"type": "preview_part", "index": index})
            self.assertEqual(preview["type"], "preview")
            self.assertEqual([round(v, 3) for v in preview["dims"]], [15.0, 15.0, 15.0])
            members = [p["member"] for p in parts if p["filetype"] == "3mf"]
            saved = pp._get_profile
            pp._get_profile = lambda: {"name": "Test printer", "size": {"x": 245, "y": 245}, "height": 250}
            try:
                plan = page.send({"type": "plan", "margin": 4, "included": members})
            finally:
                pp._get_profile = saved
            self.assertEqual(plan["type"], "plan")
            self.assertEqual(sorted(it["name"] for plate in plan["plates"] for it in plate["items"]), ["kit.3mf · a", "kit.3mf · b"])
            blobs = page._read_stl_blobs(page._selected_source, page._current_parts(page._config()), members)
            self.assertEqual([pp._binary_stl_info(blobs[m])["dims"] for m in members], [[30.0, 20.0, 10.0], [15.0, 15.0, 15.0]])

    def test_a_step_part_is_handed_to_orca(self):
        sent = []
        saved = pp._send_to_own_window
        pp._send_to_own_window = sent.append
        try:
            with tempfile.TemporaryDirectory() as storage, tempfile.TemporaryDirectory() as root:
                root = os.path.realpath(root)
                with zipfile.ZipFile(os.path.join(root, "kit.zip"), "w") as archive:
                    archive.writestr("bracket.step", b"ISO-10303-21; step data")
                page = FakePage(storage)
                page.config_text = json.dumps({"folders": [root]})
                page.send({"type": "scan"})
                page.send({"type": "open_model", "path": os.path.join(root, "kit.zip"), "kind": "zip"})
                parts = next(m for m in page.messages if m.get("type") == "archive")["parts"]
                if os.name == "nt":
                    page.send({"type": "open_in_orca", "index": 0})
                    self.assertEqual(len(sent), 1)
                    with open(sent[0], "rb") as stream:
                        self.assertEqual(stream.read(), b"ISO-10303-21; step data")
                    self.assertTrue(pp._is_inside(sent[0], storage))
                else:
                    with self.assertRaises(AssertionError):
                        page.send({"type": "open_in_orca", "index": 0})
                self.assertEqual(parts[0]["filetype"], "step")
        finally:
            pp._send_to_own_window = saved


class ProjectName(unittest.TestCase):
    """Projects are named after their source and reused while Orca has not changed them."""

    def test_names_from_sources(self):
        self.assertEqual(pp._project_base("Dragon set.zip"), "Dragon set")
        self.assertEqual(pp._project_base("G:\\Download\\Gears v2\\"), "Gears v2")
        self.assertEqual(pp._project_base('a<b>:c?.zip'), "a_b__c_")
        self.assertEqual(pp._project_base(""), "PlatePilot")
        saved = getattr(pp.orca.host, "app_info", None)
        try:
            pp._NON_LATIN_OK.clear()
            pp.orca.host.app_info = lambda: {"version": "2.4.2"}
            self.assertEqual(pp._project_base("Дракон.zip"), "Drakon")
            pp._NON_LATIN_OK.clear()
            pp.orca.host.app_info = lambda: {"version": "2.5.0"}
            self.assertEqual(pp._project_base("Дракон.zip"), "Дракон")
        finally:
            pp._NON_LATIN_OK.clear()
            if saved is None:
                del pp.orca.host.app_info
            else:
                pp.orca.host.app_info = saved

    def test_export_uses_the_source_name_and_reuses_it_until_orca_changes_it(self):
        saved = (pp._get_profile, pp._project_settings, pp._app_version, pp._open_project)
        opened = []
        pp._get_profile = lambda: {"name": "Test printer", "size": {"x": 245, "y": 245}, "height": 250}
        pp._project_settings = lambda: "{}"
        pp._app_version = lambda: "2.5.0"
        pp._open_project = opened.append
        try:
            with tempfile.TemporaryDirectory() as storage, tempfile.TemporaryDirectory() as root:
                root = os.path.realpath(root)
                with zipfile.ZipFile(os.path.join(root, "Dragon set.zip"), "w") as archive:
                    archive.writestr("body.stl", box_stl(30, 20, 10))
                page = FakePage(storage)
                page.config_text = json.dumps({"folders": [root]})
                page.send({"type": "scan"})
                page.send({"type": "open_model", "path": os.path.join(root, "Dragon set.zip"), "kind": "zip"})

                def export():
                    page.send({"type": "plan", "margin": 4, "included": ["body.stl"]})
                    return page.send({"type": "export_project"})["path"]

                first = export()
                self.assertEqual(os.path.basename(first), "Dragon set - PlatePilot.3mf")
                self.assertTrue(pp._is_inside(first, os.path.join(storage, "projects")))
                with zipfile.ZipFile(first) as project:
                    self.assertIn('<metadata name="Title">Dragon set</metadata>', project.read("3D/3dmodel.model").decode())
                self.assertEqual(export(), first)              # unchanged: replaced, one entry in Orca's list
                with open(first, "ab") as stream:              # saved over in Orca
                    stream.write(b" ")
                second = export()
                self.assertEqual(os.path.basename(second), "Dragon set - PlatePilot (2).3mf")
                self.assertEqual(opened, [first, first, second])
        finally:
            pp._get_profile, pp._project_settings, pp._app_version, pp._open_project = saved


def boxes_stl(*boxes):
    """Binary STL of several boxes, each (x0, y0, z0, x1, y1, z1), as separate shells."""
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    raw = bytearray(80) + struct.pack("<I", 12 * len(boxes))
    for x0, y0, z0, x1, y1, z1 in boxes:
        v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
        for a, b, c, d in quads:
            for tri in ((v[a], v[b], v[c]), (v[a], v[c], v[d])):
                raw += struct.pack("<3f", 0, 0, 0) + struct.pack("<9f", *[q for p in tri for q in p]) + b"\0\0"
    return bytes(raw)


MUSHROOM = boxes_stl((15, 15, 0, 25, 25, 24), (0, 0, 20, 40, 40, 24))      # a table on its leg, top in the air
SIDEWAYS = boxes_stl((0, 15, 15, 24, 25, 25), (20, 0, 0, 24, 40, 40))      # the same table lying on its edge


class Orientation(unittest.TestCase):
    """Parts lying worse than they could are pointed out, and turned only when asked."""

    def test_a_part_lying_well_is_left_alone(self):
        self.assertIsNone(pp._orientation_advice(box_stl(30, 20, 10)))
        self.assertIsNone(pp._orientation_advice(boxes_stl((15, 15, 0, 25, 25, 24), (0, 0, 0, 40, 40, 4))))

    def test_an_upside_down_part_is_flagged(self):
        advice = pp._orientation_advice(MUSHROOM)
        self.assertEqual(advice["down"], [0.0, 0.0, 1.0])
        self.assertEqual(advice["pct"], 100)
        self.assertEqual(advice["overhang_now"], 1600.0)
        self.assertEqual(advice["overhang_new"], 0.0)
        turned = pp._turned_stl(MUSHROOM, pp._rotation_to_down(advice["down"]))
        self.assertEqual(pp._binary_stl_info(turned)["dims"], [40.0, 40.0, 24.0])
        self.assertIsNone(pp._orientation_advice(turned))

    def test_a_part_on_its_edge_is_laid_flat(self):
        advice = pp._orientation_advice(SIDEWAYS)
        self.assertEqual(advice["down"], [1.0, 0.0, 0.0])
        self.assertEqual((advice["height_now"], advice["height_new"]), (40.0, 24.0))
        turned = pp._turned_stl(SIDEWAYS, pp._rotation_to_down(advice["down"]))
        self.assertEqual([round(v, 3) for v in pp._binary_stl_info(turned)["dims"]], [40.0, 40.0, 24.0])

    def test_rotation_puts_the_side_down(self):
        for d in ((0, 0, -1), (0, 0, 1), (1, 0, 0), (0, -1, 0), (0.6, 0.0, 0.8), (0.48, 0.6, -0.64)):
            rows = pp._rotation_to_down(d)
            moved = [sum(r * v for r, v in zip(row, d)) for row in rows]
            for got, want in zip(moved, (0, 0, -1)):
                self.assertAlmostEqual(got, want, places=9)
            for i in range(3):                                  # still a rotation
                for j in range(3):
                    self.assertAlmostEqual(sum(rows[i][k] * rows[j][k] for k in range(3)), float(i == j), places=9)

    def test_big_meshes_are_sampled_with_the_right_weight(self):
        triangles, weight = pp._sample_triangles(boxes_stl(*[(i, 0, 0, i + 1, 1, 1) for i in range(10)]), limit=30)
        self.assertEqual((len(triangles), weight), (30, 4))

    def open_zip(self, storage, root):
        with zipfile.ZipFile(os.path.join(root, "set.zip"), "w") as archive:
            archive.writestr("box.stl", box_stl(30, 20, 10))
            archive.writestr("side.stl", SIDEWAYS)
            archive.writestr("up.stl", MUSHROOM)
        page = FakePage(storage)
        page.config_text = json.dumps({"folders": [root]})
        page.send({"type": "scan"})
        page.send({"type": "open_model", "path": os.path.join(root, "set.zip"), "kind": "zip"})
        return page

    def check_all(self, page):
        index, found = 0, {}
        while index >= 0:
            reply = page.send({"type": "orient_check", "index": index})
            self.assertEqual(reply["type"], "orient_advice")
            if reply.get("advice"):
                found[page._open_parts[reply["index"]]["name"]] = reply["index"]
            index = reply["next"]
        self.assertEqual((reply["done"], reply["total"], reply["advised"]), (3, 3, 2))
        return found

    def test_checking_and_turning_from_the_page(self):
        with tempfile.TemporaryDirectory() as storage, tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            page = self.open_zip(storage, root)
            found = self.check_all(page)
            self.assertEqual(sorted(found), ["side.stl", "up.stl"])
            config = json.loads(page.config_text)
            source = page._selected_source

            def dims(name):
                return [round(v, 3) for v in next(p for p in page._current_parts(config) if p["name"] == name)["dims"]]

            self.assertEqual(dims("side.stl"), [24.0, 40.0, 40.0])      # nothing turns until asked
            reply = page.send({"type": "turn_part", "index": found["side.stl"], "on": True})
            self.assertEqual(reply["type"], "parts_turned")
            self.assertEqual([round(v, 3) for v in reply["parts"][0]["dims"]], [40.0, 40.0, 24.0])
            self.assertEqual(dims("side.stl"), [40.0, 40.0, 24.0])
            parts = page._current_parts(config)
            blob = page._read_stl_blobs(source, parts, ["side.stl"])["side.stl"]
            self.assertEqual([round(v, 3) for v in pp._binary_stl_info(blob)["dims"]], [40.0, 40.0, 24.0])
            self.assertEqual(page._read_stl_blobs(source, parts, ["side.stl"], turned=False)["side.stl"], SIDEWAYS)

            reply = page.send({"type": "turn_all"})                      # only the ones not turned yet
            self.assertEqual([p["index"] for p in reply["parts"]], [found["up.stl"]])
            self.assertEqual(sorted(page._turns), ["side.stl", "up.stl"])

            reply = page.send({"type": "turn_part", "index": found["side.stl"], "on": False})
            self.assertEqual(reply["parts"][0]["turned"], False)
            self.assertEqual(dims("side.stl"), [24.0, 40.0, 40.0])
            reply = page.send({"type": "turn_part", "index": 0, "on": True})     # no advice for the box: ignored
            self.assertEqual(reply["parts"], [])

            page.send({"type": "open_model", "path": os.path.join(root, "set.zip"), "kind": "zip"})
            self.assertEqual((page._advice, page._turns), ({}, {}))     # a source opens as it is

    def test_orca_arrange_can_orient_and_takes_turned_parts(self):
        saved = (pp._get_profile, pp._project_settings, pp._orca_executable, pp._write_cli_settings, pp.subprocess.Popen)
        commands = []
        pp._get_profile = lambda: {"name": "Test printer", "size": {"x": 245, "y": 245}, "height": 250}
        pp._project_settings = lambda: "{}"
        pp._orca_executable = lambda: "orca-slicer"
        pp._write_cli_settings = lambda folder: ["machine.json"]
        pp.subprocess.Popen = lambda command, **kw: commands.append(command) or types.SimpleNamespace(poll=lambda: None)
        try:
            with tempfile.TemporaryDirectory() as storage, tempfile.TemporaryDirectory() as root:
                root = os.path.realpath(root)
                page = self.open_zip(storage, root)
                found = self.check_all(page)
                page.send({"type": "turn_part", "index": found["side.stl"], "on": True})
                page.send({"type": "native_arrange", "included": ["box.stl", "side.stl"]})
                self.assertNotIn("--orient", commands[0])
                inputs = [a for a in commands[0] if a.endswith(".stl")]
                self.assertEqual(len(inputs), 2)
                with open(inputs[1], "rb") as stream:
                    self.assertEqual([round(v, 3) for v in pp._binary_stl_info(stream.read())["dims"]], [40.0, 40.0, 24.0])
                page._native_arrange_job = None
                page.send({"type": "save_settings", "config": {"orca_orient": True}})
                time.sleep(1.1)                                      # run folders are named to the second
                page.send({"type": "native_arrange", "included": ["box.stl"]})
                at = commands[1].index("--orient")
                self.assertEqual(commands[1][at + 1], "1")
                self.assertLess(commands[1].index("--arrange"), at)
        finally:
            pp._get_profile, pp._project_settings, pp._orca_executable, pp._write_cli_settings, pp.subprocess.Popen = saved


class DropIntoPlatePilot(unittest.TestCase):
    def test_dropped_files_are_copied_listed_and_opened(self):
        with tempfile.TemporaryDirectory() as storage:
            page = FakePage(storage)
            raw = box_stl(20, 10, 5)
            encoded = __import__("base64").b64encode(raw).decode()
            page.send({"type": "import_start", "loose": 1, "first": "bracket.stl"})
            reply = page.send({"type": "import_begin", "name": "bracket.stl", "top": "", "rel": "bracket.stl", "size": len(raw)})
            self.assertEqual((reply["type"], reply["done"]), ("import_next", False))
            reply = page.send({"type": "import_chunk", "data": encoded})
            self.assertTrue(reply["done"])
            page.send({"type": "import_finish"})
            opened = next(m for m in page.messages if m.get("type") == "archive")
            self.assertTrue(opened["added"])
            self.assertEqual([p["name"] for p in opened["parts"]], ["bracket.stl"])
            self.assertTrue(os.path.isfile(os.path.join(storage, "added", "bracket", "bracket.stl")))
            library = [m for m in page.messages if m.get("type") == "archives"][-1]
            # Opened for you after the drop, but still listed under New until you open it yourself.
            self.assertEqual([(a["name"], a["added"], a["new"]) for a in library["archives"]], [(os.path.join("bracket"), True, True)])
            page.send({"type": "open_model", "path": opened["path"], "kind": "folder"})
            library = [m for m in page.messages if m.get("type") == "archives"][-1]
            self.assertFalse(library["archives"][0]["new"])

            page.send({"type": "delete_source", "path": opened["path"]})
            self.assertFalse(os.path.exists(os.path.join(storage, "added", "bracket")))

    def test_library_lists_the_newest_sources_first(self):
        with tempfile.TemporaryDirectory() as storage:
            page = FakePage(storage)
            row = lambda name, when: {"kind": "folder", "name": name, "path": os.path.join(storage, name), "count": 1,
                                      "type_counts": {"stl": 1}, "new": False, "likely": True, "time": when}
            page._rows = [row("alpha", 100.0), row("zeta", 300.0), row("mid", 200.0), row("undated", None)]
            page._post_rows(page._config())
            self.assertEqual([a["name"] for a in page.messages[-1]["archives"]], ["zeta", "mid", "alpha", "undated"])

    def test_dropped_folder_with_non_latin_names_keeps_them_for_display(self):
        with tempfile.TemporaryDirectory() as storage:
            page = FakePage(storage)
            raw = box_stl(5, 5, 5)
            encoded = __import__("base64").b64encode(raw).decode()
            page.send({"type": "import_start", "loose": 0, "first": ""})
            page.send({"type": "import_begin", "name": "шестерня.stl", "top": "Новая папка", "rel": "Детали/шестерня.stl", "size": len(raw)})
            page.send({"type": "import_chunk", "data": encoded})
            page.send({"type": "import_finish"})
            stored = os.path.join(storage, "added", "Novaya papka", "Detali", "shesternya.stl")
            self.assertTrue(os.path.isfile(stored))
            library = [m for m in page.messages if m.get("type") == "archives"][-1]
            self.assertEqual([a["name"] for a in library["archives"]], [os.path.join("Новая папка", "Детали")])
            opened = next(m for m in page.messages if m.get("type") == "archive")
            self.assertEqual([p["name"] for p in opened["parts"]], ["shesternya.stl"])

    def test_interrupted_scan_is_reported_at_start(self):
        with tempfile.TemporaryDirectory() as storage:
            page = FakePage(storage)
            pp.orca.host.app_info = lambda: {"language": "en"}
            page.on_message({"type": "init"})
            self.assertFalse(page.messages[-1]["scan_interrupted"])
            open(os.path.join(storage, "scan-running"), "w").close()
            page.on_message({"type": "init"})
            self.assertTrue(page.messages[-1]["scan_interrupted"])
            page.send({"type": "scan"})
            self.assertFalse(os.path.exists(os.path.join(storage, "scan-running")))

    def test_wrong_size_or_type_is_refused(self):
        with tempfile.TemporaryDirectory() as storage:
            page = FakePage(storage)
            page.send({"type": "import_start", "loose": 1, "first": "x.exe"})
            with self.assertRaises(AssertionError):
                page.send({"type": "import_begin", "name": "x.exe", "top": "", "rel": "x.exe", "size": 3})
            page.messages.clear()
            page.send({"type": "import_start", "loose": 1, "first": "a.stl"})
            page.send({"type": "import_begin", "name": "a.stl", "top": "", "rel": "a.stl", "size": 1})
            with self.assertRaises(AssertionError):
                page.send({"type": "import_chunk", "data": "AAAA"})
            self.assertEqual([n for n in os.listdir(os.path.join(storage, "added", "a")) if n.endswith(".part")], [])


if __name__ == "__main__":
    unittest.main()
