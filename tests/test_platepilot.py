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
            self.assertEqual([(a["name"], a["added"], a["new"]) for a in library["archives"]], [(os.path.join("bracket"), True, False)])

            page.send({"type": "delete_source", "path": opened["path"]})
            self.assertFalse(os.path.exists(os.path.join(storage, "added", "bracket")))

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
