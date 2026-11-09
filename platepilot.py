# /// script
# requires-python = ">=3.9"
# [tool.orcaslicer.plugin]
# name = "PlatePilot"
# description = "Model library for STL bundles: preview parts on the active printer bed, arrange them on several plates and open the project in Prepare."
# author = "GNUGiminot"
# version = "0.7.0"
# ///

"""PlatePilot: an OrcaSlicer plugin that browses STL bundles and sends arranged multi-plate projects to Prepare."""

import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from xml.sax.saxutils import quoteattr
import xml.etree.ElementTree as ElementTree
import zipfile

import orca


VERSION = "0.7.0"
MAX_ARCHIVES = 500
# A binary STL of 2 million facets, the most the STL reader takes, is 100 000 084 bytes.
MAX_STL_BYTES = 100 * 1024 * 1024
MAX_TOTAL_STL_BYTES = 500 * 1024 * 1024
# Facets sent for a preview: the WebGL view shows this many in full; the 2D fallback asks for fewer.
MAX_PREVIEW_FACETS = 250_000
MAX_PROJECT_FACETS = 5_000_000
EXPORT_BATCH = 50000
MAX_NATIVE_PARTS = 200
DEFAULT_MARGIN_MM = 4.0
MAX_SCAN_DIRECTORIES = 20000
MAX_IMPORT_BYTES = 500 * 1024 * 1024
FOLDER_CACHE_KEEP = 4
# Orca keeps the plugin's data folder under its own data folder; past ~240 characters a path
# no longer fits Windows' 260-character limit once Orca adds its own suffixes.
MAX_IMPORT_PATH = 240
IMPORT_EXTENSIONS = {".zip", ".stl", ".step", ".stp", ".3mf"}
# Orca refuses any path with one of these components (PluginAuditManager.cpp,
# default_denied_path_keywords), so names of dropped files are adjusted to avoid them.
DENIED_PATH_NAMES = {"secret", "secrets", "cert", "certs", "certificate", "certificates", "conf", "config"}
WINDOWS_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(1, 10)}
CYRILLIC_LATIN = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґў",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
     "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya", "i", "yi", "ye", "g", "u"]))
MODEL_EXTENSIONS = {".stl": "stl", ".step": "step", ".stp": "step", ".3mf": "3mf"}
MODEL_TYPE_LABELS = {"stl": "STL", "step": "STEP", "3mf": "3MF"}
# Orca lays plates out in a grid with a gap of 1/5 of the plate size (PartPlate.cpp,
# LOGICAL_PART_PLATE_GAP) and assigns objects to plates by their position on load.
ORCA_PLATE_GAP = 1.0 / 5.0
# Keys that tie a preset to its parent or to other presets; the CLI must not try to
# resolve them when it loads the flattened settings of the active profile.
CLI_SKIP_KEYS = {
    "inherits", "compatible_printers", "compatible_printers_condition", "compatible_prints",
    "compatible_prints_condition", "print_settings_id", "printer_settings_id", "filament_settings_id",
}
DEFAULT_CONFIG = {
    "folders": [], "hidden": {}, "check_on_open": True, "recursive": True, "gpu_preview": True,
    "margin_mm": DEFAULT_MARGIN_MM,
    "model_types": ["stl", "step", "3mf"], "arrange_mode": "own", "language": "auto",
}
LANGUAGES = ("en", "ru")

# Every user-facing string, as (English, Russian). Plural forms are separated by "|":
# English "one|other", Russian "one|few|many". The page receives the active language's column.
STRINGS = {
    # page: static text
    "app.tagline": ("Model library and plate planner", "Библиотека моделей и раскладка по столам"),
    "app.loading_profile": ("Loading the printer profile…", "Загружаю профиль принтера…"),
    "btn.refresh": ("Refresh", "Обновить"),
    "btn.folders": ("Folders", "Папки"),
    "btn.add_files": ("Add files", "Добавить файлы"),
    "btn.add_files_tip": ("Add ZIP, STL, STEP or 3MF files. They are copied into PlatePilot, so Orca does not ask for permission.", "Добавить файлы ZIP, STL, STEP или 3MF. Они копируются в PlatePilot, поэтому Orca не спрашивает разрешения."),
    "lib.title": ("Library", "Библиотека"),
    "lib.search": ("Search by name", "Поиск по названию"),
    "lib.all": ("All", "Все"),
    "lib.folders": ("Folders", "Папки"),
    "parts.title": ("Parts", "Детали"),
    "parts.all": ("All", "Все"),
    "parts.none": ("None", "Снять"),
    "view.reset": ("Reset view", "Сбросить вид"),
    "view.hint": ("Drag to rotate · wheel to zoom · double-click to reset", "Перетаскивайте, чтобы вращать · колесо — масштаб · двойной щелчок — сброс"),
    "view.empty": ("Select an STL part to see it on the printer bed", "Выберите STL-деталь, чтобы увидеть её на столе принтера"),
    "mode.own": ("PlatePilot layout", "Раскладка PlatePilot"),
    "mode.orca": ("Orca auto-arrange", "Авторасстановка Orca"),
    "mode.gap": ("Gap", "Зазор"),
    "mode.plates": ("Plates", "Столов"),
    "mode.plates_auto": ("auto", "авто"),
    "mode.plates_tip": ("How many plates to spread the parts over. Empty: as few as they fit on. More than that spreads them evenly, even if they would fit on one.",
                        "На сколько столов разложить детали. Пусто — на как можно меньше. Больше — детали распределяются поровну, даже если поместились бы на одном."),
    "unit.mm": ("mm", "мм"),
    "btn.plan": ("Calculate", "Рассчитать"),
    "btn.plan_smart": ("Smart layout", "Умная раскладка"),
    "btn.plan_smart_tip": ("Beta: the parts of each plate are grouped in the middle, small ones nearest the centre, away from the bed edges and evenly spaced; the plates are filled evenly",
                           "Бета: детали каждого стола собраны в центре, мелкие ближе к середине, подальше от краёв и с ровными зазорами; столы загружены поровну"),
    "btn.send": ("Open in Prepare", "В «Подготовку»"),
    "btn.arrange": ("Arrange in Orca", "Расставить в Orca"),
    "mode.own_note": ("PlatePilot packs the parts by their footprint on the active printer's bed, shows the plates and opens the project in Prepare.",
                      "PlatePilot раскладывает детали по габаритам на столе активного принтера, показывает столы и отправляет готовый проект в «Подготовку»."),
    "mode.orca_note": ("The parts go to Orca's own arrange: it uses the part outlines and the active printer and process profiles, adds plates as needed and opens the project.",
                       "Детали передаются в Orca: её авторасстановка учитывает контуры деталей и активные профили принтера и процесса, сама добавляет столы и открывает проект."),
    "drawer.title": ("Sources and refresh", "Источники и обновление"),
    "drawer.close": ("Close", "Закрыть"),
    "drawer.folders": ("Watched folders", "Отслеживаемые папки"),
    "drawer.folders_note": ("PlatePilot finds ZIP archives and folders with model files in them by their names. A file is read only when you open it.",
                            "PlatePilot находит в них ZIP-архивы и папки с моделями по именам файлов. Сам файл читается, только когда вы его открываете."),
    "drawer.pick": ("Choose folder…", "Выбрать папку…"),
    "drawer.paste": ("Or paste a folder path (quotes are fine, as from \"Copy as path\"):", "Или вставьте путь вручную (можно с кавычками, как из «Копировать как путь»):"),
    "drawer.paste_only": ("Paste the full path of a folder with models:", "Вставьте полный путь к папке с моделями:"),
    "drawer.path_ph": ("Full folder path", "Полный путь к папке"),
    "drawer.add": ("Add", "Добавить"),
    "drawer.formats": ("Formats", "Форматы"),
    "drawer.formats_note": ("Sources without the selected formats are hidden. Preview and layout work with STL.", "Источники без выбранных форматов скрываются. Просмотр и раскладка доступны для STL."),
    "drawer.scan": ("Scanning", "Сканирование"),
    "drawer.recursive": ("Search subfolders", "Искать в подпапках"),
    "drawer.check_on_open": ("Check folders when PlatePilot opens", "Проверять папки при открытии PlatePilot"),
    "drawer.audit_note": ("Orca asks once before PlatePilot looks into a watched folder, and once for each archive or model folder you open. Files you drop into PlatePilot need no permission.",
                          "Orca один раз спросит разрешение на просмотр отслеживаемой папки и по одному разу — на каждый архив или папку с моделями, которые вы откроете. Файлам, перетащенным в PlatePilot, разрешения не нужны."),
    "drawer.preview": ("3D preview", "3D-просмотр"),
    "drawer.gpu_preview": ("Draw with the graphics card (WebGL)", "Рисовать видеокартой (WebGL)"),
    "drawer.gpu_note": ("Shows parts of up to 250 000 facets in full and turns smoothly. If the preview looks wrong, turn it off: the simple mode draws up to 30 000 facets.",
                        "Показывает детали до 250 000 граней целиком и плавно вращается. Если просмотр выглядит неправильно, выключите: простой режим рисует до 30 000 граней."),
    "drawer.language": ("Language", "Язык"),
    "drawer.lang_auto": ("Same as Orca", "Как в Orca"),
    "modal.confirm": ("Confirm", "Подтверждение"),
    "modal.cancel": ("Cancel", "Отмена"),
    "modal.continue": ("Continue", "Продолжить"),
    # page: dynamic text
    "st.choose_folder": ("Drop ZIP or STL files here, or open Folders and choose a folder with models.", "Перетащите сюда ZIP или STL либо откройте «Папки» и выберите папку с моделями."),
    "st.press_refresh": ("Press Refresh to scan the folders.", "Нажмите «Обновить», чтобы просканировать папки."),
    "st.scanning": ("Scanning folders…", "Сканирую папки…"),
    "st.scanned": ("Folders checked · {sources}", "Папки проверены · {sources}"),
    "st.scanned_new": ("Folders checked · {sources} · {n} new", "Папки проверены · {sources} · новых: {n}"),
    "st.importing": ("Adding {name} · {i} of {n} · {pct}%", "Добавляю {name} · {i} из {n} · {pct}%"),
    "st.import_none": ("Nothing to add: drop ZIP, STL, STEP or 3MF files or a folder with them.", "Добавлять нечего: перетащите ZIP, STL, STEP, 3MF или папку с ними."),
    "st.import_skipped": (" Other files skipped: {n}.", " Другие файлы пропущены: {n}."),
    "st.opening": ("Opening…", "Открываю…"),
    "st.opening_picker":("Opening the folder picker…", "Открываю выбор папки…"),
    "st.opened_source": ("Opened \"{name}\".", "Открыт источник «{name}»."),
    "st.plan_ready": ("The layout is ready. Open it in Prepare.", "Раскладка готова — отправьте её в «Подготовку»."),
    "st.plan_problems": ("Some parts do not fit on the bed. Change the selection.", "Есть детали, которые не помещаются на стол; исправьте выбор."),
    "st.building": ("Building the project for Orca…", "Собираю проект для Orca…"),
    "st.project_opened": ("The project is opening in Orca. If Prepare already has models, Orca asks what to do: \"Open as project\" keeps the plates, \"Import geometry\" adds the parts to the current project.",
                          "Проект открывается в Orca. Если в «Подготовке» уже есть модели, Orca спросит: «Открыть как проект» сохранит раскладку по столам, «Импорт геометрии» добавит детали в текущий проект."),
    "st.project_saved": ("3MF saved: {path}. Open it with File > Open Project. {error}", "3MF создан: {path}. Откройте его через «Файл → Открыть проект». {error}"),
    "st.arranging": ("Orca is arranging {n} parts and adding plates as needed…", "Orca расставляет {n} дет. и при необходимости добавляет столы…"),
    "st.arrange_done": ("Auto-arrange finished.", "Авторасстановка завершена."),
    "st.plugin_error": ("Plugin error.", "Ошибка плагина."),
    "st.removed_source": ("The source was deleted from disk or hidden.", "Источник удалён с диска или скрыт."),
    "printer.unreadable": ("Profile not read", "Профиль не прочитан"),
    "printer.zones": ("zones", "зоны"),
    "printer.zones_tip": ("The printer profile has excluded bed areas. Check the layout in Orca.", "В профиле есть исключённые зоны стола — проверьте раскладку в Orca."),
    "lib.nothing_found": ("Nothing matches the filter.", "Ничего не найдено по фильтру."),
    "lib.press_refresh": ("Press Refresh to find models in the folders.", "Нажмите «Обновить», чтобы найти модели в папках."),
    "lib.add_folder": ("Drop ZIP or STL files here, or add a folder with models under Folders.", "Перетащите сюда ZIP или STL либо добавьте папку с моделями в «Папках»."),
    "lib.new": ("New", "Новые"),
    "lib.added": ("Added", "Добавленные"),
    "lib.all_folders": ("All folders", "Все папки"),
    "lib.folder_tip": ("Show the sources of one folder only", "Показать источники только из одной папки"),
    "lib.other_show": ("{n} more archives whose names don't look like models", "Ещё архивов, не похожих на модели по названию: {n}"),
    "lib.other_hide": ("Hide the archives whose names don't look like models", "Скрыть архивы, не похожие на модели"),
    "lib.other_note": ("PlatePilot judges by the name, because looking inside every archive would make Orca ask about each one. Open an archive to check it: with models it moves up, without them it disappears. Search looks through these too.",
                       "PlatePilot судит по названию: чтобы заглянуть в каждый архив, Orca спрашивала бы разрешение на каждый. Откройте архив, чтобы проверить: с моделями он поднимется в список, без них исчезнет. Поиск находит и эти архивы."),
    "lib.mark_seen": ("Mark all as seen", "Отметить все как просмотренные"),
    "lib.no_new": ("No new sources since the last check.", "Новых источников с прошлой проверки нет."),
    "drop.title": ("Drop to add to PlatePilot", "Отпустите, чтобы добавить в PlatePilot"),
    "drop.note": ("ZIP, STL, STEP, 3MF or a folder with them. The files are copied into PlatePilot's own folder, so Orca does not ask for permission. To watch a folder instead, add it under Folders or paste its path with Ctrl+V.",
                  "ZIP, STL, STEP, 3MF или папка с ними. Файлы копируются в собственную папку PlatePilot, поэтому Orca не спрашивает разрешения. Чтобы отслеживать папку, добавьте её в «Папках» или вставьте её путь через Ctrl+V."),
    "src.choose": ("Choose a source", "Выберите источник"),
    "src.choose_note": ("Pick an archive or folder in the library. PlatePilot shows its parts and a preview on the bed.", "Архив или папку из библиотеки слева — PlatePilot покажет её детали и предпросмотр на столе."),
    "src.summary": ("{n} model files · {stl} STL · {path}", "{n} файлов моделей · {stl} STL · {path}"),
    "src.hide": ("Hide", "Скрыть"),
    "src.hide_tip": ("Hide from the list. The file stays on disk.", "Скрыть из списка, файл на диске останется"),
    "src.new": ("new", "новый"),
    "src.added": ("added", "добавлен"),
    "src.unopened": ("not opened yet", "ещё не открывался"),
    "src.delete": ("Delete", "Удалить"),
    "src.delete_tip": ("Delete the copy added to PlatePilot", "Удалить копию, добавленную в PlatePilot"),
    "delete.title": ("Delete from PlatePilot?", "Удалить из PlatePilot?"),
    "delete.body": ("The copy of \"{name}\" in PlatePilot's folder is deleted. The files you dropped or pasted from are not touched.",
                    "Копия «{name}» в папке PlatePilot будет удалена. Файлы, из которых она добавлена, не затрагиваются."),
    "parts.meta": ("{dims} mm · {facets} facets", "{dims} мм · {facets} граней"),
    "parts.catalog_only": ("{type} · catalog only", "{type} · только в каталоге"),
    "parts.no_files": ("The source has no files of the selected formats.", "В источнике нет файлов выбранных форматов."),
    "parts.placeholder": ("Parts appear here.", "Детали появятся здесь."),
    "view.meta": ("{dims} mm · {facets} facets", "{dims} мм · {facets} граней"),
    "view.shown": (" · {n} shown", " · показано {n}"),
    "plan.parts": ("{n} part|{n} parts", "{n} деталь|{n} детали|{n} деталей"),
    "plan.plates": ("{n} plate|{n} plates", "{n} стол|{n} стола|{n} столов"),
    "plan.not_fit": ("does not fit: {name}", "не помещается: {name}"),
    "plan.at_least": ("needs at least {plates}", "нужно не меньше: {plates}"),
    "plan.at_most": ("at most one plate per part", "не больше одного стола на деталь"),
    "plan.too_tall": ("taller than Z: {name}", "выше Z: {name}"),
    "plan.zones": ("check excluded areas in Orca", "проверьте исключённые зоны в Orca"),
    "plan.smart": ("smart layout β", "умная раскладка β"),
    "plan.plate": ("Plate {n}", "Стол {n}"),
    "plan.plate_meta": ("{parts} · {fill}%", "{parts} · {fill}%"),
    "plan.rotated": (" · rotated {n}°", " · повёрнута на {n}°"),
    "plan.zoom_hint": ("Click a plate to see it up close", "Нажмите на стол, чтобы рассмотреть его крупно"),
    "plan.zoom_title": ("Plate {n} of {total}", "Стол {n} из {total}"),
    "plan.prev": ("Previous plate", "Предыдущий стол"),
    "plan.next": ("Next plate", "Следующий стол"),
    "plan.close": ("Close", "Закрыть"),
    "folders.none": ("No folders yet.", "Папки ещё не добавлены."),
    "folders.remove": ("Stop watching", "Перестать отслеживать"),
    "folders.sources": ("{n} source|{n} sources", "{n} источник|{n} источника|{n} источников"),
    "folders.stats": ("{sources} · checked {time}", "{sources} · проверено {time}"),
    "folders.new": ("{n} new", "новых: {n}"),
    "folders.other": ("{n} archives that don't look like models", "не похожих на модели архивов: {n}"),
    "folders.empty": ("{n} without models hidden", "без моделей скрыто: {n}"),
    "folders.never": ("Not checked yet", "Ещё не проверялась"),
    "folders.missing": ("Folder not found", "Папка не найдена"),
    "folders.skipped": ("{n} subfolders skipped: Orca cannot open them for plugins", "пропущено подпапок: {n} — Orca не даёт плагинам их открыть"),
    "folders.unreadable": ("Orca cannot open this folder for plugins: its path has non-Latin letters or a folder named config, cert or secret", "Orca не даёт плагинам открыть эту папку: в пути есть нелатинские буквы или папка с именем config, cert или secret"),
    "folders.truncated":("only the first {n} are shown", "показаны первые {n}"),
    "folders.added": ("Added to PlatePilot", "Добавлено в PlatePilot"),
    "folders.added_note": ("Files you drop or paste are copied here.", "Сюда копируются файлы, которые вы перетаскиваете или вставляете."),
    # plugin messages and errors
    "err.path_empty": ("enter a folder path", "укажи путь к папке"),
    "err.path_relative": ("enter the full folder path, for example {example}", "укажи полный путь к папке, например {example}"),
    "err.folder_missing": ("folder not found: {path}", "папка не найдена: {path}"),
    "err.not_filesystem": ("the selected item is not a folder on disk (for example, \"Libraries\"); choose a regular folder",
                           "выбранный элемент не является папкой на диске (например, «Библиотеки»); выберите обычную папку"),
    "err.picker_windows": ("The folder picker is available on Windows; paste the path instead.", "Выбор папки через Проводник доступен в Windows; введите путь вручную."),
    "picker.title": ("Folder with models for PlatePilot", "Папка с моделями для PlatePilot"),
    "picker.ok": ("Watch folder", "Отслеживать папку"),
    "err.no_main_window": ("the Orca main window was not found", "не найдено главное окно Orca для передачи проекта"),
    "err.open_unsupported": ("opening the project in this Orca window is supported on Windows only", "передача проекта в открытое окно Orca поддерживается только в Windows"),
    "err.no_executable": ("could not find the running OrcaSlicer executable", "не удалось определить исполняемый файл текущего OrcaSlicer"),
    "err.no_settings": ("could not read the settings of the active Orca profile", "не удалось прочитать настройки активного профиля Orca"),
    "err.no_printer_preset": ("no printer profile is selected in Orca", "в Orca не выбран профиль принтера"),
    "err.no_process_preset": ("no process profile is selected in Orca", "в Orca не выбран профиль процесса"),
    "warn.zip_empty": ("No STL, STEP or 3MF in the archive", "В архиве нет STL, STEP или 3MF"),
    "warn.zip_unreadable": ("Archive not read: {error}", "Архив не прочитан: {error}"),
    "err.archive_outside": ("the archive must be inside a watched folder", "архив должен находиться в выбранной папке"),
    "err.folder_outside": ("the model folder must be inside a watched folder", "папка модели должна находиться внутри отслеживаемой папки"),
    "warn.step_open": ("STEP: no preview or layout; open it in Orca, which converts it itself", "STEP: без просмотра и раскладки; откройте в Orca — она преобразует его сама"),
    "warn.3mf_error": ("3MF could not be read: {error}", "3MF не прочитан: {error}"),
    "warn.3mf_empty": ("3MF holds no model", "в 3MF нет модели"),
    "btn.open_in_orca": ("Open in Orca", "Открыть в Orca"),
    "msg.step_sent": ("{name} sent to Orca: it opens it in Prepare.", "{name} передан в Orca: она откроет его в «Подготовке»."),
    "warn.unsafe_name": ("name with non-Latin letters: this Orca build cannot open it for plugins (OrcaSlicer 2.5.0 can); drag it into PlatePilot",
                         "имя с нелатинскими буквами: эта сборка Orca не даёт плагинам открыть его (OrcaSlicer 2.5.0 даёт) — перетащите в PlatePilot"),
    "err.unsafe_path": ("{path}: Orca does not let plugins open paths with a folder named config, cert or secret, and builds before OrcaSlicer 2.5.0 none with non-Latin letters. Drag the files into PlatePilot or rename the folder.",
                        "{path}: Orca не даёт плагинам открывать пути с папкой config, cert, secret, а сборки до OrcaSlicer 2.5.0 — и пути с нелатинскими буквами. Перетащите файлы в PlatePilot или переименуйте папку."),
    "st.no_answer": ("The plugin did not answer. Press Refresh to try again.", "Плагин не ответил. Нажмите «Обновить», чтобы попробовать ещё раз."),
    "st.scan_interrupted": ("The last folder check did not finish (Orca was closed), so it was not repeated automatically. Press Refresh to check the folders.",
                            "Прошлая проверка папок не завершилась (Orca закрылась), поэтому автоматически она не повторялась. Нажмите «Обновить», чтобы проверить папки."),
    "warn.too_big":("file larger than {mb} MB", "файл больше {mb} MB"),
    "err.total_archive": ("the STL files in the archive exceed {mb} MB in total", "суммарный размер STL в архиве превышает {mb} MB"),
    "err.total_folder": ("the STL files in the folder exceed {mb} MB in total", "суммарный размер STL в папке превышает {mb} MB"),
    "warn.empty_model": ("empty model", "пустая модель"),
    "warn.stl_error": ("STL error: {error}", "ошибка STL: {error}"),
    "err.stl_short": ("STL file is too short", "слишком короткий STL"),
    "err.stl_coords": ("the STL has invalid coordinates", "в STL обнаружены некорректные координаты"),
    "err.stl_vertices": ("could not read the STL vertices", "не удалось прочитать вершины STL"),
    "err.stl_facets_limit": ("the STL exceeds 2 million facets", "STL превышает лимит 2 млн граней"),
    "err.stl_geometry": ("the STL has no valid geometry", "STL не содержит корректную геометрию"),
    "err.stl_no_triangles": ("the STL has no triangles", "STL не содержит треугольников"),
    "err.gap_too_big": ("the gap is larger than the bed", "зазор больше рабочей области стола"),
    "err.plan_first": ("calculate the layout first", "сначала рассчитай раскладку"),
    "err.reread": ("could not read the STL again: {name}", "не удалось повторно прочитать STL: {name}"),
    "err.facets_limit": ("a project is limited to {n} facets; select fewer parts or use Orca auto-arrange", "экспорт ограничен {n} гранями за проект; сними часть деталей или используй «Авторасстановку Orca»"),
    "msg.already_watched": ("This folder is already watched: {path}", "Эта папка уже отслеживается: {path}"),
    "msg.inside_watched": ("The folder is already inside the watched folder {path} (subfolder search is on).", "Папка уже входит в отслеживаемую {path} (поиск в подпапках включён)."),
    "msg.replaced_nested": ("Folder added; these nested folders are no longer needed separately: {paths}", "Добавлена папка; вложенные в неё больше не нужны отдельно: {paths}"),
    "err.scan_stopped": ("Scanning stopped: {error}", "Сканирование остановлено: {error}"),
    "err.source_gone": ("the source is no longer inside a watched folder", "источник больше не находится в отслеживаемой папке"),
    "err.select_source": ("select a ZIP or a folder with STL files first", "сначала выбери ZIP или папку с STL"),
    "err.archive_moved": ("the archive is no longer inside a watched folder", "архив больше не находится в отслеживаемой папке"),
    "err.stl_left_archive": ("the STL is no longer in the selected archive", "STL больше не находится в выбранном архиве"),
    "err.stl_left_folder": ("the STL is no longer in the selected folder", "STL больше не находится в выбранной папке"),
    "err.stl_limit": ("the STL exceeds the {mb} MB limit", "STL превышает лимит {mb} MB"),
    "err.arrange_running": ("Orca auto-arrange is already running", "авторасстановка Orca уже выполняется"),
    "err.select_stl": ("select at least one STL part", "отметь хотя бы одну STL-деталь"),
    "err.too_many_parts": ("one run can arrange at most {n} STL files", "за один запуск можно расставить не более {n} STL"),
    "err.no_bed": ("could not read the printable area of the active printer", "не удалось прочитать рабочую область активного принтера"),
    "err.cli_failed": ("The Orca CLI did not create a project. {detail}", "CLI Orca не создал проект. {detail}"),
    "err.cli_exit": ("Exit code: {code}", "Код завершения: {code}"),
    "msg.arranged": ("Orca arranged {parts} on {plates} and is opening the project. If Prepare already has models, choose \"Open as project\" to keep the plates.",
                     "Orca расставила {parts} на {plates} и открывает проект. Если в «Подготовке» уже есть модели, выберите «Открыть как проект», чтобы сохранить столы."),
    "msg.arranged_partly": ("Orca could not place {n} of {parts} on the plates, even turned: they are left beside the plates. Check their size against the bed, or use PlatePilot layout to see which ones.",
                            "Orca не смогла поставить на столы {n} из {parts} даже с поворотом — они лежат рядом со столами. Проверьте их размер относительно стола или посмотрите в «Раскладке PlatePilot», какие именно."),
    "err.arrange_failed": ("Auto-arrange did not finish: {error}", "Авторасстановка не завершилась: {error}"),
    "msg.picker_cancelled": ("Folder selection cancelled.", "Выбор папки отменён."),
    "msg.source_hidden": ("The source is hidden from the list; the file on disk is kept.", "Источник скрыт из списка; файл на диске не удалён."),
    "msg.deleted": ("Deleted from PlatePilot.", "Удалено из PlatePilot."),
    "msg.imported": ("Added to PlatePilot: {files}.", "Добавлено в PlatePilot: {files}."),
    "import.files": ("{n} file|{n} files", "{n} файл|{n} файла|{n} файлов"),
    "import.group": ("Files {time}", "Файлы {time}"),
    "err.import_type": ("{name}: only ZIP, STL, STEP and 3MF files can be added", "{name}: добавлять можно только ZIP, STL, STEP и 3MF"),
    "err.import_size": ("{name} is larger than {mb} MB", "{name} больше {mb} MB"),
    "err.import_state": ("the file transfer was interrupted; add the files again", "передача файлов прервалась; добавьте их ещё раз"),
    "err.import_path": ("the name of {name} is too long for PlatePilot's folder", "имя {name} слишком длинное для папки PlatePilot"),
    "err.no_storage": ("PlatePilot's data folder is not available: {error}", "папка данных PlatePilot недоступна: {error}"),
    "err.not_added": ("only sources added to PlatePilot can be deleted", "удалять можно только источники, добавленные в PlatePilot"),
    "err.paste_path": ("{path} is not a folder or a ZIP, STL, STEP or 3MF file", "{path} — не папка и не файл ZIP, STL, STEP или 3MF"),
    "err.part_missing": ("part not found", "деталь не найдена"),
    "err.no_preview": ("this part has no preview data", "для этой детали нет данных превью"),
    "err.stl_outside": ("the STL is outside the watched folder", "STL вне отслеживаемой папки"),
    "err.no_printable_area": ("Could not read printable_area of the active profile. Reload the printer profile and try again.", "Не удалось получить printable_area активного профиля. Перезагрузи профиль принтера и попробуй ещё раз."),
    "err.plan_source": ("calculate the layout of the selected source first", "сначала рассчитай раскладку выбранного источника"),
    "err.plan_not_exportable": ("fix the parts that do not fit on the bed or are taller than the printer first", "сначала исправь детали, которые не помещаются на стол или превышают высоту принтера"),
    "err.printer_changed": ("the active printer changed after the layout was calculated; calculate it again", "активный принтер изменился после расчёта; рассчитай раскладку заново"),
    "err.stl_changed": ("an STL changed after the layout was calculated; calculate it again", "STL изменился после расчёта раскладки; рассчитай её заново"),
}

_LANG = "en"


def _set_language(choice):
    """Select the UI language: "en", "ru" or "auto" (Orca's own language)."""
    global _LANG
    if choice not in LANGUAGES:
        try:
            choice = str(orca.host.app_info().get("language", ""))
        except Exception:
            choice = ""
        choice = "ru" if choice.lower().startswith("ru") else "en"
    _LANG = choice
    return _LANG


def _t(key, **values):
    """Translated text for key in the active language, with {placeholders} filled in."""
    text = STRINGS[key][LANGUAGES.index(_LANG)]
    return text.format(**values) if values else text


def _plural(key, n):
    """Pick the plural form of a "one|few|many" (ru) or "one|other" (en) string."""
    forms = _t(key).split("|")
    if _LANG == "ru" and len(forms) == 3:
        index = 0 if n % 10 == 1 and n % 100 != 11 else 1 if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 2
    else:
        index = 0 if n == 1 else len(forms) - 1
    return forms[index].format(n=n)


def _ui_strings():
    column = LANGUAGES.index(_LANG)
    return {key: value[column] for key, value in STRINGS.items()}


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PlatePilot</title>
<style>
:root{
  --bg:var(--orca-bg,#f5f6f7);--fg:var(--orca-fg,#1c2227);--muted:var(--orca-muted,#6c7781);--line:var(--orca-border,#d8dde2);
  --accent:var(--orca-accent,#009688);--accent-fg:var(--orca-accent-fg,#fff);
  --surface:color-mix(in srgb,var(--fg) 2.5%,var(--bg));--surface-2:color-mix(in srgb,var(--fg) 6%,var(--bg));
  --raised:color-mix(in srgb,#fff 55%,var(--bg));--accent-soft:color-mix(in srgb,var(--accent) 13%,transparent);
  --accent-line:color-mix(in srgb,var(--accent) 45%,var(--line));--warn:#c47a00;--danger:#d14343;
  --shadow:0 1px 2px rgb(16 24 32/.06),0 10px 28px -16px rgb(16 24 32/.28);--r:12px;--r-sm:8px;
  font:13px/1.45 var(--orca-font,system-ui,'Segoe UI',sans-serif);color-scheme:light dark}
[data-orca-theme=dark]{--raised:color-mix(in srgb,#fff 5%,var(--bg));--surface:color-mix(in srgb,#fff 3%,var(--bg));--surface-2:color-mix(in srgb,#fff 7%,var(--bg));--warn:#f2ab3c;--danger:#ff7a7a;--shadow:0 1px 2px rgb(0 0 0/.35),0 14px 34px -18px rgb(0 0 0/.75)}
*{box-sizing:border-box}[hidden]{display:none!important}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);display:grid;grid-template-rows:auto 3px minmax(0,1fr) auto;overflow:hidden}
svg.ic{width:16px;height:16px;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
/* Orca injects default button/input styles after this sheet (orca-plugin-defaults), so the base
   rules use "body …" to outrank them; otherwise buttons get Orca's white text and accent fill. */
body button{font:inherit;display:inline-flex;align-items:center;justify-content:center;gap:7px;height:32px;padding:0 13px;border-radius:var(--r-sm);border:1px solid var(--line);background:var(--raised);color:var(--fg);cursor:pointer;white-space:nowrap;transition:background .15s,border-color .15s,box-shadow .15s,transform .05s}body button:hover{filter:none}
button:hover:not(:disabled){border-color:var(--accent-line);background:color-mix(in srgb,var(--accent) 6%,var(--raised))}
button:active:not(:disabled){transform:translateY(1px)}button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--accent);border-color:transparent;color:var(--accent-fg);box-shadow:0 6px 16px -8px color-mix(in srgb,var(--accent) 80%,transparent)}
button.primary:hover:not(:disabled){background:color-mix(in srgb,#fff 10%,var(--accent))}
button.ghost{background:transparent;border-color:transparent;color:var(--muted)}button.ghost:hover:not(:disabled){color:var(--fg);background:var(--surface-2);border-color:transparent}
button.icon{width:32px;padding:0}.beta{font-size:10.5px;font-weight:700;line-height:16px;padding:0 5px;border-radius:999px;background:var(--accent-soft);color:var(--accent)}
body input,body select{font:inherit;color:var(--fg);background:var(--raised);border:1px solid var(--line);border-radius:var(--r-sm);height:32px;padding:0 10px;min-width:0}
body input:focus,body select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
input[type=number]{width:76px}
.muted{color:var(--muted)}.warn{color:var(--warn)}
/* app bar */
.appbar{display:flex;align-items:center;gap:14px;padding:10px 16px;border-bottom:1px solid var(--line);background:linear-gradient(100deg,color-mix(in srgb,var(--accent) 9%,var(--bg)),var(--bg) 46%)}
.brand{display:flex;align-items:center;gap:10px;min-width:0}.brand-mark{width:34px;height:34px;display:grid;place-items:center}
.brand-mark svg{width:34px;height:34px}.brand h1{font-size:16px;margin:0;letter-spacing:.2px}.brand small{display:block;color:var(--muted);font-size:11.5px;margin-top:-1px}
.printer{margin-left:auto;display:flex;align-items:center;gap:10px;padding:5px 6px 5px 12px;border:1px solid var(--line);border-radius:999px;background:var(--raised);min-width:0;max-width:52%}
.printer .name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.printer .spec{display:flex;gap:4px}
.pill{display:inline-flex;align-items:center;gap:4px;height:22px;padding:0 8px;border-radius:999px;background:var(--surface-2);color:var(--muted);font-size:11.5px;white-space:nowrap}
.pill.accent{background:var(--accent-soft);color:var(--accent)}.pill.warn{background:color-mix(in srgb,var(--warn) 14%,transparent);color:var(--warn)}
.appbar-actions{display:flex;gap:6px}
.progress{position:relative;overflow:hidden;background:transparent}.busy .progress::after{content:"";position:absolute;inset:0 auto 0 -30%;width:30%;background:linear-gradient(90deg,transparent,var(--accent),transparent);animation:slide 1.1s linear infinite}
@keyframes slide{to{left:100%}}
/* workspace */
.workspace{display:grid;grid-template-columns:minmax(260px,320px) minmax(0,1fr);gap:14px;padding:14px 16px;min-height:0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);min-width:0;min-height:0}
.card-head{display:flex;align-items:center;gap:8px;padding:11px 12px 9px}.card-head h2{font-size:13px;margin:0;font-weight:650;letter-spacing:.2px}
.count{min-width:22px;height:20px;padding:0 7px;border-radius:999px;background:var(--surface-2);color:var(--muted);font-size:11px;display:inline-grid;place-items:center}
.library{display:flex;flex-direction:column}
.search{position:relative;margin:0 12px}.search svg{position:absolute;left:10px;top:8px;color:var(--muted)}.search input{width:100%;padding-left:32px}
.chips{display:flex;gap:6px;padding:9px 12px 10px;flex-wrap:wrap}
.rootbar{margin:8px 12px 0}.rootbar select{width:100%}
.chip{height:26px;padding:0 10px;border-radius:999px;font-size:12px;background:transparent}label.chip{display:inline-flex;align-items:center;border:1px solid var(--line);cursor:pointer;user-select:none}.chip.on{background:var(--accent-soft);border-color:var(--accent-line);color:var(--accent);font-weight:600}
.list{overflow:auto;flex:1;padding:0 6px 8px;border-top:1px solid var(--line)}
.source{display:flex;gap:10px;align-items:flex-start;padding:9px 8px;margin-top:4px;border-radius:var(--r-sm);cursor:pointer;border:1px solid transparent}
.source:hover{background:var(--surface-2)}.source.on{background:var(--accent-soft);border-color:var(--accent-line)}
.source .glyph{width:30px;height:30px;border-radius:8px;display:grid;place-items:center;flex:none;background:var(--surface-2);color:var(--muted)}.source.on .glyph{background:var(--accent);color:#fff}
.glyph.k-zip{--k:#d97706}.glyph.k-folder{--k:#2563eb}.glyph.k-added{--k:var(--accent)}[data-orca-theme=dark] .glyph.k-zip{--k:#f5a524}[data-orca-theme=dark] .glyph.k-folder{--k:#60a5fa}
.source .glyph.k-zip,.source .glyph.k-folder,.source .glyph.k-added,.source-head .glyph.k-zip,.source-head .glyph.k-folder,.source-head .glyph.k-added{background:color-mix(in srgb,var(--k) 15%,transparent);color:var(--k)}
.source.on .glyph.k-zip,.source.on .glyph.k-folder,.source.on .glyph.k-added{background:var(--k);color:#fff}
.source .t{min-width:0;flex:1}.source .n{font-weight:600;overflow-wrap:anywhere;line-height:1.3}.source .m{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}
.empty{display:grid;place-items:center;text-align:center;gap:8px;padding:26px 18px;color:var(--muted)}.empty svg{width:42px;height:42px;stroke-width:1.2;opacity:.6}
.detail{display:grid;grid-template-rows:auto minmax(220px,1fr) auto;gap:14px;min-height:0;min-width:0}
.source-head{display:flex;align-items:center;gap:12px;padding:12px 14px}.source-head .glyph{width:40px;height:40px;border-radius:11px;display:grid;place-items:center;background:var(--accent-soft);color:var(--accent);flex:none}
.source-head .glyph svg{width:22px;height:22px}.source-head h2{margin:0;font-size:16px;overflow-wrap:anywhere}.source-head .sub{color:var(--muted);font-size:12px;margin-top:2px;overflow-wrap:anywhere}
.source-head .grow{flex:1;min-width:0}
.split{display:grid;grid-template-columns:minmax(260px,1fr) minmax(300px,1.25fr);gap:14px;min-height:0}
.parts{display:flex;flex-direction:column}.parts .card-head{border-bottom:1px solid var(--line)}
.part-list{overflow:auto;flex:1;padding:4px 6px 6px}
.part{display:flex;gap:10px;align-items:center;padding:7px 8px;border-radius:var(--r-sm);cursor:pointer}.part:hover{background:var(--surface-2)}.part.on{background:var(--accent-soft)}
.part.off{cursor:default;opacity:.62}
.part.ext{cursor:default}.part.ext .t{opacity:.62}
button.small{height:26px;padding:0 10px;font-size:12px;flex:none}.part .t{min-width:0;flex:1}.part .n{overflow-wrap:anywhere;line-height:1.3}.part .m{color:var(--muted);font-size:11.5px;margin-top:2px}
.check{appearance:none;width:18px;height:18px;margin:0;flex:none;border-radius:5px;border:1.5px solid color-mix(in srgb,var(--fg) 30%,transparent);background:var(--raised);display:grid;place-items:center;cursor:pointer;padding:0}
.check:checked{background:var(--accent);border-color:var(--accent)}.check:checked::after{content:"";width:9px;height:5px;border:2px solid #fff;border-top:0;border-right:0;transform:translateY(-1px) rotate(-45deg)}.check:disabled{opacity:.4;cursor:default}
.viewer{position:relative;overflow:hidden;background:radial-gradient(120% 90% at 50% 0%,color-mix(in srgb,var(--accent) 7%,var(--surface)),var(--surface) 60%)}
canvas{position:absolute;inset:0;display:block;width:100%;height:100%;cursor:grab;touch-action:none}#viewGl{pointer-events:none}canvas:active{cursor:grabbing}
.viewer .hud{position:absolute;left:12px;top:10px;right:12px;display:flex;align-items:flex-start;gap:8px;pointer-events:none}.viewer .hud .t{flex:1;min-width:0}
.viewer .hud b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.viewer .hud button{pointer-events:auto}
.viewer .hint{position:absolute;left:12px;bottom:10px;font-size:11.5px;color:var(--muted);pointer-events:none}
.viewer .empty{position:absolute;inset:0}
/* layout panel */
.layout-panel{padding:12px 14px 14px}.layout-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.seg{display:inline-flex;padding:3px;border-radius:10px;background:var(--surface-2);border:1px solid var(--line)}.seg button{height:28px;border:0;background:transparent;color:var(--muted)}
.seg button.on{background:var(--raised);color:var(--fg);box-shadow:0 1px 3px rgb(0 0 0/.12);font-weight:600}
.field{display:inline-flex;align-items:center;gap:7px;color:var(--muted)}
.spacer{flex:1}.mode-note{color:var(--muted);font-size:12px;margin-top:9px}
.summary{margin-top:10px;display:flex;gap:6px;flex-wrap:wrap}
.plates{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px;margin-top:12px;max-height:48vh;overflow:auto;padding:2px}
.plate{border:1px solid var(--line);border-radius:10px;background:var(--raised);padding:9px;cursor:zoom-in}
.plate:hover{border-color:var(--accent-line)}
.zoom-hint{font-size:11.5px;color:var(--muted);margin-top:6px}
.zoom{width:min(1100px,100%);max-height:calc(100vh - 40px);display:grid;grid-template-rows:auto 1fr;gap:10px;background:var(--bg);border:1px solid var(--line);border-radius:14px;box-shadow:0 30px 60px -20px rgb(0 0 0/.45);padding:14px 16px}
.zoom-head{display:flex;align-items:center;gap:8px}.zoom-head h2{margin:0;font-size:15px;flex:1}.zoom-head span{color:var(--muted);font-size:12px}
.zoom-body{display:grid;grid-template-columns:minmax(0,1fr) 260px;gap:14px;min-height:0}
.zoom-body svg{width:100%;height:auto;max-height:calc(100vh - 140px);display:block}
.zoom-list{overflow:auto;font-size:12px;margin:0;padding:0;list-style:none}
.zoom-list li{display:grid;grid-template-columns:22px 1fr;gap:2px 6px;padding:5px 0;border-top:1px solid var(--line)}
.zoom-list li:first-child{border-top:0}.zoom-list .no{font-weight:700}.zoom-list .nm{overflow-wrap:anywhere}.zoom-list .dm{grid-column:2;color:var(--muted)}
@media (max-width:760px){.zoom-body{grid-template-columns:1fr}.zoom-list{max-height:30vh}}
.plate-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:7px;gap:6px}.plate-head b{font-size:12.5px}.plate-head span{font-size:11px;color:var(--muted)}
.plate svg{display:block;width:100%;height:auto;border-radius:6px}
/* status */
.statusbar{display:flex;align-items:center;gap:9px;padding:7px 16px;border-top:1px solid var(--line);font-size:12px;color:var(--muted);background:var(--surface)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}.statusbar.ok .dot{background:var(--accent)}.statusbar.warn .dot{background:var(--warn)}.statusbar.err .dot{background:var(--danger)}.statusbar.err{color:var(--danger)}
.busy .statusbar .dot{background:var(--accent);animation:pulse 1s ease-in-out infinite}@keyframes pulse{50%{opacity:.3}}
/* drawer */
.scrim{position:fixed;inset:0;background:rgb(10 14 18/.38);opacity:0;pointer-events:none;transition:opacity .2s;z-index:20}.scrim.open{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(440px,100%);background:var(--bg);border-left:1px solid var(--line);box-shadow:-20px 0 40px -24px rgb(0 0 0/.5);transform:translateX(102%);transition:transform .22s ease;z-index:21;display:flex;flex-direction:column}
.drawer.open{transform:none}.drawer-head{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--line)}.drawer-head h2{margin:0;font-size:15px;flex:1}
.drawer-body{overflow:auto;padding:6px 16px 20px}.section{padding:14px 0;border-bottom:1px solid var(--line)}.section:last-child{border-bottom:0}
.section h3{margin:0 0 4px;font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted)}.section p{margin:0 0 10px;color:var(--muted);font-size:12px}
.folders{display:flex;flex-direction:column;gap:6px;margin-bottom:10px}
.folder{display:flex;align-items:center;gap:9px;padding:7px 6px 7px 10px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--raised)}.folder .p{flex:1;min-width:0;overflow-wrap:anywhere;font-size:12.5px}.folder svg.ic{color:var(--accent)}
.folder .fm{color:var(--muted);font-size:11.5px;margin-top:2px}.folder .fm b{color:var(--accent);font-weight:600}.folder.added{background:var(--surface-2);border-style:dashed}
.chip .n{margin-left:5px;font-size:11px;opacity:.8}
body button.more{width:calc(100% - 4px);margin:10px 2px 4px;justify-content:flex-start;background:transparent;border-style:dashed;color:var(--muted);white-space:normal;height:auto;min-height:32px;padding:6px 10px;text-align:left}
.list-note{color:var(--muted);font-size:11.5px;padding:2px 8px 6px}.source.other .n{color:var(--muted)}.chips .end{margin-left:auto}
.drop{position:fixed;inset:10px;z-index:40;display:grid;place-items:center;border:2px dashed var(--accent);border-radius:16px;background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(2px);pointer-events:none}
.drop-card{display:grid;justify-items:center;gap:8px;max-width:440px;text-align:center;padding:24px}.drop-card svg{width:46px;height:46px;color:var(--accent);stroke-width:1.4}.drop-card h2{margin:0;font-size:17px}.drop-card p{margin:0;color:var(--muted)}
.row{display:flex;gap:8px;align-items:center}.row input{flex:1}
.toggle{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 0}
.switch{appearance:none;width:36px;height:20px;border-radius:999px;background:color-mix(in srgb,var(--fg) 22%,transparent);position:relative;cursor:pointer;margin:0;flex:none;transition:background .15s}
.switch::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;box-shadow:0 1px 2px rgb(0 0 0/.3);transition:left .15s}.switch:checked{background:var(--accent)}.switch:checked::after{left:18px}
/* modal */
.modal{position:fixed;inset:0;z-index:30;display:grid;place-items:center;padding:20px;background:rgb(10 14 18/.45)}.modal[hidden]{display:none}
.dialog{width:min(460px,100%);background:var(--bg);border:1px solid var(--line);border-radius:14px;box-shadow:0 30px 60px -20px rgb(0 0 0/.45);padding:20px}
.dialog h2{margin:0 0 8px;font-size:16px}.dialog .body{color:var(--muted);overflow-wrap:anywhere;white-space:pre-line}.dialog .actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
@media(max-width:1000px){body{overflow:auto;display:block}.workspace{grid-template-columns:1fr}.library{max-height:320px}.split{grid-template-columns:1fr}.viewer{height:300px}.printer{display:none}.appbar-actions{margin-left:auto}}
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
 <symbol id="i-logo" viewBox="0 0 320 320"><defs><pattern id="logo-tex" width="14" height="14" patternUnits="userSpaceOnUse"><circle cx="3" cy="4" r="1.3" fill="#000" opacity=".16"/><circle cx="10" cy="9" r="1.1" fill="#fff" opacity=".28"/><circle cx="6" cy="12" r=".9" fill="#000" opacity=".12"/></pattern><linearGradient id="logo-sheen" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".18"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".12"/></linearGradient></defs><rect width="320" height="320" rx="72" fill="#1F1C19"/><rect x="44" y="44" width="232" height="232" rx="28" fill="#C8A266"/><rect x="44" y="44" width="232" height="232" rx="28" fill="url(#logo-sheen)"/><rect x="44" y="44" width="232" height="232" rx="28" fill="url(#logo-tex)"/><rect x="45.5" y="45.5" width="229" height="229" rx="27" fill="none" stroke="#fff" stroke-width="3" opacity=".28"/><g fill="#000" opacity=".24"><circle cx="108" cy="113" r="44"/><rect x="162" y="69" width="94" height="120" rx="16"/><rect x="64" y="167" width="88" height="94" rx="16"/><rect x="162" y="199" width="94" height="62" rx="16"/></g><circle cx="108" cy="108" r="44" fill="#F2EBDC"/><circle cx="108" cy="108" r="14" fill="#C8A266"/><rect x="162" y="64" width="94" height="120" rx="16" fill="#F2EBDC"/><rect x="199" y="84" width="20" height="80" rx="10" fill="#C8A266"/><rect x="64" y="162" width="88" height="94" rx="16" fill="#F2EBDC"/><rect x="84" y="182" width="48" height="12" rx="6" fill="#C8A266"/><rect x="84" y="206" width="48" height="12" rx="6" fill="#C8A266"/><rect x="162" y="194" width="94" height="62" rx="16" fill="#1F1C19"/><circle cx="209" cy="225" r="12" fill="#C8A266"/></symbol>
 <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M20 11a8 8 0 0 0-14.6-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.6 4.5L20 16m0 4v-4h-4"/></symbol>
 <symbol id="i-folder" viewBox="0 0 24 24"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l2 2h8.8A1.5 1.5 0 0 1 21 9.5v8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z"/></symbol>
 <symbol id="i-zip" viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="18" rx="2.5"/><path d="M12 3v2.5m0 2v2m0 2v2"/><rect x="10" y="14" width="4" height="4.5" rx="1"/></symbol>
 <symbol id="i-models" viewBox="0 0 24 24"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l2 2h8.8A1.5 1.5 0 0 1 21 9.5v8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z"/><path d="m12 10.5 3 1.6v3.3l-3 1.6-3-1.6v-3.3zM9 12.1l3 1.6 3-1.6M12 13.7v3.3"/></symbol>
 <symbol id="i-file-plus" viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M12 11v6m-3-3h6"/></symbol>
 <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/></symbol>
 <symbol id="i-sliders" viewBox="0 0 24 24"><path d="M4 7h10m4 0h2M4 17h4m4 0h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></symbol>
 <symbol id="i-cube" viewBox="0 0 24 24"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/></symbol>
 <symbol id="i-grid" viewBox="0 0 24 24"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></symbol>
 <symbol id="i-send" viewBox="0 0 24 24"><path d="M5 12h12m-5-5 5 5-5 5"/><path d="M20 5v14"/></symbol>
 <symbol id="i-wand" viewBox="0 0 24 24"><path d="m4 20 11-11m-2-2 4 4M17 3v3m-1.5-1.5h3M20 9v2m-1-1h2M9 3v2M8 4h2"/></symbol>
 <symbol id="i-eye-off" viewBox="0 0 24 24"><path d="M3 3l18 18M10.6 6.2A9.6 9.6 0 0 1 12 6c5 0 9 6 9 6a15.8 15.8 0 0 1-2.6 3.2M6.3 7.8C4.2 9.4 3 12 3 12s4 6 9 6a8.5 8.5 0 0 0 3.6-.8"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></symbol>
 <symbol id="i-x" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></symbol>
 <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
 <symbol id="i-target" viewBox="0 0 24 24"><circle cx="12" cy="12" r="7"/><path d="M12 2v4m0 12v4M2 12h4m12 0h4"/></symbol>
</svg>

<header class="appbar">
 <div class="brand"><div class="brand-mark"><svg><use href="#i-logo"/></svg></div><div><h1>PlatePilot</h1><small data-i18n="app.tagline">Model library and plate planner</small></div></div>
 <div id="printer" class="printer"><span class="name muted" data-i18n="app.loading_profile">Loading the printer profile…</span></div>
 <div class="appbar-actions"><button id="scan" class="primary"><svg class="ic"><use href="#i-refresh"/></svg><span data-i18n="btn.refresh">Refresh</span></button><button id="addFiles" data-i18n-title="btn.add_files_tip" title="Add ZIP, STL, STEP or 3MF files"><svg class="ic"><use href="#i-file-plus"/></svg><span data-i18n="btn.add_files">Add files</span></button><input id="filePick" type="file" multiple accept=".zip,.stl,.step,.stp,.3mf" hidden><button id="openSettings"><svg class="ic"><use href="#i-sliders"/></svg><span data-i18n="btn.folders">Folders</span></button></div>
</header>
<div class="progress"></div>

<main class="workspace">
 <section class="card library">
  <div class="card-head"><h2 data-i18n="lib.title">Library</h2><span id="libCount" class="count">0</span></div>
  <div class="search"><svg class="ic"><use href="#i-search"/></svg><input id="filter" data-i18n-ph="lib.search" placeholder="Search by name"></div>
  <div class="rootbar" id="rootBar" hidden><select id="rootFilter" data-i18n-title="lib.folder_tip" title="Show the sources of one folder only"></select></div>
  <div class="chips" id="kindChips"><button class="chip on" data-kind="all" data-i18n="lib.all">All</button><button class="chip" data-kind="new"><span data-i18n="lib.new">New</span><span id="newCount" class="n"></span></button><button class="chip" data-kind="zip">ZIP</button><button class="chip" data-kind="folder" data-i18n="lib.folders">Folders</button><button id="addedChip" class="chip" data-kind="added" data-i18n="lib.added" hidden>Added</button><button id="markSeen" class="chip ghost end" data-i18n="lib.mark_seen" hidden>Mark all as seen</button></div>
  <div id="archives" class="list"></div>
 </section>

 <section class="detail">
  <div id="sourceHead" class="card source-head"></div>
  <div class="split">
   <div class="card parts">
    <div class="card-head"><h2 data-i18n="parts.title">Parts</h2><span id="partCount" class="count">0</span><span class="spacer"></span><button id="all" class="ghost" data-i18n="parts.all">All</button><button id="none" class="ghost" data-i18n="parts.none">None</button></div>
    <div id="parts" class="part-list"></div>
   </div>
   <div class="card viewer">
    <canvas id="view"></canvas><canvas id="viewGl" aria-hidden="true"></canvas>
    <div class="hud"><div class="t"><b id="viewTitle"></b><span id="viewMeta" class="muted"></span></div><button id="resetView" class="icon" data-i18n-title="view.reset" title="Reset view" hidden><svg class="ic"><use href="#i-target"/></svg></button></div>
    <div id="viewHint" class="hint" data-i18n="view.hint" hidden>Drag to rotate · wheel to zoom · double-click to reset</div>
    <div id="viewEmpty" class="empty"><svg class="ic"><use href="#i-cube"/></svg><div data-i18n="view.empty">Select an STL part to see it on the printer bed</div></div>
   </div>
  </div>
  <div class="card layout-panel">
   <div class="layout-row">
    <div class="seg" id="modeSeg"><button data-mode="own" class="on"><svg class="ic"><use href="#i-grid"/></svg><span data-i18n="mode.own">PlatePilot layout</span></button><button data-mode="orca"><svg class="ic"><use href="#i-wand"/></svg><span data-i18n="mode.orca">Orca auto-arrange</span></button></div>
    <label class="field" id="marginField"><span data-i18n="mode.gap">Gap</span> <input id="margin" type="number" min="0" max="20" step="0.5" value="4"> <span data-i18n="unit.mm">mm</span></label>
    <label class="field" id="platesField" data-i18n-title="mode.plates_tip" title="How many plates"><span data-i18n="mode.plates">Plates</span> <input id="plateCount" type="number" min="1" max="50" step="1" data-i18n-ph="mode.plates_auto" placeholder="auto"></label>
    <span class="spacer"></span>
    <button id="plan"><svg class="ic"><use href="#i-grid"/></svg><span data-i18n="btn.plan">Calculate</span></button>
    <button id="planSmart" data-i18n-title="btn.plan_smart_tip" title="Beta: parts grouped in the middle of each plate"><svg class="ic"><use href="#i-wand"/></svg><span data-i18n="btn.plan_smart">Smart layout</span><span class="beta">β</span></button>
    <button id="sendPrepare" class="primary" disabled><svg class="ic"><use href="#i-send"/></svg><span data-i18n="btn.send">Open in Prepare</span></button>
    <button id="nativeArrange" class="primary" hidden><svg class="ic"><use href="#i-wand"/></svg><span data-i18n="btn.arrange">Arrange in Orca</span></button>
   </div>
   <div id="modeNote" class="mode-note"></div>
   <div id="summary" class="summary"></div>
   <div id="plates" class="plates"></div><div id="zoomHint" class="zoom-hint" hidden data-i18n="plan.zoom_hint">Click a plate to see it up close</div>
  </div>
 </section>
</main>

<footer id="statusbar" class="statusbar"><span class="dot"></span><span id="status" data-i18n="app.loading_profile">Loading the printer profile…</span></footer>

<div id="scrim" class="scrim"></div>
<aside id="drawer" class="drawer">
 <div class="drawer-head"><svg class="ic"><use href="#i-sliders"/></svg><h2 data-i18n="drawer.title">Sources and refresh</h2><button id="closeSettings" class="icon ghost" data-i18n-title="drawer.close" title="Close"><svg class="ic"><use href="#i-x"/></svg></button></div>
 <div class="drawer-body">
  <div class="section"><h3 data-i18n="drawer.folders">Watched folders</h3><p data-i18n="drawer.folders_note">PlatePilot looks for ZIP archives and unpacked model folders in them.</p>
   <div id="folderList" class="folders"></div>
   <div class="row"><button id="pickFolder" class="primary"><svg class="ic"><use href="#i-folder"/></svg><span data-i18n="drawer.pick">Choose folder…</span></button><button id="addFiles2"><svg class="ic"><use href="#i-file-plus"/></svg><span data-i18n="btn.add_files">Add files</span></button></div>
   <p id="pasteNote" style="margin:12px 0 6px" data-i18n="drawer.paste">Or paste a folder path:</p>
   <div class="row"><input id="folder" data-i18n-ph="drawer.path_ph" placeholder="Full folder path"><button id="addFolder"><svg class="ic"><use href="#i-plus"/></svg><span data-i18n="drawer.add">Add</span></button></div>
  </div>
  <div class="section"><h3 data-i18n="drawer.formats">Formats</h3><p data-i18n="drawer.formats_note">Sources without the selected formats are hidden.</p>
   <div class="chips" style="padding:0"><label class="chip on" data-type="stl"><input type="checkbox" id="modelStl" hidden checked>STL</label><label class="chip on" data-type="step"><input type="checkbox" id="modelStep" hidden checked>STEP / STP</label><label class="chip on" data-type="3mf"><input type="checkbox" id="model3mf" hidden checked>3MF</label></div>
  </div>
  <div class="section"><h3 data-i18n="drawer.scan">Scanning</h3>
   <label class="toggle"><span data-i18n="drawer.recursive">Search subfolders</span><input id="recursive" class="switch" type="checkbox" checked></label>
   <label class="toggle"><span data-i18n="drawer.check_on_open">Check folders when PlatePilot opens</span><input id="checkOnOpen" class="switch" type="checkbox" checked></label>
   <p style="margin-top:8px" data-i18n="drawer.audit_note">Orca asks once before PlatePilot looks into a watched folder.</p>
  </div>
  <div class="section"><h3 data-i18n="drawer.preview">3D preview</h3>
   <label class="toggle"><span data-i18n="drawer.gpu_preview">Draw with the graphics card (WebGL)</span><input id="gpuPreview" class="switch" type="checkbox" checked></label>
   <p style="margin-top:8px" data-i18n="drawer.gpu_note">Shows parts of up to 250 000 facets in full and turns smoothly.</p>
  </div>
  <div class="section"><h3 data-i18n="drawer.language">Language</h3>
   <select id="language"><option value="auto" data-i18n="drawer.lang_auto">Same as Orca</option><option value="en">English</option><option value="ru">Русский</option></select>
  </div>
 </div>
</aside>

<div id="drop" class="drop" hidden><div class="drop-card"><svg class="ic"><use href="#i-plus"/></svg><h2 data-i18n="drop.title">Drop to add to PlatePilot</h2><p data-i18n="drop.note">ZIP, STL, STEP, 3MF or a folder with them.</p></div></div>
<div id="zoom" class="modal" hidden><div class="zoom" role="dialog" aria-modal="true"><div class="zoom-head"><h2 id="zoomTitle"></h2><span id="zoomMeta"></span><button id="zoomPrev" class="icon" data-i18n-title="plan.prev" title="Previous plate">&#8249;</button><button id="zoomNext" class="icon" data-i18n-title="plan.next" title="Next plate">&#8250;</button><button id="zoomClose" data-i18n="plan.close">Close</button></div><div class="zoom-body"><div id="zoomPlate"></div><ol id="zoomList" class="zoom-list"></ol></div></div></div>
<div id="modal" class="modal" hidden><div class="dialog"><h2 id="modalTitle" data-i18n="modal.confirm">Confirm</h2><div id="modalText" class="body"></div><div class="actions"><button id="modalNo" data-i18n="modal.cancel">Cancel</button><button id="modalYes" class="primary" data-i18n="modal.continue">Continue</button></div></div></div>

<script>
const $=id=>document.getElementById(id);
const S={archives:[],folders:[],selected:null,selectedKind:null,selectedName:'',selectedAdded:false,parts:[],profile:null,config:{},mesh:null,drag:null,busy:false,scanning:false,importing:false,importWait:null,replyTimer:null,lastScan:0,showOther:false,pending:null,nativeTick:null,mode:'own',kind:'all',plan:null,previewIndex:-1,strings:{},lang:'en',platform:'windows'};
const IMPORT_RE=/\.(zip|stl|step|stp|3mf)$/i,CHUNK=1536*1024;
const PALETTE=['#14b8a6','#6366f1','#f59e0b','#ec4899','#0ea5e9','#84cc16','#a855f7','#f97316'];
const icon=n=>`<svg class="ic"><use href="#i-${n}"/></svg>`;
function t(key,values={}){return String(S.strings[key]??key).replace(/\{(\w+)\}/g,(m,k)=>values[k]??m)}
function plural(key,n){const forms=t(key).split('|');let i=n===1?0:forms.length-1;if(S.lang==='ru'&&forms.length===3)i=n%10===1&&n%100!==11?0:n%10>=2&&n%10<=4&&(n%100<12||n%100>14)?1:2;return forms[i].replace('{n}',n)}
function applyStrings(){document.documentElement.lang=S.lang;document.querySelectorAll('[data-i18n]').forEach(el=>el.textContent=t(el.dataset.i18n));document.querySelectorAll('[data-i18n-ph]').forEach(el=>el.placeholder=t(el.dataset.i18nPh));document.querySelectorAll('[data-i18n-title]').forEach(el=>el.title=t(el.dataset.i18nTitle))}
function send(type,extra={}){orca.postMessage({type,...extra})}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function locale(){return S.lang==='ru'?'ru-RU':'en-US'}
function fmt(v,d=1){return Number(v||0).toLocaleString(locale(),{maximumFractionDigits:d,minimumFractionDigits:0})}
function status(text,level='info'){$('status').textContent=text;$('statusbar').className='statusbar '+({ok:'ok',warn:'warn',error:'err'}[level]||'')}
function setBusy(b){S.busy=b;document.body.classList.toggle('busy',b);$('scan').disabled=b;clearTimeout(S.replyTimer);if(b)S.replyTimer=setTimeout(noAnswer,30000);updateActions()}
/* a plugin call that fails inside Orca never answers; the page must not stay busy forever */
function noAnswer(){if(!S.busy||S.importing||S.nativeTick)return;S.scanning=false;setBusy(false);status(t('st.no_answer'),'error')}
function meshPart(p){return (p.filetype==='stl'||p.filetype==='3mf')&&p.member&&!p.blocked}
function selectedStl(){return S.parts.filter(p=>p.included&&meshPart(p))}
function updateActions(){const n=selectedStl().length;$('plan').disabled=S.busy||!n;$('planSmart').disabled=S.busy||!n;$('sendPrepare').disabled=S.busy||!(S.plan&&S.plan.exportable);$('nativeArrange').disabled=S.busy||!n;$('partCount').textContent=S.parts.length?`${n} / ${S.parts.length}`:'0'}
function scheduleNativeTick(){if(S.nativeTick)return;S.nativeTick=setTimeout(()=>{S.nativeTick=null;send('native_arrange_tick')},700)}
function stopNativeTicks(){if(S.nativeTick)clearTimeout(S.nativeTick);S.nativeTick=null}
function requestScan(){if(S.busy)return;S.scanning=true;S.lastScan=Date.now();setBusy(true);status(t('st.scanning'));send('scan')}
function ask(title,body,kind,payload={},yes){S.pending={kind,payload};$('modalTitle').textContent=title;$('modalText').textContent=body;$('modalYes').textContent=yes||t('modal.continue');$('modal').hidden=false;$('modalYes').focus()}
function finishAsk(yes){const p=S.pending;S.pending=null;$('modal').hidden=true;if(p&&yes&&p.kind==='delete')send('delete_source',{path:p.payload.path})}
function openDrawer(open){$('drawer').classList.toggle('open',open);$('scrim').classList.toggle('open',open)}

/* settings */
function applyConfig(c){S.config=c||{};$('checkOnOpen').checked=S.config.check_on_open!==false;$('recursive').checked=S.config.recursive!==false;$('gpuPreview').checked=S.config.gpu_preview!==false;const types=S.config.model_types||['stl','step','3mf'];for(const [k,id] of [['stl','modelStl'],['step','modelStep'],['3mf','model3mf']]){$(id).checked=types.includes(k);$(id).parentElement.classList.toggle('on',types.includes(k))}if(S.config.margin_mm!=null)$('margin').value=S.config.margin_mm;$('language').value=S.config.language||'auto';setMode(S.config.arrange_mode||'own',false);renderFolders()}
function when(sec){const d=new Date(sec*1000),hm=d.toLocaleTimeString(locale(),{hour:'2-digit',minute:'2-digit'});return d.toDateString()===new Date().toDateString()?hm:d.toLocaleDateString(locale(),{day:'numeric',month:'short'})+' '+hm}
function folderMeta(f){if(!f||f.checked==null)return esc(t('folders.never'));if(f.missing)return `<span class="warn">${esc(t('folders.missing'))}</span>`;if(f.unreadable)return `<span class="warn">${esc(t('folders.unreadable'))}</span>`;const out=[esc(t('folders.stats',{sources:plural('folders.sources',f.sources||0),time:when(f.checked)}))];if(f.new)out.push(`<b>${esc(t('folders.new',{n:f.new}))}</b>`);if(f.other)out.push(esc(t('folders.other',{n:f.other})));if(f.empty)out.push(esc(t('folders.empty',{n:f.empty})));if(f.skipped)out.push(`<span class="warn">${esc(t('folders.skipped',{n:f.skipped}))}</span>`);if(f.truncated)out.push(`<span class="warn">${esc(t('folders.truncated',{n:f.truncated}))}</span>`);return out.join(' · ')}
function renderFolders(){const folders=S.config.folders||[],info=p=>S.folders.find(f=>f.path===p&&f.origin==='watched'),added=S.folders.find(f=>f.origin==='added');let html=folders.length?folders.map(p=>`<div class="folder">${icon('folder')}<div class="p">${esc(p)}<div class="fm">${folderMeta(info(p))}</div></div><button class="icon ghost" title="${esc(t('folders.remove'))}" data-remove="${esc(p)}">${icon('x')}</button></div>`).join(''):`<div class="muted">${esc(t('folders.none'))}</div>`;if(added)html+=`<div class="folder added">${icon('plus')}<div class="p">${esc(t('folders.added'))}<div class="fm">${folderMeta(added)} · ${esc(t('folders.added_note'))}</div></div></div>`;$('folderList').innerHTML=html;document.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>{if(S.busy)return;S.scanning=true;setBusy(true);send('remove_folder',{path:b.dataset.remove})})}
function saveUiSettings(){S.config.check_on_open=$('checkOnOpen').checked;S.config.recursive=$('recursive').checked;S.config.gpu_preview=$('gpuPreview').checked;S.config.model_types=['stl','step','3mf'].filter(k=>$(k==='stl'?'modelStl':k==='step'?'modelStep':'model3mf').checked);S.config.margin_mm=Math.max(0,Math.min(20,Number($('margin').value)||0));S.config.arrange_mode=S.mode;S.config.language=$('language').value;send('save_settings',{config:S.config})}

/* printer */
function setProfile(p){S.profile=p||null;if(!p||p.error){$('printer').innerHTML=`<span class="pill warn">${esc(t('printer.unreadable'))}</span><span class="name muted">${esc(p&&p.error||'')}</span>`;return}const s=p.size||{};$('printer').innerHTML=`<span class="name" title="${esc(p.name)}">${esc(p.name||'—')}</span><span class="spec"><span class="pill accent">${fmt(s.x)} × ${fmt(s.y)} ${esc(t('unit.mm'))}</span><span class="pill">Z ${fmt(p.height)}</span>${p.exclusions?`<span class="pill warn" title="${esc(t('printer.zones_tip'))}">${esc(t('printer.zones'))}</span>`:''}</span>`;if(S.mesh)renderMesh()}

/* library */
function kindClass(kind,added){return added?'k-added':kind==='folder'?'k-folder':'k-zip'}
function sourceRow(a){const badges=a.type_counts?Object.entries(a.type_counts).map(([k,v])=>`<span class="pill">${esc(k)} ${v}</span>`).join(''):`<span class="pill">${esc(t('src.unopened'))}</span>`;return `<div class="source ${S.selected===a.path?'on':''} ${a.likely===false?'other':''}" data-path="${esc(a.path)}" data-kind="${esc(a.kind)}"><div class="glyph ${kindClass(a.kind,a.added)}">${icon(a.kind==='folder'?'models':'zip')}</div><div class="t"><div class="n">${esc(a.name)}</div><div class="m">${a.new?`<span class="pill accent">${esc(t('src.new'))}</span>`:''}${a.added?`<span class="pill">${esc(t('src.added'))}</span>`:''}${badges}<span class="pill">${esc(a.size)}</span>${a.warning?`<span class="pill warn">${esc(a.warning)}</span>`:''}</div></div></div>`}
// The folder switch: every watched folder and PlatePilot's own folder of added files, when there are two or more.
function renderRoots(){const roots=(S.folders||[]).filter(f=>f.path),sel=$('rootFilter');if(S.root&&!roots.some(f=>f.path===S.root))S.root='';
 $('rootBar').hidden=roots.length<2;if(roots.length<2)return;const n=p=>S.archives.filter(x=>x.root===p&&x.likely!==false).length,base=p=>p.replace(/[\\/]+$/,'').split(/[\\/]/).pop()||p;
 sel.innerHTML=`<option value="">${esc(t('lib.all_folders'))} (${S.archives.filter(x=>x.likely!==false).length})</option>`+roots.map(f=>`<option value="${esc(f.path)}" title="${esc(f.path)}"${f.path===S.root?' selected':''}>${esc(f.origin==='added'?t('folders.added'):base(f.path))} (${n(f.path)})</option>`).join('');sel.value=S.root||''}
function renderArchives(){const q=$('filter').value.trim().toLowerCase();renderRoots();const match=x=>(!S.root||x.root===S.root)&&(S.kind==='all'||(S.kind==='new'?x.new:S.kind==='added'?x.added:x.kind===S.kind));const found=S.archives.filter(x=>x.name.toLowerCase().includes(q)&&match(x));
 /* a search looks through everything; otherwise archives whose names don't look like models wait in a collapsed group */
 const main=q?found:found.filter(x=>x.likely!==false),rest=q?[]:found.filter(x=>x.likely===false);const fresh=S.archives.filter(x=>x.new).length;
 $('libCount').textContent=S.archives.filter(x=>x.likely!==false).length;$('newCount').textContent=fresh||'';$('addedChip').hidden=!S.archives.some(x=>x.added)&&S.kind!=='added';$('markSeen').hidden=!fresh||S.kind!=='new';
 if(S.selected&&!S.archives.some(x=>x.path===S.selected))clearSource(t('st.removed_source'));
 let html=main.map(sourceRow).join('');
 if(rest.length)html+=`<button id="toggleOther" class="more">${icon(S.showOther?'eye-off':'zip')}<span>${esc(t(S.showOther?'lib.other_hide':'lib.other_show',{n:rest.length}))}</span></button>${S.showOther?`<div class="list-note">${esc(t('lib.other_note'))}</div>`+rest.map(sourceRow).join(''):''}`;
 if(!main.length&&!rest.length)html=`<div class="empty">${icon('folder')}<div>${esc(S.kind==='new'&&S.archives.length?t('lib.no_new'):S.archives.length?t('lib.nothing_found'):(S.config.folders||[]).length&&!S.lastScan?t('lib.press_refresh'):t('lib.add_folder'))}</div></div>`;
 $('archives').innerHTML=html;if($('toggleOther'))$('toggleOther').onclick=()=>{S.showOther=!S.showOther;renderArchives()};
 document.querySelectorAll('.source').forEach(el=>el.onclick=()=>{if(S.busy)return;setBusy(true);status(t('st.opening'));send('open_model',{path:el.dataset.path,kind:el.dataset.kind})})}
function clearSource(note){S.selected=null;S.parts=[];S.plan=null;S.mesh=null;S.previewIndex=-1;renderSourceHead();renderParts(note);renderPlan(null);drawEmptyView()}
function renderSourceHead(){if(!S.selected){$('sourceHead').innerHTML=`<div class="glyph">${icon('cube')}</div><div class="grow"><h2>${esc(t('src.choose'))}</h2><div class="sub">${esc(t('src.choose_note'))}</div></div>`;return}const stl=S.parts.filter(p=>p.filetype==='stl').length;const action=S.selectedAdded?`<button id="deleteSelected" class="ghost" title="${esc(t('src.delete_tip'))}">${icon('x')}${esc(t('src.delete'))}</button>`:`<button id="hideSelected" class="ghost" title="${esc(t('src.hide_tip'))}">${icon('eye-off')}${esc(t('src.hide'))}</button>`;$('sourceHead').innerHTML=`<div class="glyph ${kindClass(S.selectedKind,S.selectedAdded)}">${icon(S.selectedKind==='folder'?'models':'zip')}</div><div class="grow"><h2>${esc(S.selectedName)}</h2><div class="sub">${esc(t('src.summary',{n:S.parts.length,stl,path:S.selected}))}</div></div>${action}`;if(S.selectedAdded)$('deleteSelected').onclick=()=>ask(t('delete.title'),t('delete.body',{name:S.selectedName}),'delete',{path:S.selected},t('src.delete'));else $('hideSelected').onclick=()=>send('hide_source',{path:S.selected})}
function partMeta(p){if(Array.isArray(p.dims)&&p.dims.length===3&&p.filetype==='stl')return esc(t('parts.meta',{dims:p.dims.map(v=>fmt(v)).join(' × '),facets:Number(p.facets||0).toLocaleString(locale())}));return esc(t('parts.catalog_only',{type:(p.filetype||'model').toUpperCase()}))}
function renderParts(note){if(!S.parts.length){$('parts').innerHTML=`<div class="empty">${icon('cube')}<div>${esc(note||(S.selected?t('parts.no_files'):t('parts.placeholder')))}</div></div>`;updateActions();return}$('parts').innerHTML=S.parts.map((p,i)=>{const usable=meshPart(p);return `<div class="part ${usable?'':p.open_in_orca?'ext':'off'} ${S.previewIndex===i?'on':''}" data-index="${i}"><input class="check" type="checkbox" data-check="${i}" ${p.included?'checked':''} ${usable?'':'disabled'}><div class="t"><div class="n">${esc(p.name)}</div><div class="m">${partMeta(p)}${p.warning?` · <span class="warn">${esc(p.warning)}</span>`:''}</div></div>${p.open_in_orca?`<button class="small" data-orca="${i}">${esc(t('btn.open_in_orca'))}</button>`:''}</div>`}).join('');
 document.querySelectorAll('[data-orca]').forEach(el=>el.onclick=e=>{e.stopPropagation();if(!S.busy)send('open_in_orca',{index:Number(el.dataset.orca)})});document.querySelectorAll('[data-check]').forEach(el=>{el.onclick=e=>e.stopPropagation();el.onchange=()=>{S.parts[Number(el.dataset.check)].included=el.checked;invalidatePlan();updateActions()}});document.querySelectorAll('.part:not(.off):not(.ext)').forEach(el=>el.onclick=()=>{S.previewIndex=Number(el.dataset.index);document.querySelectorAll('.part').forEach(x=>x.classList.toggle('on',x===el));send('preview_part',{index:S.previewIndex,max_facets:previewBudget()})});updateActions()}
function invalidatePlan(){if(S.plan){S.plan=null;renderPlan(null)}}

/* 3D preview */
function accentRgb(){const v=getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();const m=/^#([0-9a-f]{6})$/i.exec(v);if(m){const n=parseInt(m[1],16);return [n>>16&255,n>>8&255,n&255]}const r=/rgba?\((\d+)[ ,]+(\d+)[ ,]+(\d+)/.exec(v);return r?[+r[1],+r[2],+r[3]]:[0,150,136]}
function drawEmptyView(){const c=$('view');const ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);glClear();$('viewEmpty').hidden=false;$('viewHint').hidden=true;$('resetView').hidden=true;$('viewTitle').textContent='';$('viewMeta').textContent=''}
function fitZoom(lo,hi){const bed=Math.max(Number(S.profile?.size?.x)||250,Number(S.profile?.size?.y)||250),part=Math.max(1,...hi.map((v,i)=>v-lo[i]));return Math.max(1,Math.min(2.4,.5*bed/part))}
/* WebGL draws the part with a depth buffer on #viewGl, over the 2D canvas that keeps the bed,
   grid, axes, shadow and labels. Same projection and shading as the 2D code in renderMesh(),
   which still draws the part when WebGL is missing, turned off in the settings, lost or failing. */
const GL={ctx:null,prog:null,mesh:null,failed:false};
function glUsable(){if(GL.failed||S.config.gpu_preview===false)return null;try{if(!GL.ctx){const c=$('viewGl');GL.ctx=c.getContext('webgl',{antialias:true});if(!GL.ctx){GL.failed=true;return null}c.addEventListener('webglcontextlost',e=>{e.preventDefault();GL.prog=null;GL.mesh=null});c.addEventListener('webglcontextrestored',()=>renderMesh())}return GL.ctx.isContextLost()?null:GL.ctx}catch(e){GL.failed=true;return null}}
// Facets to ask the plugin for: 0 means its full preview limit; the 2D fallback draws 30 000 at most.
function previewBudget(){return glUsable()?0:30000}
function glClear(){const gl=GL.ctx;if(gl&&!gl.isContextLost()){gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT)}}
function glProgram(gl){const vs='attribute vec3 p;attribute vec3 n;uniform mat3 R;uniform vec3 C;uniform vec4 V;uniform float D;varying float k;varying float rim;void main(){vec3 q=R*(p-C);vec3 m=R*n;k=.32+.68*abs(dot(m,normalize(vec3(.35,-.55,.76))));rim=pow(1.-abs(m.z),3.);gl_Position=vec4((V.x+q.x*V.z)*2.-1.,1.-(V.y+q.y*V.w)*2.,-q.z/D,1.);}',
 fs='precision mediump float;uniform vec3 A;uniform vec3 B;varying float k;varying float rim;void main(){gl_FragColor=vec4(min((A*k+vec3(40.,40.,48.)*rim+B)/255.,1.),1.);}';
 const sh=(type,src)=>{const s=gl.createShader(type);gl.shaderSource(s,src);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(s));return s};
 const prog=gl.createProgram();gl.attachShader(prog,sh(gl.VERTEX_SHADER,vs));gl.attachShader(prog,sh(gl.FRAGMENT_SHADER,fs));gl.linkProgram(prog);if(!gl.getProgramParameter(prog,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(prog));
 const loc={p:gl.getAttribLocation(prog,'p'),n:gl.getAttribLocation(prog,'n')};for(const u of ['R','C','V','D','A','B'])loc[u]=gl.getUniformLocation(prog,u);return {prog,loc,pos:gl.createBuffer(),nrm:gl.createBuffer()}}
function glUpload(gl,m){const f=m.f,nrm=new Float32Array(f.length);for(let i=0;i+8<f.length;i+=9){const ux=f[i+3]-f[i],uy=f[i+4]-f[i+1],uz=f[i+5]-f[i+2],vx=f[i+6]-f[i],vy=f[i+7]-f[i+1],vz=f[i+8]-f[i+2];let x=uy*vz-uz*vy,y=uz*vx-ux*vz,z=ux*vy-uy*vx;const l=Math.hypot(x,y,z)||1;x/=l;y/=l;z/=l;for(let j=0;j<9;j+=3){nrm[i+j]=x;nrm[i+j+1]=y;nrm[i+j+2]=z}}
 gl.bindBuffer(gl.ARRAY_BUFFER,GL.prog.pos);gl.bufferData(gl.ARRAY_BUFFER,f,gl.STATIC_DRAW);gl.bindBuffer(gl.ARRAY_BUFFER,GL.prog.nrm);gl.bufferData(gl.ARRAY_BUFFER,nrm,gl.STATIC_DRAW);GL.mesh=m}
function glDraw(m,v){// While the browser restores a lost context, draw nothing rather than a large mesh in 2D.
 if(GL.ctx&&GL.ctx.isContextLost()&&!GL.failed&&S.config.gpu_preview!==false)return true;const gl=glUsable();if(!gl)return false;try{const c=$('viewGl'),W=Math.round(v.w*v.dpr),H=Math.round(v.h*v.dpr);if(c.width!==W||c.height!==H){c.width=W;c.height=H}
 if(!GL.prog)GL.prog=glProgram(gl);if(GL.mesh!==m)glUpload(gl,m);const P=GL.prog,L=P.loc;gl.viewport(0,0,W,H);gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.enable(gl.DEPTH_TEST);gl.depthFunc(gl.LESS);gl.useProgram(P.prog);
 gl.bindBuffer(gl.ARRAY_BUFFER,P.pos);gl.enableVertexAttribArray(L.p);gl.vertexAttribPointer(L.p,3,gl.FLOAT,false,0,0);gl.bindBuffer(gl.ARRAY_BUFFER,P.nrm);gl.enableVertexAttribArray(L.n);gl.vertexAttribPointer(L.n,3,gl.FLOAT,false,0,0);
 // Columns of rot() in renderMesh(): a turn about Z by the yaw, then about X by the pitch.
 gl.uniformMatrix3fv(L.R,false,[v.cyw,v.cp*v.syw,v.sp*v.syw,-v.syw,v.cp*v.cyw,v.sp*v.cyw,0,-v.sp,v.cp]);gl.uniform3f(L.C,v.cx,v.cy,v.z0);gl.uniform4f(L.V,v.ox/v.w,v.oy/v.h,v.scale/v.w,v.scale/v.h);
 gl.uniform1f(L.D,Math.hypot(m.hi[0]-m.lo[0],m.hi[1]-m.lo[1],m.hi[2]-m.lo[2])+1);gl.uniform3f(L.A,v.rgb[0],v.rgb[1],v.rgb[2]);gl.uniform3f(L.B,v.dark?14:0,v.dark?14:0,v.dark?18:0);
 gl.drawArrays(gl.TRIANGLES,0,Math.floor(m.f.length/3));const err=gl.getError();if(err&&err!==gl.CONTEXT_LOST_WEBGL)throw new Error('WebGL error '+err);return true}
 catch(e){console.warn('PlatePilot: the WebGL preview failed, drawing in 2D',e);GL.failed=true;glClear();
 // A full preview is too heavy for the 2D canvas: ask for the simplified one.
 if(m.f.length/9>30000&&S.previewIndex>=0)setTimeout(()=>send('preview_part',{index:S.previewIndex,max_facets:previewBudget()}),0);return false}}
function drawMesh(data){if(!data||!data.vertices){S.mesh=null;drawEmptyView();return}const bin=atob(data.vertices),arr=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)arr[i]=bin.charCodeAt(i);const f=new Float32Array(arr.buffer,0,arr.length>>2);let lo=[Infinity,Infinity,Infinity],hi=[-Infinity,-Infinity,-Infinity];for(let i=0;i<f.length;i+=3)for(let a=0;a<3;a++){lo[a]=Math.min(lo[a],f[i+a]);hi[a]=Math.max(hi[a],f[i+a])}S.mesh={f,lo,hi,yaw:.62,pitch:1.02,zoom:fitZoom(lo,hi),dims:data.dims,data};$('viewEmpty').hidden=true;$('viewHint').hidden=false;$('resetView').hidden=false;$('viewTitle').textContent=data.name;const shown=data.facets>f.length/9?t('view.shown',{n:Number(f.length/9).toLocaleString(locale())}):'';$('viewMeta').textContent=t('view.meta',{dims:data.dims.map(v=>fmt(v)).join(' × '),facets:Number(data.facets).toLocaleString(locale())})+shown;renderMesh()}
function renderMesh(){const m=S.mesh,c=$('view');if(!m)return;const dpr=window.devicePixelRatio||1,w=Math.max(1,c.clientWidth),h=Math.max(1,c.clientHeight);c.width=w*dpr;c.height=h*dpr;const ctx=c.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
const bx=Number(S.profile?.size?.x)||250,by=Number(S.profile?.size?.y)||250,f=m.f,cx=(m.lo[0]+m.hi[0])/2,cy=(m.lo[1]+m.hi[1])/2,z0=m.lo[2];
const cyw=Math.cos(m.yaw),syw=Math.sin(m.yaw),cp=Math.cos(m.pitch),sp=Math.sin(m.pitch);
const rot=(x,y,z)=>{const x1=cyw*x-syw*y,y1=syw*x+cyw*y;return [x1,cp*y1-sp*z,sp*y1+cp*z]};
const corners=[[-bx/2,-by/2,0],[bx/2,-by/2,0],[bx/2,by/2,0],[-bx/2,by/2,0]];const top=Math.max(1,m.hi[2]-m.lo[2]);const probe=corners.concat(corners.map(p=>[p[0],p[1],top])).map(p=>rot(...p));
const minX=Math.min(...probe.map(p=>p[0])),maxX=Math.max(...probe.map(p=>p[0])),minY=Math.min(...probe.map(p=>p[1])),maxY=Math.max(...probe.map(p=>p[1]));
const scale=Math.min((w-40)/(maxX-minX),(h-90)/(maxY-minY))*m.zoom,ox=w/2-(minX+maxX)/2*scale,oy=h/2+18-(minY+maxY)/2*scale;const P=p=>[ox+p[0]*scale,oy+p[1]*scale];
const dark=document.documentElement.dataset.orcaTheme==='dark',[ar,ag,ab]=accentRgb();
const bed=corners.map(p=>P(rot(...p)));ctx.beginPath();bed.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.closePath();ctx.fillStyle=dark?'rgba(255,255,255,.045)':'rgba(20,40,60,.045)';ctx.fill();
ctx.lineWidth=.6;for(let x=-bx/2;x<=bx/2+.01;x+=10){const major=Math.abs((x+bx/2)%50)<.01;ctx.strokeStyle=dark?`rgba(255,255,255,${major?.14:.05})`:`rgba(20,40,60,${major?.16:.06})`;const a=P(rot(x,-by/2,0)),b=P(rot(x,by/2,0));ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);ctx.stroke()}
for(let y=-by/2;y<=by/2+.01;y+=10){const major=Math.abs((y+by/2)%50)<.01;ctx.strokeStyle=dark?`rgba(255,255,255,${major?.14:.05})`:`rgba(20,40,60,${major?.16:.06})`;const a=P(rot(-bx/2,y,0)),b=P(rot(bx/2,y,0));ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);ctx.stroke()}
ctx.beginPath();bed.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.closePath();ctx.strokeStyle=`rgba(${ar},${ag},${ab},.85)`;ctx.lineWidth=1.6;ctx.stroke();
const origin=P(rot(-bx/2,-by/2,0)),ax=P(rot(-bx/2+Math.min(40,bx/5),-by/2,0)),ay=P(rot(-bx/2,-by/2+Math.min(40,by/5),0));ctx.lineWidth=2.2;ctx.strokeStyle='#e5484d';ctx.beginPath();ctx.moveTo(...origin);ctx.lineTo(...ax);ctx.stroke();ctx.strokeStyle='#30a46c';ctx.beginPath();ctx.moveTo(...origin);ctx.lineTo(...ay);ctx.stroke();
const hx=(m.hi[0]-m.lo[0])/2,hy=(m.hi[1]-m.lo[1])/2,shadow=[[-hx,-hy,0],[hx,-hy,0],[hx,hy,0],[-hx,hy,0]].map(p=>P(rot(...p)));ctx.beginPath();shadow.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.closePath();ctx.fillStyle='rgba(0,0,0,.13)';ctx.filter='blur(6px)';ctx.fill();ctx.filter='none';
if(!glDraw(m,{w,h,dpr,ox,oy,scale,cyw,syw,cp,sp,cx,cy,z0,dark,rgb:[ar,ag,ab]})){const L=[.35,-.55,.76],ln=Math.hypot(...L),lx=L[0]/ln,ly=L[1]/ln,lz=L[2]/ln,tris=[];
for(let i=0;i+8<f.length;i+=9){const a=rot(f[i]-cx,f[i+1]-cy,f[i+2]-z0),b=rot(f[i+3]-cx,f[i+4]-cy,f[i+5]-z0),d=rot(f[i+6]-cx,f[i+7]-cy,f[i+8]-z0);const ux=b[0]-a[0],uy=b[1]-a[1],uz=b[2]-a[2],vx=d[0]-a[0],vy=d[1]-a[1],vz=d[2]-a[2];let nx=uy*vz-uz*vy,ny=uz*vx-ux*vz,nz=ux*vy-uy*vx;const nl=Math.hypot(nx,ny,nz)||1;nx/=nl;ny/=nl;nz/=nl;const lit=Math.abs(nx*lx+ny*ly+nz*lz);tris.push({a:P(a),b:P(b),d:P(d),z:a[2]+b[2]+d[2],k:.32+.68*lit,rim:Math.pow(1-Math.abs(nz),3)})}
tris.sort((p,q)=>p.z-q.z);for(const tr of tris){const k=tr.k,r=Math.min(255,ar*k+40*tr.rim+(dark?14:0)),g=Math.min(255,ag*k+40*tr.rim+(dark?14:0)),b=Math.min(255,ab*k+48*tr.rim+(dark?18:0));ctx.beginPath();ctx.moveTo(...tr.a);ctx.lineTo(...tr.b);ctx.lineTo(...tr.d);ctx.closePath();ctx.fillStyle=`rgb(${r|0},${g|0},${b|0})`;ctx.fill();ctx.strokeStyle=ctx.fillStyle;ctx.lineWidth=.5;ctx.stroke()}}
ctx.fillStyle=dark?'rgba(255,255,255,.55)':'rgba(30,40,50,.55)';ctx.font='11px system-ui,sans-serif';ctx.textAlign='right';ctx.fillText(`${fmt(bx)} × ${fmt(by)} ${t('unit.mm')}`,w-12,h-12)}

/* plan */
function renderPlan(plan){S.plan=plan;updateActions();if(!$('zoom').hidden)closeZoom();if(!plan){$('zoomHint').hidden=true;$('summary').innerHTML='';$('plates').innerHTML='';return}const bx=plan.bed.x,by=plan.bed.y;const pills=[`<span class="pill accent">${esc(plural('plan.parts',plan.total))}</span>`,`<span class="pill accent">${esc(plural('plan.plates',plan.plates.length))}</span>`].concat((plan.problems||[]).map(p=>`<span class="pill warn">${esc(t('plan.not_fit',{name:p}))}</span>`)).concat((plan.too_tall||[]).map(p=>`<span class="pill warn">${esc(t('plan.too_tall',{name:p}))}</span>`));if(plan.exclusions)pills.push(`<span class="pill warn">${esc(t('plan.zones'))}</span>`);if(plan.count_note==='at_least')pills.push(`<span class="pill warn">${esc(t('plan.at_least',{plates:plural('plan.plates',plan.min_plates)}))}</span>`);if(plan.count_note==='at_most')pills.push(`<span class="pill warn">${esc(t('plan.at_most'))}</span>`);if(plan.smart===true)pills.unshift(`<span class="pill accent">${esc(t('plan.smart'))}</span>`);$('summary').innerHTML=pills.join('');
$('plates').innerHTML=plan.plates.map((p,pi)=>`<div class="plate" data-plate="${pi}"><div class="plate-head"><b>${esc(t('plan.plate',{n:pi+1}))}</b><span>${esc(plateMeta(p))}</span></div>${plateSvg(p,pi,false)}</div>`).join('');
 $('zoomHint').hidden=!plan.plates.length}
function plateMeta(p){const used=p.items.reduce((s,it)=>s+it.w*it.h,0)/(S.plan.bed.x*S.plan.bed.y)*100;return t('plan.plate_meta',{parts:plural('plan.parts',p.items.length),fill:fmt(used,0)})}
function itemTitle(it){return `${it.name} · ${fmt(it.w)} × ${fmt(it.h)} ${t('unit.mm')}${(it.angle||it.rotated)?t('plan.rotated',{n:((it.angle||0)+(it.rotated?90:0))%360}):''}`}
// The plate as seen from above, front edge down. The close-up numbers every part (the list beside it
// names them) and labels the ones wide enough; the thumbnail only labels the wide ones.
function plateSvg(p,pi,big){const bx=S.plan.bed.x,by=S.plan.bed.y;let grid='';for(let x=50;x<bx;x+=50)grid+=`<line x1="${x}" y1="0" x2="${x}" y2="${by}"/>`;for(let y=50;y<by;y+=50)grid+=`<line x1="0" y1="${y}" x2="${bx}" y2="${y}"/>`;
 const items=p.items.map((it,ii)=>{const col=PALETTE[(pi*3+ii)%PALETTE.length],y=by-it.y-it.h,cx=it.x+it.w/2,cy=y+it.h/2,fs=Math.max(big?4:5,Math.min(it.w/7,it.h/2.6,big?9:13));
  const name=esc(it.name.replace(/\.stl$/i,'').slice(0,Math.max(3,Math.floor(it.w/fs*1.7))));let label='';
  if(big){const r=Math.max(3.2,Math.min(6,it.w/4,it.h/4)),wide=it.w>30&&it.h>16;label=`<circle cx="${cx}" cy="${wide?cy-fs*.8:cy}" r="${r}" fill="${col}"/><text x="${cx}" y="${wide?cy-fs*.8:cy}" font-size="${r*1.25}" font-weight="700" text-anchor="middle" dominant-baseline="central" fill="#fff">${ii+1}</text>`+(wide?`<text x="${cx}" y="${cy+fs*.9}" font-size="${fs}" text-anchor="middle" dominant-baseline="central" fill="currentColor">${name}</text>`:'')}
  else if(it.w>22&&it.h>10)label=`<text x="${cx}" y="${cy}" font-size="${fs}" text-anchor="middle" dominant-baseline="central" fill="currentColor">${name}</text>`;
  return `<g><title>${esc(itemTitle(it))}</title><rect x="${it.x}" y="${y}" width="${it.w}" height="${it.h}" rx="2.5" fill="${col}" fill-opacity=".26" stroke="${col}" stroke-width="${big?.8:1.2}"/>${label}</g>`}).join('');
 return `<svg viewBox="-2 -2 ${bx+4} ${by+4}"><rect x="0" y="0" width="${bx}" height="${by}" rx="4" fill="var(--surface-2)" stroke="var(--accent-line)" stroke-width="1.2"/><g stroke="var(--line)" stroke-width=".6">${grid}</g>${items}</svg>`}
function openZoom(pi){const plates=S.plan&&S.plan.plates||[];if(!plates.length)return;S.zoom=(pi+plates.length)%plates.length;const p=plates[S.zoom];
 $('zoomTitle').textContent=t('plan.zoom_title',{n:S.zoom+1,total:plates.length});$('zoomMeta').textContent=plateMeta(p);$('zoomPlate').innerHTML=plateSvg(p,S.zoom,true);
 $('zoomList').innerHTML=p.items.map((it,ii)=>`<li><span class="no" style="color:${PALETTE[(S.zoom*3+ii)%PALETTE.length]}">${ii+1}</span><span class="nm">${esc(it.name)}</span><span class="dm">${esc(fmt(it.w)+' × '+fmt(it.h)+' '+t('unit.mm')+((it.angle||it.rotated)?t('plan.rotated',{n:((it.angle||0)+(it.rotated?90:0))%360}):''))}</span></li>`).join('');
 $('zoomPrev').disabled=$('zoomNext').disabled=plates.length<2;$('zoom').hidden=false;$('zoomClose').focus()}
function closeZoom(){$('zoom').hidden=true;S.zoom=null}
function setMode(mode,save=true){S.mode=mode==='orca'?'orca':'own';document.querySelectorAll('#modeSeg button').forEach(b=>b.classList.toggle('on',b.dataset.mode===S.mode));const own=S.mode==='own';$('marginField').hidden=!own;$('platesField').hidden=!own;$('plan').hidden=!own;$('planSmart').hidden=!own;$('sendPrepare').hidden=!own;$('nativeArrange').hidden=own;$('modeNote').textContent=t(own?'mode.own_note':'mode.orca_note');if(!own)renderPlan(null);if(save)saveUiSettings()}

/* wiring */
$('openSettings').onclick=()=>openDrawer(true);$('closeSettings').onclick=()=>openDrawer(false);$('scrim').onclick=()=>openDrawer(false);
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(!$('modal').hidden)finishAsk(false);else openDrawer(false)}});
$('pickFolder').onclick=()=>{if(S.busy)return;S.scanning=true;status(t('st.opening_picker'));send('pick_folder',{initial:$('folder').value.trim()||(S.config.folders||[])[0]||''})};
$('addFolder').onclick=()=>{const path=$('folder').value.trim();if(path&&!S.busy){S.scanning=true;setBusy(true);send('request_folder_add',{path})}};$('folder').onkeydown=e=>{if(e.key==='Enter')$('addFolder').click()};
const pickFiles=()=>{if(!S.busy){openDrawer(false);$('filePick').value='';$('filePick').click()}};$('addFiles').onclick=pickFiles;$('addFiles2').onclick=pickFiles;
$('filePick').onchange=()=>{const files=Array.from($('filePick').files||[]);if(files.length)importFiles(files.map(f=>({file:f,top:'',rel:f.name})))};
$('scan').onclick=requestScan;$('filter').oninput=renderArchives;$('rootFilter').onchange=()=>{S.root=$('rootFilter').value;renderArchives()};$('markSeen').onclick=()=>send('mark_seen');
document.querySelectorAll('#kindChips .chip').forEach(b=>b.onclick=()=>{S.kind=b.dataset.kind;document.querySelectorAll('#kindChips .chip').forEach(x=>x.classList.toggle('on',x===b));renderArchives()});
for(const id of ['checkOnOpen','recursive'])$(id).onchange=saveUiSettings;
$('gpuPreview').onchange=()=>{saveUiSettings();glClear();if(S.mesh&&S.previewIndex>=0)send('preview_part',{index:S.previewIndex,max_facets:previewBudget()});else renderMesh()};
for(const id of ['modelStl','modelStep','model3mf'])$(id).onchange=()=>{$(id).parentElement.classList.toggle('on',$(id).checked);saveUiSettings()};
$('language').onchange=()=>{saveUiSettings();send('init')};
$('margin').onchange=()=>{invalidatePlan();saveUiSettings()};
$('all').onclick=()=>{S.parts.forEach(p=>{if(p.filetype==='stl'&&p.member&&!p.blocked)p.included=true});invalidatePlan();renderParts()};$('none').onclick=()=>{S.parts.forEach(p=>p.included=false);invalidatePlan();renderParts()};
document.querySelectorAll('#modeSeg button').forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
// Empty or 0 is "auto": as few plates as the parts fit on.
function wantedPlates(){const v=Math.round(Number($('plateCount').value));return v>0?Math.min(50,v):0}
$('plateCount').onchange=()=>{const v=wantedPlates();$('plateCount').value=v||''};
$('plan').onclick=()=>{renderPlan(null);send('plan',{margin:Number($('margin').value)||0,plates:wantedPlates(),included:selectedStl().map(p=>p.member)})};
$('planSmart').onclick=()=>{renderPlan(null);send('plan',{margin:Number($('margin').value)||0,plates:wantedPlates(),included:selectedStl().map(p=>p.member),smart:true})};
$('sendPrepare').onclick=()=>{if(!S.busy){status(t('st.building'));send('export_project')}};
$('nativeArrange').onclick=()=>{if(!S.busy)send('native_arrange',{included:selectedStl().map(p=>p.member)})};
$('modalYes').onclick=()=>finishAsk(true);$('modalNo').onclick=()=>finishAsk(false);
const view=$('view');view.onpointerdown=e=>{if(!S.mesh)return;S.drag={x:e.clientX,y:e.clientY};view.setPointerCapture(e.pointerId)};view.onpointermove=e=>{if(!S.drag||!S.mesh)return;S.mesh.yaw+=(e.clientX-S.drag.x)*.01;S.mesh.pitch=Math.max(.05,Math.min(1.55,S.mesh.pitch+(e.clientY-S.drag.y)*.01));S.drag={x:e.clientX,y:e.clientY};renderMesh()};view.onpointerup=()=>S.drag=null;
view.onwheel=e=>{if(!S.mesh)return;e.preventDefault();S.mesh.zoom=Math.max(.4,Math.min(6,S.mesh.zoom*(e.deltaY<0?1.12:1/1.12)));renderMesh()};
const resetView=()=>{if(S.mesh){S.mesh.yaw=.62;S.mesh.pitch=1.02;S.mesh.zoom=fitZoom(S.mesh.lo,S.mesh.hi);renderMesh()}};view.ondblclick=resetView;$('resetView').onclick=resetView;
window.onresize=()=>renderMesh();new ResizeObserver(()=>renderMesh()).observe(view);

/* folders are checked again when the page comes back into view; nothing runs while it is hidden */
const recheck=()=>{if(document.visibilityState==='visible'&&S.config.check_on_open!==false&&S.lastScan&&!S.busy&&Date.now()-S.lastScan>60000)requestScan()};
document.addEventListener('visibilitychange',recheck);window.addEventListener('focus',recheck);

$('plates').addEventListener('click',e=>{const card=e.target.closest('.plate');if(card)openZoom(Number(card.dataset.plate))});
$('zoomClose').onclick=closeZoom;$('zoomPrev').onclick=()=>openZoom(S.zoom-1);$('zoomNext').onclick=()=>openZoom(S.zoom+1);
$('zoom').addEventListener('click',e=>{if(e.target===$('zoom'))closeZoom()});
document.addEventListener('keydown',e=>{if($('zoom').hidden)return;if(e.key==='Escape'){e.preventDefault();closeZoom()}else if(e.key==='ArrowLeft')openZoom(S.zoom-1);else if(e.key==='ArrowRight')openZoom(S.zoom+1)});
/* drag and drop, paste */
let dragDepth=0;
const dragHasFiles=e=>Array.from(e.dataTransfer?.types||[]).includes('Files');
document.addEventListener('dragenter',e=>{if(!dragHasFiles(e))return;e.preventDefault();dragDepth++;$('drop').hidden=false});
document.addEventListener('dragover',e=>{if(!dragHasFiles(e))return;e.preventDefault();e.dataTransfer.dropEffect=S.busy?'none':'copy'});
document.addEventListener('dragleave',e=>{if(!dragHasFiles(e))return;dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)$('drop').hidden=true});
document.addEventListener('drop',e=>{if(!dragHasFiles(e))return;e.preventDefault();dragDepth=0;$('drop').hidden=true;if(S.busy)return;const entries=Array.from(e.dataTransfer.items||[]).map(i=>i.webkitGetAsEntry?i.webkitGetAsEntry():null).filter(Boolean),files=Array.from(e.dataTransfer.files||[]);collectEntries(entries,files).then(importFiles,err=>status(String(err&&err.message||err),'error'))});
document.addEventListener('paste',e=>{const el=e.target;if(el&&(el.tagName==='INPUT'||el.tagName==='TEXTAREA'||el.isContentEditable)||S.busy)return;const files=Array.from(e.clipboardData?.files||[]);if(files.length){e.preventDefault();importFiles(files.map(f=>({file:f,top:'',rel:f.name})));return}const text=(e.clipboardData?.getData('text')||'').trim();if(text&&text.length<4000&&/^["']?([a-zA-Z]:[\\/]|\\\\|\/|~)/.test(text)){e.preventDefault();S.scanning=true;setBusy(true);send('paste_path',{text})}});
const entryFile=en=>new Promise((res,rej)=>en.file(res,rej));
async function walkEntry(dir,top,prefix,out,depth){if(depth>8||out.length>2000)return;const reader=dir.createReader();for(;;){const batch=await new Promise((res,rej)=>reader.readEntries(res,rej));if(!batch.length)break;for(const en of batch){if(en.name.startsWith('.'))continue;if(en.isFile)out.push({entry:en,top,rel:prefix+en.name});else if(en.isDirectory)await walkEntry(en,top,prefix+en.name+'/',out,depth+1)}}}
async function collectEntries(entries,files){const out=[];if(!entries.length){files.forEach(f=>out.push({file:f,top:'',rel:f.name}));return out}for(const en of entries){if(en.isFile)out.push({entry:en,top:'',rel:en.name});else if(en.isDirectory)await walkEntry(en,en.name,'',out,0)}return out}
function importRequest(type,extra){return new Promise((res,rej)=>{const timer=setTimeout(()=>{if(S.importWait&&S.importWait.res===done){S.importWait=null;rej(new Error(t('st.no_answer')))}},30000);const done=v=>{clearTimeout(timer);res(v)};S.importWait={res:done,rej:e=>{clearTimeout(timer);rej(e)}};send(type,extra)})}
function toBase64(bytes){let s='';for(let i=0;i<bytes.length;i+=0x8000)s+=String.fromCharCode.apply(null,bytes.subarray(i,i+0x8000));return btoa(s)}
async function importFiles(list){const items=list.filter(x=>IMPORT_RE.test(x.rel)),skipped=list.length-items.length,extra=skipped?t('st.import_skipped',{n:skipped}):'';if(!items.length){status(t('st.import_none')+extra,'warn');return}const loose=items.filter(x=>!x.top&&!/\.zip$/i.test(x.rel));S.importing=true;setBusy(true);
 try{send('import_start',{loose:loose.length,first:loose.length?loose[0].rel:''});
  for(let i=0;i<items.length;i++){const it=items[i],file=it.file||await entryFile(it.entry);const progress=done=>status(t('st.importing',{name:file.name,i:i+1,n:items.length,pct:Math.round(done/Math.max(1,file.size)*100)}));progress(0);let reply=await importRequest('import_begin',{name:file.name,top:it.top,rel:it.rel,size:file.size}),offset=0;
   while(!reply.done){const bytes=new Uint8Array(await file.slice(offset,offset+CHUNK).arrayBuffer());if(!bytes.length)throw new Error(t('err.import_state'));offset+=bytes.length;progress(offset);reply=await importRequest('import_chunk',{data:toBase64(bytes)})}}
  S.importing=false;send('import_finish')}
 catch(err){S.importing=false;S.importWait=null;send('import_cancel');setBusy(false);status(String(err&&err.message||err)+extra,'error')}}

orca.onMessage(msg=>{const k=msg.type;
 if(k==='init'){S.strings=msg.strings||{};S.lang=msg.language||'en';S.platform=msg.platform||'windows';applyStrings();const win=S.platform==='windows';$('pickFolder').hidden=!win;$('pasteNote').textContent=t(win?'drawer.paste':'drawer.paste_only');applyConfig(msg.config||{});setProfile(msg.profile);renderSourceHead();renderParts();renderArchives();renderFolders();if(S.plan)renderPlan(S.plan);if(S.mesh)drawMesh(S.mesh.data);else drawEmptyView();if(msg.scan_interrupted&&!S.lastScan)status(t('st.scan_interrupted'),'warn');else if(!S.lastScan&&S.config.check_on_open!==false)requestScan();else if(!S.lastScan)status(t((msg.config?.folders||[]).length?'st.press_refresh':'st.choose_folder'))}
 else if(k==='config'){applyConfig(msg.config||{})}
 else if(k==='profile'){setProfile(msg.profile)}
 else if(k==='archives'){S.archives=msg.archives||[];S.folders=msg.folders||[];renderArchives();renderFolders();if(S.scanning){S.scanning=false;if(!S.importing)setBusy(false);const n=S.archives.filter(a=>a.new).length,sources=plural('folders.sources',S.archives.filter(a=>a.likely!==false).length);if(!S.archives.length&&!S.folders.length)status(t('st.choose_folder'));else status(n?t('st.scanned_new',{sources,n}):t('st.scanned',{sources}),'ok')}}
 else if(k==='folder_selected'){if(msg.path)$('folder').value=msg.path}
 else if(k==='folder_list'){S.config.folders=msg.folders||[];renderFolders();renderArchives()}
 else if(k==='archive'){S.selected=msg.path;S.selectedKind=msg.kind;S.selectedName=msg.name;S.selectedAdded=!!msg.added;S.parts=msg.parts||[];S.previewIndex=-1;S.mesh=null;if(!S.importing)setBusy(false);renderSourceHead();renderArchives();renderParts();renderPlan(null);drawEmptyView();status(msg.error||t('st.opened_source',{name:msg.name}),msg.error?'warn':'info')}
 else if(k==='import_next'){if(S.importWait){const w=S.importWait;S.importWait=null;w.res(msg)}}
 else if(k==='preview'){drawMesh(msg)}
 else if(k==='plan'){setProfile(msg.profile);renderPlan(msg);status(t(msg.exportable?'st.plan_ready':'st.plan_problems'),msg.exportable?'ok':'warn')}
 else if(k==='project_ready'){setBusy(false);status(msg.opened?t('st.project_opened'):t('st.project_saved',{path:msg.path,error:msg.error||''}),msg.opened?'ok':'warn')}
 else if(k==='native_arrange_started'){setBusy(true);status(t('st.arranging',{n:msg.count}));scheduleNativeTick()}
 else if(k==='native_arrange_progress'){if(msg.done){stopNativeTicks();setBusy(false);status(msg.message||t('st.arrange_done'),msg.ok?'ok':'error')}else scheduleNativeTick()}
 else if(k==='notice'){S.scanning=false;if(!S.nativeTick&&!S.importing)setBusy(false);status(msg.message||'',msg.level||'info')}
 else if(k==='error'){S.scanning=false;if(S.importWait){const w=S.importWait;S.importWait=null;w.rej(new Error(msg.message||t('st.plugin_error')));return}stopNativeTicks();setBusy(false);status(msg.message||t('st.plugin_error'),'error')}
});
send('init');
</script>
</body>
</html>"""


def _fmt_size(size):
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _model_type_for_name(name):
    return MODEL_EXTENSIONS.get(os.path.splitext(str(name).lower())[1])


def _type_counts(names):
    counts = {name: 0 for name in MODEL_TYPE_LABELS}
    for name in names:
        model_type = _model_type_for_name(name)
        if model_type:
            counts[model_type] += 1
    return counts


def _type_summary(counts):
    return " · ".join(f"{MODEL_TYPE_LABELS[t]} {counts.get(t, 0)}" for t in MODEL_TYPE_LABELS if counts.get(t, 0))


def _clean_folder_input(raw):
    """Turn a typed or pasted folder path into an absolute, normalized path.

    Explorer's "Copy as path" wraps the path in quotes, and a relative path would
    silently resolve against Orca's working directory, so both are handled here.
    """
    text = str(raw or "").strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if not text:
        raise ValueError(_t("err.path_empty"))
    text = os.path.expandvars(os.path.expanduser(text))
    if os.name == "nt" and re.fullmatch(r"[A-Za-z]:", text):
        text += "\\"
    if not os.path.isabs(text):
        raise ValueError(_t("err.path_relative", example="C:\\Models" if os.name == "nt" else "~/Models"))
    return os.path.normpath(os.path.abspath(text))


def _folder_key(path):
    return os.path.normcase(os.path.realpath(path))


def _is_inside(path, root):
    try:
        return os.path.commonpath([_folder_key(path), _folder_key(root)]) == _folder_key(root)
    except ValueError:
        return False


def _owner_window():
    import ctypes
    user = ctypes.WinDLL("user32")
    user.GetForegroundWindow.restype = ctypes.c_void_p
    user.GetAncestor.restype = ctypes.c_void_p
    user.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    hwnd = user.GetForegroundWindow()
    return user.GetAncestor(hwnd, 2) if hwnd else None  # GA_ROOT


def _pick_folder_modern(initial=None):
    """Show the Windows Vista+ folder picker (IFileOpenDialog with FOS_PICKFOLDERS).

    Returns the selected path, or None when the user cancels.
    """
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

    ole = ctypes.WinDLL("ole32")
    shell = ctypes.WinDLL("shell32")
    for fn in (ole.CLSIDFromString, ole.CoInitializeEx, ole.CoCreateInstance, shell.SHCreateItemFromParsingName):
        fn.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None

    def guid(text):
        value = GUID()
        if ole.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(value)) < 0:
            raise OSError("bad GUID " + text)
        return value

    def method(obj, index, *argtypes):
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(table[index])

    def check(hr, what):
        if hr < 0:
            raise OSError(f"{what}: HRESULT 0x{hr & 0xFFFFFFFF:08X}")

    hr = ole.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED; wx has usually done this already
    uninit = hr in (0, 1)
    dialog = ctypes.c_void_p()
    item = ctypes.c_void_p()
    folder = ctypes.c_void_p()
    try:
        check(ole.CoCreateInstance(ctypes.byref(guid("{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}")), None, 1,
                                   ctypes.byref(guid("{D57C7288-D4AD-4768-BE02-9D969532D960}")), ctypes.byref(dialog)),
              "CoCreateInstance")
        options = ctypes.c_uint()
        check(method(dialog, 10, ctypes.POINTER(ctypes.c_uint))(dialog, ctypes.byref(options)), "GetOptions")
        # FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR
        check(method(dialog, 9, ctypes.c_uint)(dialog, options.value | 0x20 | 0x40 | 0x800 | 0x8), "SetOptions")
        method(dialog, 17, ctypes.c_wchar_p)(dialog, _t("picker.title"))
        method(dialog, 18, ctypes.c_wchar_p)(dialog, _t("picker.ok"))
        if initial and os.path.isdir(initial):
            if shell.SHCreateItemFromParsingName(ctypes.c_wchar_p(initial), None,
                                                 ctypes.byref(guid("{43826D1E-E718-42EE-BC55-A1E261C37BFE}")),
                                                 ctypes.byref(folder)) >= 0:
                method(dialog, 12, ctypes.c_void_p)(dialog, folder)
        hr = method(dialog, 3, wintypes.HWND)(dialog, _owner_window())
        if (hr & 0xFFFFFFFF) == 0x800704C7:  # HRESULT_FROM_WIN32(ERROR_CANCELLED)
            return None
        check(hr, "Show")
        check(method(dialog, 20, ctypes.POINTER(ctypes.c_void_p))(dialog, ctypes.byref(item)), "GetResult")
        name = ctypes.c_void_p()
        check(method(item, 5, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))(item, 0x80058000, ctypes.byref(name)),
              "GetDisplayName")  # SIGDN_FILESYSPATH
        try:
            return ctypes.wstring_at(name.value)
        finally:
            ole.CoTaskMemFree(name)
    finally:
        for obj in (folder, item, dialog):
            if obj.value:
                method(obj, 2)(obj)
        if uninit:
            ole.CoUninitialize()


def _pick_folder_legacy():
    """Fallback for systems where the modern picker is unavailable."""
    import ctypes
    from ctypes import wintypes

    class BrowseInfo(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", ctypes.c_ssize_t),
            ("iImage", ctypes.c_int),
        ]

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    shell.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None

    display = ctypes.create_unicode_buffer(32768)
    info = BrowseInfo(hwndOwner=_owner_window(), pidlRoot=None, pszDisplayName=display,
                      lpszTitle=_t("picker.title"),
                      ulFlags=0x0001 | 0x0040,  # file-system folders only, resizable new-style dialog
                      lpfn=None, lParam=0, iImage=0)
    pidl = shell.SHBrowseForFolderW(ctypes.byref(info))
    if not pidl:
        return None
    try:
        selected = ctypes.create_unicode_buffer(32768)
        if not shell.SHGetPathFromIDListW(pidl, selected):
            raise RuntimeError(_t("err.not_filesystem"))
        return selected.value
    finally:
        ole.CoTaskMemFree(pidl)


def _pick_folder_windows(initial=None):
    if os.name != "nt":
        raise RuntimeError(_t("err.picker_windows"))
    try:
        return _pick_folder_modern(initial)
    except OSError:
        return _pick_folder_legacy()


def _escape_strings_cstyle(values):
    """Python copy of Slic3r::escape_strings_cstyle(), the format of single-instance messages."""
    out = []
    for value in values:
        if any(c in value for c in " \t;\\\"\r\n"):
            value = '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n") + '"'
        out.append(value)
    return ";".join(out)


def _send_to_own_window(project):
    """Ask the Orca main window of this process to load `project` (Windows).

    Orca's `--single-instance` command-line flag is rejected in 2.5 (its option definition
    is commented out, so a second orca-slicer.exe exits with "Invalid option"). The plugin
    runs inside the Orca process, so it sends the same WM_COPYDATA message that a second
    instance would send (InstanceCheck.cpp) straight to its own main window. Orca then
    loads the file through Plater::load_files(), like a double-clicked 3MF.
    """
    import ctypes
    from ctypes import wintypes

    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]

    user = ctypes.WinDLL("user32")
    kernel = ctypes.WinDLL("kernel32")
    user.GetPropW.restype = ctypes.c_void_p
    user.GetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user.SendMessageW.restype = ctypes.c_ssize_t
    user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p]
    pid = kernel.GetCurrentProcessId()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, name, 256)
        # The main frame carries the instance-hash properties set by init_windows_properties().
        if name.value == "wxWindowNR" and (user.GetPropW(hwnd, "Instance_Hash_Minor") or user.GetPropW(hwnd, "Instance_Hash_Major")):
            found.append(hwnd)
            return False
        return True

    user.EnumWindows(visit, 0)
    if not found:
        raise OSError(_t("err.no_main_window"))
    # The receiver skips the first value: it is the path of the sending executable.
    text = _escape_strings_cstyle([_orca_executable(), os.path.abspath(project)])
    buffer = ctypes.create_unicode_buffer(text)
    data = COPYDATASTRUCT(1, ctypes.sizeof(buffer), ctypes.cast(buffer, ctypes.c_void_p))
    user.SendMessageW(found[0], 0x004A, 0, ctypes.byref(data))  # WM_COPYDATA


def _open_project(project):
    """Open a 3MF as a project in this Orca window."""
    plater = orca.host.plater()
    # Proposed upstream as Plater.open_project(); use it once Orca has it.
    if hasattr(plater, "open_project"):
        plater.open_project(project)
    elif os.name == "nt":
        _send_to_own_window(project)
    else:
        raise OSError(_t("err.open_unsupported"))


def _orca_executable():
    """Path of the running OrcaSlicer executable (the embedded Python's sys.executable may differ)."""
    candidates = []
    try:
        import ctypes
        if os.name == "nt":
            buffer = ctypes.create_unicode_buffer(32768)
            if ctypes.WinDLL("kernel32").GetModuleFileNameW(None, buffer, len(buffer)):
                candidates.append(buffer.value)
        elif sys.platform == "darwin":
            size = ctypes.c_uint32(4096)
            buffer = ctypes.create_string_buffer(4096)
            if ctypes.CDLL(None)._NSGetExecutablePath(buffer, ctypes.byref(size)) == 0:
                candidates.append(os.path.realpath(buffer.value.decode("utf-8")))
        elif os.path.exists("/proc/self/exe"):
            candidates.append(os.path.realpath("/proc/self/exe"))
    except (OSError, AttributeError):
        pass
    candidates.append(sys.executable)
    for path in candidates:
        if path and os.path.isfile(path) and "orca" in os.path.basename(path).lower():
            return os.path.abspath(path)
    raise OSError(_t("err.no_executable"))


def _parse_area_points(value):
    points = []

    def add_point(item):
        if isinstance(item, str):
            nums = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", item)
            if len(nums) >= 2:
                points.append([float(nums[0]), float(nums[1])])
            return
        if hasattr(item, "x") and hasattr(item, "y"):
            points.append([float(item.x), float(item.y)])
            return
        try:
            if len(item) >= 2:
                points.append([float(item[0]), float(item[1])])
        except (TypeError, ValueError, IndexError):
            pass

    if value is None:
        return points
    if isinstance(value, str):
        # Some Orca profile bindings serialize the whole polygon as one string,
        # e.g. "0x0;245x0;245x245;0x245", rather than a list of point strings.
        nums = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value)
        if len(nums) >= 6 and len(nums) % 2 == 0:
            return [[float(nums[i]), float(nums[i + 1])] for i in range(0, len(nums), 2)]
        add_point(value)
        return points
    try:
        for item in value:
            add_point(item)
    except TypeError:
        add_point(value)
        if not points:
            return _parse_area_points(str(value))
    return points


def _get_profile():
    try:
        bundle = orca.host.preset_bundle()
        preset = bundle.current_printer_preset()
        if preset is None:
            return None
        # Prefer the flattened current configuration because bed fields can be inherited.
        area = bundle.full_config_value("printable_area")
        height = bundle.full_config_value("printable_height")
        excluded = bundle.full_config_value("bed_exclude_area")
        if area is None:
            area = preset.config_value("printable_area")
        if height is None:
            height = preset.config_value("printable_height")
        points = _parse_area_points(area)
        if len(points) < 3:
            points = _parse_area_points(preset.config_value("printable_area"))
        if points:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            bed = {"x": max(xs) - min(xs), "y": max(ys) - min(ys),
                   "origin_x": min(xs), "origin_y": min(ys)}
        else:
            bed = None
        filaments = bundle.current_filament_preset_names()
        return {
            "name": str(preset.name),
            "size": bed,
            "height": float(height) if height is not None else None,
            "exclusions": len(_parse_area_points(excluded)) >= 3,
            "filaments": [str(x) for x in filaments if x],
        }
    except Exception as exc:
        return {"error": str(exc)}


def _app_version():
    try:
        version = str(orca.host.app_info().get("version", ""))
    except Exception:
        version = ""
    # Orca reads the generator version from "OrcaSlicer-<semver>" and treats files older
    # than 1.5.9 as legacy projects, so always write a parseable modern version.
    match = re.match(r"\d+\.\d+\.\d+", version)
    return match.group(0) if match else "2.5.0"


def _binary_stl_info(raw, include_preview=False, max_facets=None):
    if len(raw) < 84:
        raise ValueError(_t("err.stl_short"))
    count = struct.unpack_from("<I", raw, 80)[0]
    if count > 2_000_000 or 84 + count * 50 != len(raw):
        return _ascii_stl_info(raw, include_preview, max_facets)
    if not count:
        raise ValueError(_t("err.stl_vertices"))
    lo, hi = _binary_bounds(raw, count)
    if lo is None:
        raise ValueError(_t("err.stl_coords"))
    dims = [round(hi[i] - lo[i], 3) for i in range(3)]
    limit = max_facets or MAX_PREVIEW_FACETS
    if include_preview and count <= limit:
        # Every facet: its nine corner floats are bytes 12 to 48 of each 50-byte record.
        view = memoryview(raw)
        vertices = base64.b64encode(b"".join(view[84 + i * 50 + 12:84 + i * 50 + 48] for i in range(count))).decode("ascii")
    else:
        vertices = _preview_vertices(lambda: _iter_stl_triangles(raw), count, lo, hi, limit) if include_preview else None
    return {"facets": count, "dims": dims, "vertices": vertices}


def _ascii_stl_info(raw, include_preview=False, max_facets=None):
    text = raw.decode("utf-8", "replace")
    verts = []
    for match in re.finditer(r"\bvertex\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)", text, re.I):
        xyz = [float(v) for v in match.groups()]
        if all(math.isfinite(v) for v in xyz):
            verts.extend(xyz)
    if not verts:
        raise ValueError(_t("err.stl_vertices"))
    facets = len(re.findall(r"\bfacet\s+normal\b", text, re.I))
    lo = [min(verts[axis::3]) for axis in range(3)]
    hi = [max(verts[axis::3]) for axis in range(3)]
    triangles = [tuple(verts[i:i + 9]) for i in range(0, len(verts) - 8, 9)]
    vertices = _preview_vertices(lambda: iter(triangles), len(triangles), lo, hi, max_facets) if include_preview and triangles else None
    return {"facets": facets or len(verts) // 9, "dims": [round(hi[i] - lo[i], 3) for i in range(3)], "vertices": vertices}


def _binary_bounds(raw, count):
    """Lowest and highest corner coordinate per axis of a binary STL whose size was checked,
    or (None, None) for a NaN or infinity.

    The corners are read in place as strided float32 views, so a part of two million facets
    needs no copy of its 100 MB. A facet record is a normal, three corners and a 16-bit
    attribute, 50 bytes: even records start on a 4-byte boundary after the 84-byte header and
    odd ones 2 bytes past one, so a float view from byte 84 holds the even records and one
    from byte 86 the odd ones, 25 floats per pair of records. STL and every platform Orca runs
    on are little-endian.
    """
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    view = memoryview(raw)
    for start, first, records in ((84, 3, (count + 1) // 2), (86, 15, count // 2)):
        floats = view[start:start + (len(raw) - start) // 4 * 4].cast("f")
        for j in range(9):
            values = floats[first + j::25][:records]
            if not values:
                continue
            # min() and max() step over NaN silently; a sum is not finite if any value is not.
            if not math.isfinite(sum(values)):
                return None, None
            axis = j % 3
            lo[axis] = min(lo[axis], min(values))
            hi[axis] = max(hi[axis], max(values))
    return lo, hi


def _preview_vertices(triangles, count, lo, hi, limit=None):
    """Base64 float32 corners for the page's preview, at most `limit` triangles
    (MAX_PREVIEW_FACETS by default).

    A larger part is simplified as a whole by vertex clustering: corners are snapped to a
    cubic grid, the corners in each cell merge into their average, and triangles that collapse
    or repeat are dropped. Taking the first facets of the file instead showed scattered pieces,
    because STL exporters write facets in no spatial order. Only the preview is simplified;
    projects always get every facet of the part. `triangles` returns a new iterator over the
    count triangles for each pass, so a large part is never held as a list of facets.
    """
    limit = limit or MAX_PREVIEW_FACETS
    if count <= limit:
        flat = [value for t in triangles() for value in t]
    else:
        span = max(hi[axis] - lo[axis] for axis in range(3)) or 1.0
        # About 0.65·√N cells along the longest side keep most parts under N in one pass;
        # flat or thin parts come out much smaller, so they get a finer grid once.
        cells = int(math.sqrt(limit) * 0.65)
        flat = _cluster_triangles(triangles(), lo, span / cells)
        if len(flat) // 9 < limit // 3:
            finer = _cluster_triangles(triangles(), lo, span / (cells * 1.6))
            if len(finer) // 9 <= limit:
                flat = finer
        while len(flat) // 9 > limit and cells > 4:
            cells = max(4, int(cells * math.sqrt(limit * 9 / len(flat)) * 0.95))
            flat = _cluster_triangles(triangles(), lo, span / cells)
        flat = flat[:limit * 9]
    return base64.b64encode(struct.pack("<%sf" % len(flat), *flat)).decode("ascii")


def _cluster_triangles(triangles, lo, step):
    """Flat corner list of the triangles after merging the corners in each grid cell."""
    inverse = 1.0 / step
    lx, ly, lz = lo
    cells = {}
    sums = []
    kept = []
    seen = set()
    for t in triangles:
        ids = []
        for j in (0, 3, 6):
            x, y, z = t[j], t[j + 1], t[j + 2]
            key = (int((x - lx) * inverse), int((y - ly) * inverse), int((z - lz) * inverse))
            slot = cells.get(key)
            if slot is None:
                slot = cells[key] = len(sums)
                sums.append([x, y, z, 1])
            else:
                total = sums[slot]
                total[0] += x
                total[1] += y
                total[2] += z
                total[3] += 1
            ids.append(slot)
        a, b, c = ids
        if a == b or b == c or a == c:
            continue
        # The same triangle in any rotation of its corners, keeping the winding (and so its side).
        key = (a, b, c) if a < b and a < c else (b, c, a) if b < c else (c, a, b)
        if key not in seen:
            seen.add(key)
            kept.append(key)
    points = [(x / n, y / n, z / n) for x, y, z, n in sums]
    return [value for triangle in kept for corner in triangle for value in points[corner]]


def _safe_archive_path(path, root):
    try:
        full = os.path.realpath(path)
        base = os.path.realpath(root)
        return os.path.commonpath([full, base]) == base
    except (OSError, ValueError):
        return False


def _freshness(stat):
    """When a file got here, in seconds: the later of its modification and creation times. A copy
    pasted in Explorer keeps the original's modification time, but Windows gives it a new
    creation time (st_ctime there; on Linux and macOS st_ctime is the last metadata change)."""
    return max(stat.st_mtime, stat.st_ctime)


def _fingerprint(path):
    stat = os.stat(path)
    return f"{stat.st_size}:{getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1e9))}"


_NON_LATIN_OK = []   # whether this Orca's permission check copes with non-Latin path names


def _version_handles_non_latin(version):
    """True for OrcaSlicer 2.5.0 (the release) and later: its permission check keeps non-Latin
    path names intact (OrcaSlicer#16925, in the release). A 2.5.0-dev or -beta build may be
    older than that fix, so it counts as not fixed."""
    match = re.match(r"\s*(\d+)\.(\d+)\.(\d+)(.*)", str(version or ""))
    if not match:
        return False
    numbers = tuple(int(n) for n in match.groups()[:3])
    return numbers > (2, 5, 0) or (numbers == (2, 5, 0) and not match.group(4).strip())


def _orca_handles_non_latin():
    """Asked once: app_info() answers on the UI thread, where the page's messages arrive."""
    if not _NON_LATIN_OK:
        try:
            version = orca.host.app_info().get("version", "")
        except Exception:
            return False
        _NON_LATIN_OK.append(_version_handles_non_latin(version))
    return _NON_LATIN_OK[0]


def _orca_can_read(path):
    """False for a path that Orca's plugin permission check cannot handle.

    Before OrcaSlicer 2.5.0 that check lowercased every path component byte by byte in the
    Windows code page, which turned UTF-8 letters outside Latin (Cyrillic, CJK, ...) into
    invalid UTF-8; converting the result back to a path threw inside Orca's audit hook, so the
    plugin call never returned and Orca could crash. Those builds get no such paths: they are
    never listed or opened, and their files can be dropped into PlatePilot instead. Components
    named like a secret, certificate or config are refused by every build on purpose.
    """
    if any(ord(c) > 127 for c in str(path)) and not _orca_handles_non_latin():
        return False
    parts = [p.lower() for p in re.split(r"[\\/]+", str(path)) if p]
    return not any(p in DENIED_PATH_NAMES or p.endswith((".conf", ".ini")) for p in parts)


# What an archive probably holds, from its name alone: reading it would make Orca ask about
# every archive. Model sites name their downloads recognizably (Printables "..._stls.zip" and
# "+" for spaces, Thingiverse "Name - 1234567.zip", "model_files.zip"); programs, drivers and
# source code carry platforms, versions and branch names.
ARCHIVE_MODEL_NAME = re.compile(r"stls?\b|_stl|model_files|\s-\s\d{4,8}\.zip$|\b3mf\b|\bstep\b", re.I)
ARCHIVE_OTHER_NAME = re.compile(r"driver|win(?:32|64|dows)|\bx(?:64|86)\b|amd64|arm64|setup|install|portable|nightly|\bsdk\b"
                                r"|-(?:master|main)\.zip$|\d+\.\d+\.\d+|firmware|bluetooth|wireless|adapter|desktop|linux|\bmac\b", re.I)
ARCHIVE_MAYBE_MODEL_NAME = re.compile(r"\+|_files\.zip$", re.I)


def _guess_archive(name):
    """"models", "other" or "unknown" for an archive that was never opened."""
    if ARCHIVE_MODEL_NAME.search(name):
        return "models"
    if ARCHIVE_OTHER_NAME.search(name):
        return "other"
    return "models" if ARCHIVE_MAYBE_MODEL_NAME.search(name) else "unknown"


def _likely_models(row):
    """A source worth listing up front: a model folder, an archive known to hold models, or a
    never-opened archive whose name looks like a model download."""
    if row["kind"] != "zip":
        return True
    counts = row.get("type_counts")
    if counts is not None:
        return sum(counts.values()) > 0
    return _guess_archive(row["name"]) == "models"


def _scan_folder(folder, recursive=True):
    """Rows for the ZIP archives and model folders under folder, found by their names only.

    Nothing is opened here. Orca asks before a plugin reads a file, so a scan that opened every
    ZIP asked about archives that turned out to hold no models. Listing the tree takes a single
    os.walk(), which Orca grants once per watched folder and which covers every subfolder while
    it runs, so the whole walk has to happen within one call. Subfolders whose path Orca cannot
    check (see _orca_can_read) are not entered. Returns (rows, truncated, skipped folders).
    """
    rows = []
    directories = 0
    skipped = 0
    for current, dirs, files in os.walk(folder, followlinks=False):
        directories += 1
        if directories > MAX_SCAN_DIRECTORIES:
            return rows, True, skipped
        if recursive:
            visible = [d for d in dirs if not d.startswith(".")]
            readable = [d for d in visible if _orca_can_read(os.path.join(current, d))]
            skipped += len(visible) - len(readable)
            dirs[:] = sorted(readable, key=str.casefold)
        else:
            dirs[:] = []
        for name in sorted([n for n in files if n.lower().endswith(".zip")], key=str.casefold):
            path = os.path.join(current, name)
            try:
                stat = os.stat(path)
                row = {"kind": "zip", "name": name, "path": path, "root": folder,
                       "fingerprint": _fingerprint(path), "size": _fmt_size(stat.st_size),
                       "count": None, "type_counts": None, "time": _freshness(stat)}
            except OSError:
                continue
            if not _orca_can_read(path):
                row["warning"] = _t("warn.unsafe_name")
            rows.append(row)
            if len(rows) >= MAX_ARCHIVES:
                return rows, True, skipped
        model_files = sorted([n for n in files if _model_type_for_name(n)], key=str.casefold)
        if model_files and _safe_archive_path(current, folder):
            try:
                stats = [os.stat(os.path.join(current, name)) for name in model_files]
            except OSError:
                continue
            total = sum(stat.st_size for stat in stats)
            newest = max(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)) for stat in stats)
            rows.append({"kind": "folder", "name": os.path.relpath(current, folder) if current != folder else os.path.basename(folder),
                         "path": os.path.realpath(current), "root": folder,
                         "fingerprint": f"{len(model_files)}:{total}:{newest}", "size": _fmt_size(total),
                         "count": len(model_files), "type_counts": _type_counts(model_files), "files": model_files,
                         "time": max(_freshness(stat) for stat in stats)})
            if len(rows) >= MAX_ARCHIVES:
                return rows, True, skipped
    return rows, False, skipped


def _update_library(state, folder, rows, truncated, first_scan, now=None):
    """Mark the new rows of one folder in the saved library state and return the folder's summary.

    state keeps "known" (the fingerprint of every source the user has already seen) and "zips"
    (the model counts of archives that were opened once). On a folder's first scan everything
    is taken as seen: a freshly added folder has nothing "new" in it yet.
    """
    known = state.setdefault("known", {})
    zips = state.setdefault("zips", {})
    summary = {"checked": now if now is not None else time.time(), "sources": 0, "new": 0, "empty": 0, "other": 0,
               "truncated": MAX_ARCHIVES if truncated else 0}
    for row in rows:
        if row["kind"] == "zip":
            cached = zips.get(row["path"])
            if cached and cached.get("fingerprint") == row["fingerprint"]:
                row["type_counts"] = dict(cached.get("counts", {}))
                row["count"] = sum(row["type_counts"].values())
        if first_scan:
            known[row["path"]] = row["fingerprint"]
        row["new"] = known.get(row["path"]) != row["fingerprint"]
        row["likely"] = _likely_models(row)
        if row.get("count") == 0:
            summary["empty"] += 1
            continue
        if not row["likely"]:
            # Listed apart and not counted as new: a downloaded installer is no news.
            row["new"] = False
            summary["other"] += 1
            continue
        summary["sources"] += 1
        summary["new"] += 1 if row["new"] else 0
    if not truncated:
        # Forget sources that are gone from this folder, so the state does not grow forever.
        present = {row["path"] for row in rows}
        for table in (known, zips):
            for path in [p for p in table if p not in present and _is_inside(p, folder)]:
                del table[path]
    return summary


def _safe_name(name, fallback="file"):
    """A file or folder name from a dropped file that Windows and Orca's permission check accept.
    Cyrillic is transliterated and other non-Latin letters replaced (see _orca_can_read)."""
    name = "".join(CYRILLIC_LATIN.get(c.lower(), c).capitalize() if c.isupper() and c.lower() in CYRILLIC_LATIN
                   else CYRILLIC_LATIN.get(c, c) for c in str(name or ""))
    name = re.sub(r"[^\x20-\x7e]", "_", name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip().rstrip(". ")
    if not name or name in (".", ".."):
        name = fallback
    stem, ext = os.path.splitext(name)
    if stem.lower() in WINDOWS_RESERVED_NAMES or name.lower() in DENIED_PATH_NAMES or stem.lower() in DENIED_PATH_NAMES:
        stem += "_"
    if len(stem) > 80:
        stem = stem[:80].rstrip(". ")
    return stem + ext.lower() if ext.lower() in IMPORT_EXTENSIONS else stem + ext


def _unique_path(folder, name):
    """folder/name, or folder/name (2), (3)... when that name is taken."""
    stem, ext = os.path.splitext(name)
    candidate = os.path.join(folder, name)
    number = 2
    while os.path.exists(candidate) or os.path.exists(candidate + ".part"):
        candidate = os.path.join(folder, f"{stem} ({number}){ext}")
        number += 1
    return candidate


def _snapshot_folder(source, names, cache_root):
    """Copy the STL files of a watched model folder into the plugin's own folder and return the copy.

    Reading the files one by one makes Orca ask about every file. shutil.copytree() is a single
    operation that Orca asks about once per folder, and the copy inside the plugin's data folder
    can then be read freely. The copy is kept while its files match the folder's.
    """
    target = os.path.join(cache_root, hashlib.sha1(_folder_key(source).encode("utf-8")).hexdigest()[:12])
    manifest_path = os.path.join(target, "manifest.json")
    wanted = {}
    for name in names:
        stat = os.stat(os.path.join(source, name))
        wanted[name] = [stat.st_size, getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))]
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            current = json.load(stream)
    except (OSError, ValueError):
        current = None
    if current != wanted or not all(os.path.isfile(os.path.join(target, name)) for name in wanted):
        if sum(size for size, _ in wanted.values()) > MAX_TOTAL_STL_BYTES:
            raise ValueError(_t("err.total_folder", mb=MAX_TOTAL_STL_BYTES // (1024 * 1024)))
        shutil.rmtree(target, ignore_errors=True)
        if wanted:
            shutil.copytree(source, target, ignore=lambda _folder, entries: [e for e in entries if e not in wanted],
                            copy_function=shutil.copyfile)
        else:
            os.makedirs(target)
        with open(manifest_path, "w", encoding="utf-8") as stream:
            json.dump(wanted, stream)
    os.utime(manifest_path)
    copies = sorted((os.path.join(cache_root, d) for d in os.listdir(cache_root)),
                    key=lambda d: os.path.getmtime(os.path.join(d, "manifest.json")) if os.path.isfile(os.path.join(d, "manifest.json")) else 0,
                    reverse=True)
    for old in copies[FOLDER_CACHE_KEEP:]:
        shutil.rmtree(old, ignore_errors=True)
    return target


def _read_archive(path, root):
    if not _safe_archive_path(path, root):
        raise ValueError(_t("err.archive_outside"))
    parts = []
    total = 0
    with zipfile.ZipFile(path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir() or not _model_type_for_name(info.filename):
                continue
            model_type = _model_type_for_name(info.filename)
            if model_type == "step":
                parts.append({"name": os.path.basename(info.filename), "member": info.filename, "filetype": "step",
                              "dims": [], "facets": 0, "included": False, "open_in_orca": True, "warning": _t("warn.step_open")})
                continue
            if model_type == "3mf":
                if info.file_size > MAX_STL_BYTES:
                    parts.append({"name": os.path.basename(info.filename), "filetype": "3mf", "blocked": True, "dims": [0, 0, 0], "facets": 0, "included": False, "warning": _t("warn.too_big", mb=MAX_STL_BYTES // (1024 * 1024))})
                    continue
                total += info.file_size
                if total > MAX_TOTAL_STL_BYTES:
                    raise ValueError(_t("err.total_archive", mb=MAX_TOTAL_STL_BYTES // (1024 * 1024)))
                parts.extend(_threemf_rows_or_warning(os.path.basename(info.filename), info.filename,
                                                      ("zip", path, info.filename, info.file_size, info.CRC), lambda info=info: zf.read(info)))
                continue
            if info.file_size > MAX_STL_BYTES:
                parts.append({"name": os.path.basename(info.filename), "filetype": "stl", "blocked": True, "dims": [0, 0, 0], "facets": 0, "included": False, "warning": _t("warn.too_big", mb=MAX_STL_BYTES // (1024 * 1024))})
                continue
            total += info.file_size
            if total > MAX_TOTAL_STL_BYTES:
                raise ValueError(_t("err.total_archive", mb=MAX_TOTAL_STL_BYTES // (1024 * 1024)))
            raw = zf.read(info)
            try:
                stats = _binary_stl_info(raw)
                dims = stats["dims"]
                warning = "" if all(v > 0 for v in dims) else _t("warn.empty_model")
                parts.append({"name": os.path.basename(info.filename), "member": info.filename, "filetype": "stl", "dims": dims, "facets": stats["facets"], "included": True, "warning": warning})
            except Exception as exc:
                parts.append({"name": os.path.basename(info.filename), "member": info.filename, "filetype": "stl", "blocked": True, "dims": [0, 0, 0], "facets": 0, "included": False, "warning": _t("warn.stl_error", error=str(exc)[:100])})
    return parts


def _threemf_rows_or_warning(name, member, key, read):
    try:
        items = _threemf_cached(key, read)
    except Exception as exc:
        return [{"name": name, "member": member, "filetype": "3mf", "blocked": True, "dims": [0, 0, 0], "facets": 0,
                 "included": False, "warning": _t("warn.3mf_error", error=str(exc)[:100])}]
    if not items:
        return [{"name": name, "member": member, "filetype": "3mf", "blocked": True, "dims": [0, 0, 0], "facets": 0,
                 "included": False, "warning": _t("warn.3mf_empty")}]
    return _threemf_part_rows(name, member, items)


def _read_loose_folder(path, root, catalog=()):
    """Parts of a model folder. catalog holds (name, warning) of files that stay in the original
    folder (STEP, 3MF, oversized STL and names Orca cannot check, which a copy made by
    _snapshot_folder() leaves out); they are listed from their names without being read."""
    if not _safe_archive_path(path, root):
        raise ValueError(_t("err.folder_outside"))
    parts = []
    total = 0
    catalog = dict(catalog)
    names = set(n for n in os.listdir(path) if _model_type_for_name(n)) | set(catalog)
    for name in sorted(names, key=str.casefold):
        model_type = _model_type_for_name(name)
        if model_type == "step":
            # Not read here: the button hands the file to Orca, which opens STEP itself.
            parts.append({"name": name, "member": os.path.join(path, name), "filetype": "step", "dims": [], "facets": 0,
                          "included": False, "open_in_orca": not catalog.get(name), "warning": catalog.get(name) or _t("warn.step_open")})
            continue
        if name in catalog and (model_type in ("stl", "3mf") or catalog[name]):
            parts.append({"name": name, "filetype": model_type, "blocked": True, "dims": [0, 0, 0], "facets": 0, "included": False,
                          "warning": catalog[name] or _t("warn.too_big", mb=MAX_STL_BYTES // (1024 * 1024))})
            continue
        member = os.path.join(path, name)
        if model_type == "3mf":
            size = os.path.getsize(member)
            if size > MAX_STL_BYTES:
                parts.append({"name": name, "filetype": "3mf", "blocked": True, "dims": [0, 0, 0], "facets": 0, "included": False, "warning": _t("warn.too_big", mb=MAX_STL_BYTES // (1024 * 1024))})
                continue
            total += size
            if total > MAX_TOTAL_STL_BYTES:
                raise ValueError(_t("err.total_folder", mb=MAX_TOTAL_STL_BYTES // (1024 * 1024)))

            def read(member=member):
                with open(member, "rb") as stream:
                    return stream.read()
            parts.extend(_threemf_rows_or_warning(name, member, ("file", member, size, os.path.getmtime(member)), read))
            continue
        size = os.path.getsize(member)
        if size > MAX_STL_BYTES:
            parts.append({"name": name, "member": member, "filetype": "stl", "blocked": True, "dims": [0, 0, 0], "facets": 0, "included": False, "warning": _t("warn.too_big", mb=MAX_STL_BYTES // (1024 * 1024))})
            continue
        total += size
        if total > MAX_TOTAL_STL_BYTES:
            raise ValueError(_t("err.total_folder", mb=MAX_TOTAL_STL_BYTES // (1024 * 1024)))
        with open(member, "rb") as stream:
            raw = stream.read()
        try:
            stats = _binary_stl_info(raw)
            parts.append({"name": name, "member": member, "filetype": "stl", "dims": stats["dims"], "facets": stats["facets"], "included": True, "warning": ""})
        except Exception as exc:
            parts.append({"name": name, "member": member, "filetype": "stl", "blocked": True, "dims": [0, 0, 0], "facets": 0, "included": False, "warning": _t("warn.stl_error", error=str(exc)[:100])})
    return parts


THREEMF_CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
THREEMF_PRODUCTION = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"
THREEMF_UNITS = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
STL_RECORD = struct.Struct("<12fH")


def _threemf_matrix(text):
    """A 3MF transform ("m00 m01 m02 m10 ... m32", applied to row vectors) as 12 floats."""
    if not text:
        return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
    values = tuple(float(v) for v in text.split())
    if len(values) != 12:
        raise ValueError("bad transform")
    return values


def _threemf_then(a, b):
    """The transform that applies a, then b."""
    out = []
    for r in range(4):
        row = a[r * 3:r * 3 + 3]
        for c in range(3):
            value = row[0] * b[c] + row[1] * b[3 + c] + row[2] * b[6 + c]
            out.append(value + (b[9 + c] if r == 3 else 0.0))
    return tuple(out)


def _threemf_parts(raw):
    """[(object name, binary STL bytes)] for each build item of a 3MF, in millimetres, placed
    as the 3MF places it. Objects made of components, also from the other model files of a
    Bambu or Orca project (the production extension's path), are followed through."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        root = "3D/3dmodel.model"
        try:
            rels = ElementTree.fromstring(archive.read("_rels/.rels"))
            for rel in rels:
                if rel.get("Type", "").endswith("/3dmodel"):
                    root = rel.get("Target", root).lstrip("/")
        except KeyError:
            pass
        models = {}

        def model(path):
            path = path.lstrip("/")
            if path not in models:
                tree = ElementTree.fromstring(archive.read(path))
                objects = {}
                resources = tree.find(THREEMF_CORE + "resources")
                for obj in (resources if resources is not None else []):
                    if obj.tag != THREEMF_CORE + "object":
                        continue
                    mesh = obj.find(THREEMF_CORE + "mesh")
                    if mesh is not None:
                        vertices = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
                                    for v in mesh.find(THREEMF_CORE + "vertices")]
                        triangles = [(int(f.get("v1")), int(f.get("v2")), int(f.get("v3")))
                                     for f in mesh.find(THREEMF_CORE + "triangles")]
                        objects[obj.get("id")] = (obj.get("name") or "", vertices, triangles, None)
                    else:
                        components = obj.find(THREEMF_CORE + "components")
                        parts = [(c.get("objectid"), _threemf_matrix(c.get("transform")),
                                  c.get(THREEMF_PRODUCTION + "path") or path) for c in (components if components is not None else [])]
                        objects[obj.get("id")] = (obj.get("name") or "", None, None, parts)
                models[path] = (tree, objects)
            return models[path]

        def collect(path, object_id, matrix, out, depth=0):
            if depth > 16:
                raise ValueError("components nest too deep")
            name, vertices, triangles, components = model(path)[1][object_id]
            if vertices is not None:
                m = matrix
                placed = [(x * m[0] + y * m[3] + z * m[6] + m[9], x * m[1] + y * m[4] + z * m[7] + m[10],
                           x * m[2] + y * m[5] + z * m[8] + m[11]) for x, y, z in vertices]
                for a, b, c in triangles:
                    out.append(placed[a] + placed[b] + placed[c])
                    if len(out) > 2_000_000:
                        raise ValueError(_t("warn.too_big", mb=MAX_STL_BYTES // (1024 * 1024)))
            for child, child_matrix, child_path in components or []:
                collect(child_path, child, _threemf_then(child_matrix, matrix), out, depth + 1)

        tree, objects = model(root)
        scale = THREEMF_UNITS.get(tree.get("unit", "millimeter"), 1.0)
        build = tree.find(THREEMF_CORE + "build")
        result = []
        for item in (build if build is not None else []):
            if item.tag != THREEMF_CORE + "item":
                continue
            triangles = []
            collect(root, item.get("objectid"), _threemf_matrix(item.get("transform")), triangles)
            if not triangles:
                continue
            stl = bytearray(84 + 50 * len(triangles))
            stl[0:80] = b"PlatePilot from 3MF".ljust(80)
            struct.pack_into("<I", stl, 80, len(triangles))
            offset = 84
            for tri in triangles:
                STL_RECORD.pack_into(stl, offset, 0.0, 0.0, 0.0, *(v * scale for v in tri), 0)
                offset += 50
            result.append((objects.get(item.get("objectid"), ("",))[0], bytes(stl)))
        return result


_THREEMF_CACHE = {}   # (where, size, stamp) -> _threemf_parts(): opening, previewing and sending read it once


def _threemf_cached(key, read):
    if key not in _THREEMF_CACHE:
        while len(_THREEMF_CACHE) >= 4:
            _THREEMF_CACHE.pop(next(iter(_THREEMF_CACHE)))
        _THREEMF_CACHE[key] = _threemf_parts(read())
    return _THREEMF_CACHE[key]


def _split_member(member):
    """("file.3mf", 2) for the third object of a 3MF ("file.3mf#2"), (member, None) otherwise."""
    base, _, index = str(member).rpartition("#")
    if base.lower().endswith(".3mf") and index.isdigit():
        return base, int(index)
    return member, None


def _threemf_part_rows(name, member, items):
    """Parts for the objects of one 3MF: a single object is listed under the file's name."""
    rows = []
    for i, (object_name, stl) in enumerate(items):
        stats = _binary_stl_info(stl)
        label = name if len(items) == 1 else f"{name} · {object_name or i + 1}"
        warning = "" if all(v > 0 for v in stats["dims"]) else _t("warn.empty_model")
        rows.append({"name": label, "member": member if len(items) == 1 else f"{member}#{i}", "filetype": "3mf",
                     "dims": stats["dims"], "facets": stats["facets"], "included": True, "warning": warning})
    return rows


def _footprint_hull(raw):
    """Convex hull of a part's outline seen from above, as (x, y) points (monotone chain).
    Points are rounded to 0.05 mm first, which keeps big meshes fast and changes nothing
    a plate layout can notice."""
    points = set()
    for triangle in _iter_stl_triangles(raw):
        for j in (0, 3, 6):
            points.add((round(triangle[j] * 20) / 20, round(triangle[j + 1] * 20) / 20))
    points = sorted(points)
    if len(points) < 3:
        return points

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _rotated_size(hull, degrees):
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    xs = [x * c - y * s for x, y in hull]
    ys = [x * s + y * c for x, y in hull]
    return max(xs) - min(xs), max(ys) - min(ys)


def _fitting_angle(hull, usable_x, usable_y):
    """The rotation about Z, in whole degrees, at which a part that fits neither straight nor
    at 90° fits the bed with the smallest footprint, as (angle, width, depth); None if none does."""
    best = None
    for degrees in range(1, 180):
        w, h = _rotated_size(hull, degrees)
        if (w <= usable_x + 1e-6 and h <= usable_y + 1e-6) or (h <= usable_x + 1e-6 and w <= usable_y + 1e-6):
            key = (w * h, min(degrees, 180 - degrees))
            if best is None or key < best[0]:
                best = (key, degrees, w, h)
    return None if best is None else best[1:]


def _turn_to_fit(parts, blobs, bed_x, bed_y, margin):
    """Copies of parts where every part too big for the bed straight or at 90° is turned to the
    angle at which it fits (diagonal placement); its dims become the turned footprint."""
    gap = max(0.0, margin)
    usable_x, usable_y = bed_x - 2 * gap, bed_y - 2 * gap
    out = []
    for part in parts:
        part = dict(part)
        w, h = (float(v) for v in part["dims"][:2])
        fits = (w <= usable_x and h <= usable_y) or (h <= usable_x and w <= usable_y)
        raw = blobs.get(part.get("member"))
        if not fits and raw:
            found = _fitting_angle(_footprint_hull(raw), usable_x, usable_y)
            if found:
                part["angle"], width, depth = found
                part["dims"] = [width, depth, part["dims"][2]]
        out.append(part)
    return out


def _pack(parts, bed_x, bed_y, margin):
    gap = max(0.0, margin)
    usable_x = bed_x - 2 * gap
    usable_y = bed_y - 2 * gap
    if usable_x <= 0 or usable_y <= 0:
        raise ValueError(_t("err.gap_too_big"))
    items = []
    for part in parts:
        dims = part["dims"]
        if len(dims) < 2 or min(dims[:2]) <= 0:
            continue
        w, h = float(dims[0]) + gap, float(dims[1]) + gap
        items.append({"name": part["name"], "member": part.get("member"), "dims": [float(v) for v in part.get("source_dims", dims)[:3]],
                      "w": w, "h": h, "z": float(dims[2]), "facets": int(part.get("facets", 0)), "angle": int(part.get("angle", 0))})

    # Try several deterministic item orders. Each run uses the MaxRects
    # best-short-side-fit rule, then we keep the layout with fewest plates.
    keys = (
        lambda p: p["w"] * p["h"],
        lambda p: max(p["w"], p["h"]),
        lambda p: min(p["w"], p["h"]),
        lambda p: p["w"],
        lambda p: p["h"],
        lambda p: p["w"] + p["h"],
    )

    def run(order):
        plates = []
        problems = []

        def add_plate():
            plates.append({"free": [(0.0, 0.0, usable_x + gap, usable_y + gap)], "items": []})
            return len(plates) - 1

        def split_free(free_rects, used):
            ux, uy, uw, uh = used
            out = []
            for fx, fy, fw, fh in free_rects:
                if ux >= fx + fw or ux + uw <= fx or uy >= fy + fh or uy + uh <= fy:
                    out.append((fx, fy, fw, fh))
                    continue
                if ux > fx:
                    out.append((fx, fy, ux - fx, fh))
                if ux + uw < fx + fw:
                    out.append((ux + uw, fy, fx + fw - (ux + uw), fh))
                if uy > fy:
                    out.append((fx, fy, fw, uy - fy))
                if uy + uh < fy + fh:
                    out.append((fx, uy + uh, fw, fy + fh - (uy + uh)))
            clean = []
            for i, rect in enumerate(out):
                x, y, w, h = rect
                if w <= 1e-6 or h <= 1e-6:
                    continue
                contained = False
                for j, other in enumerate(out):
                    if i == j:
                        continue
                    ox, oy, ow, oh = other
                    if x >= ox - 1e-6 and y >= oy - 1e-6 and x + w <= ox + ow + 1e-6 and y + h <= oy + oh + 1e-6:
                        # Of two identical rectangles keep only the first one.
                        if (x, y, w, h) != (ox, oy, ow, oh) or j < i:
                            contained = True
                            break
                if not contained:
                    clean.append(rect)
            return clean

        for item in order:
            best = None
            orientations = ((item["w"], item["h"], False), (item["h"], item["w"], True))
            for plate_index, plate in enumerate(plates):
                for free_index, (fx, fy, fw, fh) in enumerate(plate["free"]):
                    for w, h, rotated in orientations:
                        if w > fw + 1e-6 or h > fh + 1e-6:
                            continue
                        leftover_w, leftover_h = fw - w, fh - h
                        score = (min(leftover_w, leftover_h), max(leftover_w, leftover_h), leftover_w * leftover_h, plate_index, free_index, int(rotated))
                        if best is None or score < best[0]:
                            best = (score, plate_index, free_index, w, h, rotated, fx, fy)
            if best is None:
                fits = [o for o in orientations if o[0] - gap <= usable_x + 1e-6 and o[1] - gap <= usable_y + 1e-6]
                if not fits:
                    problems.append(item["name"])
                    continue
                add_plate()
                plate_index = len(plates) - 1
                fx = fy = 0.0
                w, h, rotated = min(fits, key=lambda o: (min(usable_x + gap - o[0], usable_y + gap - o[1]), max(usable_x + gap - o[0], usable_y + gap - o[1])))
            else:
                _, plate_index, free_index, w, h, rotated, fx, fy = best
            plate = plates[plate_index]
            plate["free"] = split_free(plate["free"], (fx, fy, w, h))
            plate["items"].append({
                "name": item["name"], "x": gap + fx, "y": gap + fy,
                "w": w - gap, "h": h - gap, "z": item["z"], "rotated": rotated, "angle": item["angle"],
                "member": item.get("member"), "source_dims": item["dims"],
            })
        nonempty = [p for p in plates if p["items"]]
        compactness = 0.0
        for plate in nonempty:
            right = max(it["x"] + it["w"] for it in plate["items"])
            bottom = max(it["y"] + it["h"] for it in plate["items"])
            compactness += right * bottom
        return {"plates": [{"items": p["items"]} for p in nonempty], "problems": problems, "compactness": compactness}

    candidates = [run(sorted(items, key=key, reverse=True)) for key in keys]
    best = min(candidates, key=lambda result: (len(result["plates"]) + len(result["problems"]), len(result["problems"]), result["compactness"]))
    return {"plates": best["plates"], "problems": best["problems"], "bed": {"x": usable_x, "y": usable_y}, "total": len(items)}


def _as_parts(items):
    """Packed items back to parts for another packing run, as they lie before any 90° turn."""
    return [{"name": it["name"], "member": it.get("member"),
             "dims": [it["h"], it["w"], it["z"]] if it["rotated"] else [it["w"], it["h"], it["z"]],
             "source_dims": it["source_dims"], "angle": it["angle"]} for it in items]


def _plate_count(base, wanted):
    """How many plates to use: the wanted number, but no fewer than base needs and no more
    than one per part. Returns (count, note) with note "at_least" or "at_most" when the
    wanted number could not be used."""
    least = len(base["plates"])
    parts = sum(len(plate["items"]) for plate in base["plates"])
    wanted = int(wanted or 0)
    if wanted <= 0:
        return least, None
    if wanted < least:
        return least, "at_least"
    if wanted > parts:
        return max(least, parts), "at_most"
    return wanted, None


def _pack_into(parts, bed_x, bed_y, margin, wanted=0):
    """_pack with a wanted number of plates. More plates than _pack needs: the parts are spread
    evenly (largest first, each onto the least filled plate it still fits on) and every plate
    is packed as _pack packs one. Adds "min_plates" and "count_note" to _pack's result."""
    base = _pack(parts, bed_x, bed_y, margin)
    count, note = _plate_count(base, wanted)
    result = dict(base, min_plates=len(base["plates"]), count_note=note)
    if base["problems"] or count <= len(base["plates"]):
        return result
    items = [it for plate in base["plates"] for it in plate["items"]]
    groups = [[] for _ in range(count)]
    for it in sorted(items, key=lambda it: -it["w"] * it["h"]):
        for i in sorted(range(count), key=lambda i: (sum(o["w"] * o["h"] for o in groups[i]), i)):
            trial = _pack(_as_parts(groups[i] + [it]), bed_x, bed_y, margin)
            if not trial["problems"] and len(trial["plates"]) == 1:
                groups[i].append(it)
                break
        else:
            return result   # cannot happen while each part fits a plate alone; keep _pack's plates
    plates = [_pack(_as_parts(group), bed_x, bed_y, margin)["plates"][0] for group in groups if group]
    return dict(result, plates=plates)


def _split_free_rects(free_rects, used):
    """MaxRects step for _pack_smart: free rectangles left after placing `used`, without the ones
    inside another (the same split as in _pack, kept apart so _pack stays as it is)."""
    ux, uy, uw, uh = used
    out = []
    for fx, fy, fw, fh in free_rects:
        if ux >= fx + fw or ux + uw <= fx or uy >= fy + fh or uy + uh <= fy:
            out.append((fx, fy, fw, fh))
            continue
        if ux > fx:
            out.append((fx, fy, ux - fx, fh))
        if ux + uw < fx + fw:
            out.append((ux + uw, fy, fx + fw - (ux + uw), fh))
        if uy > fy:
            out.append((fx, fy, fw, uy - fy))
        if uy + uh < fy + fh:
            out.append((fx, uy + uh, fw, fy + fh - (uy + uh)))
    out = [r for r in out if r[2] > 1e-6 and r[3] > 1e-6]
    clean = []
    for i, (x, y, w, h) in enumerate(out):
        inside = any(j != i and x >= ox - 1e-6 and y >= oy - 1e-6 and x + w <= ox + ow + 1e-6 and y + h <= oy + oh + 1e-6
                     and ((x, y, w, h) != (ox, oy, ow, oh) or j < i)
                     for j, (ox, oy, ow, oh) in enumerate(out))
        if not inside:
            clean.append((x, y, w, h))
    return clean


def _place_from_centre(items, bed_x, bed_y, edge, gap):
    """Place every item on one plate as close to the bed centre as the free space allows.

    Items are (w, h) footprints with at least `gap` between them and `edge` from the bed sides.
    Each one goes, in the given order and in either orientation, to the spot of a free
    rectangle nearest the centre (the free rectangles are maximal, so any spot inside one is
    free); of spots as near, the one that keeps the group most compact, so equal parts form a
    block rather than a row. Returns [(x, y, w, h, rotated)] in bed coordinates, or None if an
    item does not fit.
    """
    width, height = bed_x - 2 * edge + gap, bed_y - 2 * edge + gap
    if width <= 0 or height <= 0:
        return None
    free = [(0.0, 0.0, width, height)]
    cx, cy = bed_x / 2 - edge, bed_y / 2 - edge
    placed = []
    box = None
    for w0, h0 in items:
        best = None
        for w, h, rotated in ((w0, h0, False), (h0, w0, True)):
            iw, ih = w + gap, h + gap
            for fx, fy, fw, fh in free:
                if iw > fw + 1e-6 or ih > fh + 1e-6:
                    continue
                # The spot of this rectangle whose part centre is nearest the bed centre.
                x = min(max(cx - w / 2, fx), fx + fw - iw)
                y = min(max(cy - h / 2, fy), fy + fh - ih)
                grown = (x, y, x + iw, y + ih) if box is None else (min(box[0], x), min(box[1], y), max(box[2], x + iw), max(box[3], y + ih))
                side = max(grown[2] - grown[0], grown[3] - grown[1])
                key = (round((x + w / 2 - cx) ** 2 + (y + h / 2 - cy) ** 2, 3), round(side, 3), (fw - iw) * (fh - ih), rotated)
                if best is None or key < best[0]:
                    best = (key, x, y, w, h, rotated, grown)
        if best is None:
            return None
        _, x, y, w, h, rotated, box = best
        free = _split_free_rects(free, (x, y, w + gap, h + gap))
        placed.append((edge + x, edge + y, w, h, rotated))
    return placed


def _centred(placed, bed_x, bed_y):
    """The layout moved so that the bounding box of its parts is in the middle of the bed."""
    lo_x = min(p[0] for p in placed)
    lo_y = min(p[1] for p in placed)
    hi_x = max(p[0] + p[2] for p in placed)
    hi_y = max(p[1] + p[3] for p in placed)
    dx, dy = (bed_x - lo_x - hi_x) / 2, (bed_y - lo_y - hi_y) / 2
    return [(x + dx, y + dy, w, h, r) for x, y, w, h, r in placed]


def _edge_clearance(placed, bed_x, bed_y):
    return min(min(p[0], p[1], bed_x - p[0] - p[2], bed_y - p[1] - p[3]) for p in placed)


def _centre_priority(w, h, z):
    """How much a part needs the middle of the bed: more for a small footprint and for a part
    tall for its footprint, which holds on worst and suffers most from the cooler edges."""
    side = math.sqrt(max(w * h, 1.0))
    return (1.0 + max(z, 0.0) / side) / side


def _centre_score(placed, weights, bed_x, bed_y):
    """Mean distance of the part centres from the bed centre, weighted by _centre_priority."""
    total = sum(weight * math.hypot(x + w / 2 - bed_x / 2, y + h / 2 - bed_y / 2) for (x, y, w, h, _), weight in zip(placed, weights))
    return total / sum(weights)


def _smart_plate(items, bed_x, bed_y, margin):
    """The best centred layout of one plate's items, or None if it cannot be found.

    Several item orders are tried (the parts that need the middle most first, small or large
    first, alternating); the gap between parts is then widened step by step while the parts
    stay at least as far from the bed sides as with the plain gap, up to 10 mm. Layouts are
    compared by that distance from the sides, then by how close the parts are to the centre,
    weighted by _centre_priority (small and tall parts count most).
    """
    sizes = [(it["w"], it["h"]) for it in items]
    weights = [_centre_priority(w, h, it["z"]) for (w, h), it in zip(sizes, items)]
    by_area = sorted(range(len(items)), key=lambda i: sizes[i][0] * sizes[i][1])
    alternating = []
    low, high = 0, len(by_area) - 1
    while low <= high:
        alternating.append(by_area[high])
        if low != high:
            alternating.append(by_area[low])
        low, high = low + 1, high - 1
    orders = [sorted(range(len(items)), key=lambda i: -weights[i]), list(reversed(by_area)), by_area, alternating,
              sorted(range(len(items)), key=lambda i: -max(sizes[i]))]

    def layout(order, gap):
        placed = _place_from_centre([sizes[i] for i in order], bed_x, bed_y, margin, gap)
        if placed is None:
            return None
        placed = _centred(placed, bed_x, bed_y)
        by_item = [None] * len(items)
        for index, spot in zip(order, placed):
            by_item[index] = spot
        return by_item

    def score(placed):
        return (-round(min(_edge_clearance(placed, bed_x, bed_y), 10.0), 1), _centre_score(placed, weights, bed_x, bed_y))

    best = None
    for order in orders:
        placed = layout(order, margin)
        if placed is not None and (best is None or score(placed) < score(best)):
            best = placed
    if best is None:
        return None
    wanted = min(_edge_clearance(best, bed_x, bed_y), 10.0) - 0.05
    for extra in range(1, int(min(margin, 6.0)) + 1):
        wider = None
        for order in orders:
            placed = layout(order, margin + extra)
            if placed is not None and _edge_clearance(placed, bed_x, bed_y) >= wanted and (wider is None or score(placed) < score(wider)):
                wider = placed
        if wider is None:
            break
        best = wider
    return best


def _pack_smart(parts, bed_x, bed_y, margin, wanted=0):
    """Smart layout (beta): as many plates as _pack needs, with the parts of each plate in a
    centred group, small parts nearer the middle, away from the bed sides, evenly spaced, and
    the plates filled about evenly. A plate too full for that gets _pack's dense layout, moved to
    the middle. The result has the same form as _pack's, plus "smart": True, or "fallback" when
    some parts do not fit and _pack's result is returned as it is. `wanted` asks for more plates
    than _pack needs (see _plate_count)."""
    base = _pack(parts, bed_x, bed_y, margin)
    plate_count, note = _plate_count(base, wanted)
    base = dict(base, min_plates=len(base["plates"]), count_note=note)
    if base["problems"] or not base["plates"]:
        return dict(base, smart="fallback")
    gap = max(0.0, margin)
    items = [it for plate in base["plates"] for it in plate["items"]]
    # Footprints as the part lies before any 90° turn; _place_from_centre picks the orientation.
    for it in items:
        it["fw"], it["fh"] = (it["h"], it["w"]) if it["rotated"] else (it["w"], it["h"])

    def unturned(group):
        return [dict(it, w=it["fw"], h=it["fh"]) for it in group]

    # Even plates: largest parts first, each onto the least filled plate it still fits on.
    balanced = [[] for _ in range(plate_count)]
    for it in sorted(items, key=lambda it: -it["w"] * it["h"]):
        loads = sorted(range(plate_count), key=lambda i: (sum(o["w"] * o["h"] for o in balanced[i]), i))
        for i in loads:
            if _place_from_centre([(o["fw"], o["fh"]) for o in balanced[i] + [it]], bed_x, bed_y, gap, gap) is not None:
                balanced[i].append(it)
                break
        else:
            balanced = None
            break

    def row(it, x, y, w, h, turned):
        return {"name": it["name"], "x": x, "y": y, "w": w, "h": h, "z": it["z"],
                "rotated": turned, "angle": it["angle"], "member": it.get("member"), "source_dims": it["source_dims"]}

    def dense(group):
        """A plate too full to lay out from the centre: _pack's dense layout of just this
        group, moved to the middle of the bed; None if _pack needs more than one plate for it."""
        own = _pack([{"name": it["name"], "member": it.get("member"), "dims": [it["fw"], it["fh"], it["z"]],
                      "source_dims": it["source_dims"], "angle": it["angle"]} for it in group], bed_x, bed_y, gap)
        if own["problems"] or len(own["plates"]) != 1:
            return None
        rows = own["plates"][0]["items"]
        spots = _centred([(r["x"], r["y"], r["w"], r["h"], r["rotated"]) for r in rows], bed_x, bed_y)
        return [dict(r, x=x, y=y) for r, (x, y, _, _, _) in zip(rows, spots)]

    def build(groups, last_resort):
        """Plates for these groups of items, or None if a group fits neither way on one plate;
        with last_resort (the groups of _pack's plates) such a plate keeps _pack's layout, centred."""
        plates = []
        for group in groups:
            placed = _smart_plate(unturned(group), bed_x, bed_y, gap)
            if placed is not None:
                rows = [row(it, *spot) for it, spot in zip(group, placed)]
            else:
                rows = dense(group)
                if rows is None:
                    if not last_resort:
                        return None
                    spots = _centred([(it["x"], it["y"], it["w"], it["h"], it["rotated"]) for it in group], bed_x, bed_y)
                    rows = [row(it, *spot) for it, spot in zip(group, spots)]
            plates.append({"items": rows})
        return plates

    plates = build(balanced, False) if balanced else None
    if plates is None:
        plates = build([list(plate["items"]) for plate in base["plates"]], True)
    return {"plates": [p for p in plates if p["items"]], "problems": [], "bed": base["bed"], "total": base["total"],
            "smart": True, "min_plates": base["min_plates"], "count_note": note}


def _iter_stl_triangles(raw):
    """Yield every triangle from a validated binary or ASCII STL."""
    if len(raw) >= 84:
        count = struct.unpack_from("<I", raw, 80)[0]
        if count <= 2_000_000 and 84 + count * 50 == len(raw):
            for record in struct.iter_unpack("<12fH", memoryview(raw)[84:]):
                yield record[3:12]
            return
    text = raw.decode("utf-8", "replace")
    triangle = []
    count = 0
    for match in re.finditer(r"\bvertex\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)", text, re.I):
        point = tuple(float(v) for v in match.groups())
        if not all(math.isfinite(value) for value in point):
            raise ValueError(_t("err.stl_coords"))
        triangle.extend(point)
        if len(triangle) == 9:
            count += 1
            if count > 2_000_000:
                raise ValueError(_t("err.stl_facets_limit"))
            yield tuple(triangle)
            triangle = []
    if triangle or not count:
        raise ValueError(_t("err.stl_geometry"))


def _indexed_mesh(raw):
    """Merge the per-facet STL corners into shared vertices, as Orca's STL loader does."""
    # Dicts keep insertion order, so a corner's index is the number of corners seen before it.
    index = {}
    triangles = []
    for triangle in _iter_stl_triangles(raw):
        a = index.setdefault(triangle[0:3], len(index))
        b = index.setdefault(triangle[3:6], len(index))
        c = index.setdefault(triangle[6:9], len(index))
        # Degenerate facets would only produce mesh errors in Orca.
        if a != b and b != c and a != c:
            triangles.append((a, b, c))
    vertices = list(index)
    # The sum is not finite if any coordinate is NaN or infinite; float32 values cannot overflow it.
    if not math.isfinite(sum(map(sum, vertices))):
        raise ValueError(_t("err.stl_coords"))
    if not triangles:
        raise ValueError(_t("err.stl_no_triangles"))
    return vertices, triangles


def _plate_columns(count):
    # Same rule as compute_colum_count() in Orca's PartPlate.hpp.
    value = math.sqrt(count)
    rounded = math.floor(value + 0.5)
    return int(rounded + 1 if value > rounded else rounded)


def _plate_origins(count, bed_x, bed_y):
    """World offsets of Orca's plates: the grid Orca builds for `count` plates."""
    # Orca keeps the plate size as whole millimetres (PartPlateList::reset_size takes ints).
    stride_x = int(bed_x) * (1.0 + ORCA_PLATE_GAP)
    stride_y = int(bed_y) * (1.0 + ORCA_PLATE_GAP)
    cols = max(1, _plate_columns(count))
    return [((i % cols) * stride_x, -(i // cols) * stride_y) for i in range(count)]


def _write_orca_project(path, plates, model_blobs, profile, app_version, project_settings):
    """Write an Orca multi-plate 3MF with the active settings (see _project_settings()).

    Orca rebuilds plate membership from object positions when it loads a project,
    so every object is placed inside its own plate of Orca's plate grid.
    """
    if not plates:
        raise ValueError(_t("err.plan_first"))
    size = profile.get("size") or {}
    min_x = float(size.get("origin_x", 0.0))
    min_y = float(size.get("origin_y", 0.0))
    origins = _plate_origins(len(plates), float(size.get("x", 0.0)), float(size.get("y", 0.0)))
    build_items = []
    object_records = []
    plate_object_ids = []
    object_id = 1
    total_facets = 0
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml" />'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml" />'
        '</Types>')
    relationships = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" />'
        '</Relationships>')
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr('[Content_Types].xml', content_types)
        archive.writestr('_rels/.rels', relationships)
        with archive.open('3D/3dmodel.model', 'w') as model_file:
            def emit(text):
                model_file.write(text.encode('utf-8'))
            emit('<?xml version="1.0" encoding="UTF-8"?>')
            emit('<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021">')
            emit(f'<metadata name="Application">OrcaSlicer-{app_version}</metadata>'
                 '<metadata name="BambuStudio:3mfVersion">1</metadata>'
                 '<metadata name="Title">PlatePilot layout</metadata><resources>')
            for plate_index, plate in enumerate(plates):
                plate_x, plate_y = origins[plate_index]
                current_plate = []
                for item in plate["items"]:
                    raw = model_blobs.get(item["member"])
                    if raw is None:
                        raise ValueError(_t("err.reread", name=item["name"]))
                    vertices, triangles = _indexed_mesh(raw)
                    total_facets += len(triangles)
                    if total_facets > MAX_PROJECT_FACETS:
                        raise ValueError(_t("err.facets_limit", n=f"{MAX_PROJECT_FACETS:,}".replace(",", " ")))
                    # Turn the part about Z (a diagonal turn from _turn_to_fit plus the packer's
                    # 90°), then centre its footprint, so it fills exactly the cell the plan gave it.
                    angle = math.radians(float(item.get("angle", 0)) + (90 if item.get("rotated") else 0))
                    c, s = math.cos(angle), math.sin(angle)
                    if abs(s) < 1e-12 or abs(c) < 1e-12:
                        c, s = round(c), round(s)
                    turned = [(x * c - y * s, x * s + y * c, z) for x, y, z in vertices]
                    lo = [min(v[axis] for v in turned) for axis in range(3)]
                    hi = [max(v[axis] for v in turned) for axis in range(3)]
                    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
                    # Written in batches: the XML of a part with two million facets is about 140 MB.
                    emit(f'<object id="{object_id}" name={quoteattr(str(item["name"]))} type="model"><mesh><vertices>')
                    for start in range(0, len(turned), EXPORT_BATCH):
                        emit(''.join(f'<vertex x="{x - cx:.5f}" y="{y - cy:.5f}" z="{z - lo[2]:.5f}"/>'
                                     for x, y, z in turned[start:start + EXPORT_BATCH]))
                    del turned
                    emit('</vertices><triangles>')
                    for start in range(0, len(triangles), EXPORT_BATCH):
                        emit(''.join(f'<triangle v1="{v1}" v2="{v2}" v3="{v3}"/>'
                                     for v1, v2, v3 in triangles[start:start + EXPORT_BATCH]))
                    emit('</triangles></mesh></object>')
                    tx = plate_x + min_x + float(item["x"]) + float(item["w"]) / 2
                    ty = plate_y + min_y + float(item["y"]) + float(item["h"]) / 2
                    build_items.append(f'<item objectid="{object_id}" transform="1 0 0 0 1 0 0 0 1 {tx:.5f} {ty:.5f} 0" printable="1"/>')
                    current_plate.append(object_id)
                    object_records.append((object_id, str(item["name"]), len(triangles)))
                    object_id += 1
                plate_object_ids.append(current_plate)
            emit('</resources><build>')
            emit(''.join(build_items))
            emit('</build></model>')

        model_config = ['<?xml version="1.0" encoding="UTF-8"?>', '<config>']
        for oid, name, facets in object_records:
            model_config.append(f'<object id="{oid}"><metadata key="name" value={quoteattr(name)}/>'
                                f'<metadata key="extruder" value="1"/>'
                                f'<part id="1" subtype="normal_part"><metadata key="name" value={quoteattr(name)}/>'
                                f'<metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>'
                                f'<mesh_stat face_count="{facets}" edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/></part></object>')
        for index, ids in enumerate(plate_object_ids, 1):
            model_config.extend(['<plate>',
                f'<metadata key="plater_id" value="{index}"/>',
                f'<metadata key="plater_name" value="PlatePilot {index}"/>',
                '<metadata key="locked" value="false"/>'])
            for oid in ids:
                model_config.extend(['<model_instance>',
                    f'<metadata key="object_id" value="{oid}"/>',
                    '<metadata key="instance_id" value="0"/>',
                    f'<metadata key="identify_id" value="{oid}"/>', '</model_instance>'])
            model_config.append('</plate>')
        model_config.append('</config>')
        archive.writestr('Metadata/model_settings.config', ''.join(model_config))
        archive.writestr('Metadata/project_settings.config', project_settings)


def _write_cli_settings(folder):
    """Flatten the active printer and process presets into files for `--load-settings`.

    Values are taken from the edited presets, so unsaved changes in the sidebar are used too.
    """
    bundle = orca.host.preset_bundle()
    files = []
    for kind, preset in (("machine", bundle.current_printer_preset()), ("process", bundle.current_process_preset())):
        if preset is None:
            raise ValueError(_t("err.no_printer_preset" if kind == "machine" else "err.no_process_preset"))
        data = {"type": kind, "from": "User", "name": f"PlatePilot {kind}"}
        for key in preset.config_keys():
            key = str(key)
            if key in CLI_SKIP_KEYS:
                continue
            value = preset.config_value(key)
            if value is not None:
                data[key] = str(value)
        path = os.path.join(folder, kind + ".json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
        files.append(path)
    return files


def _project_settings():
    """The active printer, process and filament settings as Metadata/project_settings.config.

    Orca loads only the geometry of a 3MF without project settings: it skips the plate list
    and puts every object on plate 1 (Plater.cpp, "load_config && config_loaded.empty()").
    Writing the current full configuration, with the *_settings_id names of the selected
    presets, lets Orca create the plates and select the same presets on open.
    """
    bundle = orca.host.preset_bundle()
    data = {"name": "project_settings", "from": "project"}
    for key in bundle.full_config_keys():
        value = bundle.full_config_value(key)
        if value is not None:
            data[str(key)] = str(value)
    if not data.get("printer_settings_id"):
        raise ValueError(_t("err.no_settings"))
    return json.dumps(data, ensure_ascii=False, indent=4)


def _finish_cli_project(path, settings, names=None):
    """Prepare the 3MF the CLI exported for opening in Orca.

    Swaps the configuration the CLI embeds (flattened "PlatePilot machine/process" presets)
    for the active Orca configuration, so opening keeps the selected presets, and renames the
    objects from the short input file names ("001.stl") back to the part names.
    """
    names = names or {}

    def rename(match):
        return match.group(1) + quoteattr(names.get(match.group(2), match.group(2)), {'"': "&quot;"})

    temp = path + ".tmp"
    with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            if info.filename in ("Metadata/project_settings.config", "Metadata/slice_info.config"):
                continue
            data = source.read(info)
            if info.filename == "Metadata/model_settings.config" and names:
                text = data.decode("utf-8")
                text = re.sub(r'(<metadata key="name" value=)"([^"]*)"', rename, text)
                data = text.encode("utf-8")
            target.writestr(info, data)
        target.writestr("Metadata/project_settings.config", settings)
    os.replace(temp, path)


def _cli_unplaced(run_dir):
    """How many parts Orca's CLI arrange could not put on any plate. It says so only in its own
    log ("can not be arranged inside plate!", PartPlate.cpp), which it writes into the folder it
    runs in, and leaves those parts beside the plates."""
    count = 0
    for name in os.listdir(run_dir):
        if name.endswith(".log") and name != "orca-cli.log":
            try:
                with open(os.path.join(run_dir, name), "r", encoding="utf-8", errors="replace") as stream:
                    count += stream.read().count("can not be arranged inside plate")
            except OSError:
                pass
    return count


def _count_plates(path):
    with zipfile.ZipFile(path, "r") as project:
        try:
            settings = project.read("Metadata/model_settings.config").decode("utf-8", "replace")
        except KeyError:
            return 0
    return len(re.findall(r"<plate(?:\s|>)", settings))


class PlatePilotPage(orca.pages.PagesPluginCapabilityBase):
    def get_name(self):
        return "PlatePilot"

    def get_icon(self):
        icon = os.path.join(os.path.dirname(__file__), "platepilot.svg")
        return icon if os.path.isfile(icon) else ""

    def get_ui(self):
        return PAGE

    def _config(self):
        try:
            value = json.loads(self.get_config())
            if not isinstance(value, dict):
                value = {}
        except Exception:
            value = {}
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        if "folders" not in value and value.get("folder"):
            value["folders"] = [value["folder"]]
        config.update(value)
        # Settings of 0.4 and older: the plugin's own folder consent, timed checks and the
        # "ask after every N archives" prompt are gone.
        for key in ("folder", "folder_grants", "auto_refresh", "poll_seconds", "batch_size"):
            config.pop(key, None)
        configured_types = config.get("model_types", ["stl", "step", "3mf"])
        if not isinstance(configured_types, (list, tuple, set)):
            configured_types = ["stl", "step", "3mf"]
        config["model_types"] = [t for t in ("stl", "step", "3mf") if t in configured_types]
        # Older versions stored paths verbatim, so the same folder could appear twice
        # ("G:\Download" and "g:\download\"). Keep the first spelling of each folder.
        folders, seen = [], set()
        for folder in config.get("folders", []):
            try:
                folder = _clean_folder_input(folder)
            except ValueError:
                continue
            if _folder_key(folder) not in seen:
                seen.add(_folder_key(folder))
                folders.append(folder)
        config["folders"] = folders
        config["check_on_open"] = bool(config.get("check_on_open", True))
        config["gpu_preview"] = bool(config.get("gpu_preview", True))
        if config.get("language") not in ("auto",) + LANGUAGES:
            config["language"] = "auto"
        return config

    def get_default_config(self):
        return json.loads(json.dumps(DEFAULT_CONFIG))

    def _save(self, config):
        self.save_config(json.dumps(config, ensure_ascii=False))

    def _notice(self, message, level="info"):
        self.post_message({"type": "notice", "message": message, "level": level})

    def _publish_folders(self, config):
        self.post_message({"type": "config", "config": config})
        self.post_message({"type": "folder_list", "folders": config["folders"]})

    @staticmethod
    def _storage(*parts):
        """A folder inside the plugin's data folder, created on first use. Orca lets a plugin
        read and write there without asking."""
        try:
            folder = os.path.join(orca.host.plugin.storage(), *parts)
        except Exception as exc:
            raise ValueError(_t("err.no_storage", error=str(exc)[:160]))
        os.makedirs(folder, exist_ok=True)
        return folder

    def _added_root(self):
        return self._storage("added")

    @staticmethod
    def _is_added(path):
        """True for a path inside the folder of files dropped or pasted into PlatePilot."""
        try:
            return _is_inside(path, os.path.join(orca.host.plugin.storage(), "added"))
        except Exception:
            return False

    def _library(self):
        """The saved library state: seen sources, opened archives' model counts, folder summaries."""
        if getattr(self, "_library_state", None) is None:
            try:
                with open(os.path.join(self._storage(), "library.json"), "r", encoding="utf-8") as stream:
                    state = json.load(stream)
            except (OSError, ValueError):
                state = {}
            self._library_state = state if isinstance(state, dict) else {}
        return self._library_state

    def _save_library(self):
        try:
            path = os.path.join(self._storage(), "library.json")
            with open(path + ".tmp", "w", encoding="utf-8") as stream:
                json.dump(self._library(), stream, ensure_ascii=False)
            os.replace(path + ".tmp", path)
        except (OSError, ValueError):
            pass

    def _add_folder(self, config, folder):
        """Watch a folder the user chose, skipping duplicates and folders already covered by another one."""
        if not os.path.isdir(folder):
            raise ValueError(_t("err.folder_missing", path=folder))
        if not _orca_can_read(folder):
            raise ValueError(_t("err.unsafe_path", path=folder))
        key = _folder_key(folder)
        recursive = config.get("recursive", True)
        for existing in config["folders"]:
            if _folder_key(existing) == key:
                self._publish_folders(config)
                self._notice(_t("msg.already_watched", path=existing))
                return
            if recursive and _is_inside(folder, existing):
                self._publish_folders(config)
                self._notice(_t("msg.inside_watched", path=existing))
                return
        covered = [p for p in config["folders"] if recursive and _is_inside(p, folder)]
        config["folders"] = [p for p in config["folders"] if p not in covered] + [folder]
        self._save(config)
        self._publish_folders(config)
        self._scan(config)
        if covered:
            self._notice(_t("msg.replaced_nested", paths=", ".join(covered)))

    def _roots(self, config):
        """(folder, origin) of every place sources come from: the watched folders and, once
        something was dropped into PlatePilot, its own folder of added files."""
        roots = [(folder, "watched") for folder in config.get("folders", [])]
        try:
            added = os.path.join(orca.host.plugin.storage(), "added")
        except Exception:
            added = ""
        if added and os.path.isdir(added):
            roots.append((added, "added"))
        return roots

    def _scan(self, config):
        """Find the sources in every root in one call and send the library and folder summaries."""
        library = self._library()
        summaries = library.setdefault("folders", {})
        names = library.get("names", {})
        rows = []
        roots = self._roots(config)
        marker = self._scan_marker()
        if marker:
            # Left behind only if Orca closes during the check; the page then skips the next
            # automatic check instead of repeating it on every start.
            with open(marker, "w", encoding="utf-8") as stream:
                stream.write(str(time.time()))
        try:
            for folder, origin in roots:
                key = _folder_key(folder)
                if not os.path.isdir(folder):
                    summaries[key] = {"checked": time.time(), "missing": True}
                    continue
                if not _orca_can_read(folder):
                    summaries[key] = {"checked": time.time(), "unreadable": True}
                    continue
                found, truncated, skipped = _scan_folder(folder, config.get("recursive", True) or origin == "added")
                # Files dropped or pasted into PlatePilot are news too: they show under New until seen.
                first_scan = origin != "added" and "sources" not in summaries.get(key, {})
                summaries[key] = _update_library(library, folder, found, truncated, first_scan)
                summaries[key]["skipped"] = skipped
                for row in found:
                    row["added"] = origin == "added"
                    if row["added"]:
                        # The user put these here on purpose, whatever they are called.
                        row["name"] = self._display_name(folder, row["path"], names)
                        row["likely"] = row.get("count") != 0
                if origin == "added":
                    summaries[key]["sources"] += summaries[key].pop("other", 0)
                rows.extend(found)
        finally:
            if marker:
                try:
                    os.remove(marker)
                except OSError:
                    pass
        current = {_folder_key(folder) for folder, _ in roots}
        for key in [k for k in summaries if k not in current]:
            del summaries[key]
        self._save_library()
        self._rows = rows
        self._post_rows(config)

    def _scan_marker(self):
        try:
            return os.path.join(self._storage(), "scan-running")
        except ValueError:
            return ""

    @staticmethod
    def _display_name(root, path, names):
        """The name an added source was dropped with: its files are stored under names that
        Orca's permission check accepts (see _safe_name), the original names are kept here."""
        parts, current = [], root
        for part in os.path.relpath(path, root).split(os.sep):
            current = os.path.join(current, part)
            parts.append(names.get(current, part))
        return os.path.join(*parts)

    def _post_rows(self, config):
        hidden = config.get("hidden", {})
        enabled_types = set(config.get("model_types", ["stl", "step", "3mf"]))
        visible = []
        for row in getattr(self, "_rows", []):
            # 0.4 and older hid model folders with an empty fingerprint.
            if row["path"] in hidden and hidden[row["path"]] in (row.get("fingerprint"), "" if row["kind"] == "folder" else None):
                continue
            counts = row.get("type_counts")
            # An archive that was never opened is listed as it is: only opening it tells what it holds.
            if counts is not None and not any(counts.get(t, 0) for t in enabled_types):
                continue
            item = {k: row.get(k) for k in ("kind", "name", "path", "count", "size", "root", "warning", "new", "added", "likely", "time")}
            item["type_counts"] = None if counts is None else {MODEL_TYPE_LABELS[t]: counts.get(t, 0) for t in MODEL_TYPE_LABELS if t in enabled_types and counts.get(t, 0)}
            visible.append(item)
        visible.sort(key=lambda item: -(item.get("time") or 0))   # newest first
        summaries = self._library().get("folders", {})
        folders = [{"path": folder, "origin": origin, **summaries.get(_folder_key(folder), {})} for folder, origin in self._roots(config)]
        self.post_message({"type": "archives", "archives": visible, "folders": folders})

    def _row(self, path):
        return next((r for r in getattr(self, "_rows", []) if r["path"] == path), None)

    def _mark_seen(self, rows):
        known = self._library().setdefault("known", {})
        for row in rows:
            known[row["path"]] = row["fingerprint"]
            row["new"] = False
        self._save_library()

    def _selected_root(self, path, config):
        for folder, _ in self._roots(config):
            if _safe_archive_path(path, folder):
                return os.path.realpath(folder)
        raise ValueError(_t("err.source_gone"))

    def _open_source(self, config, path, kind, mark_seen=True):
        """Read a source's parts and make it the selected one; opening it marks it seen, except
        right after an import, whose sources stay under New."""
        root = self._selected_root(path, config)
        row = self._row(path)
        if not self._is_added(path) and not _orca_can_read(path):
            raise ValueError(_t("err.unsafe_path", path=path))
        if kind == "zip":
            source = (path, "zip", root, ())
            parts = _read_archive(path, root)
            if row is not None:
                counts = _type_counts(p["name"] for p in parts)
                self._library().setdefault("zips", {})[path] = {"fingerprint": row["fingerprint"], "counts": counts}
                row["type_counts"], row["count"] = counts, sum(counts.values())
                row["likely"] = _likely_models(row)
        elif self._is_added(path):
            source = (path, "folder", root, ())
            parts = _read_loose_folder(path, root)
        else:
            if not _safe_archive_path(path, root):
                raise ValueError(_t("err.folder_outside"))
            names = row["files"] if row else sorted(n for n in os.listdir(path) if _model_type_for_name(n))
            unsafe = [n for n in names if not _orca_can_read(os.path.join(path, n))]
            readable = [n for n in names if n not in unsafe and _model_type_for_name(n) in ("stl", "3mf")
                        and os.path.getsize(os.path.join(path, n)) <= MAX_STL_BYTES]
            catalog = tuple((n, _t("warn.unsafe_name") if n in unsafe else "") for n in names
                            if n not in readable and _model_type_for_name(n) != "step")
            copy = _snapshot_folder(path, readable, self._storage("folders"))
            source = (copy, "folder", copy, catalog)
            parts = _read_loose_folder(copy, copy, catalog)
            # STEP files are not copied: their part points at the original, which Orca opens.
            steps = {n for n in names if _model_type_for_name(n) == "step"}
            for part in parts:
                if part["filetype"] == "step" and part["name"] in steps:
                    part["member"] = os.path.join(path, part["name"])
                    part["open_in_orca"] = part["name"] not in unsafe
        self._selected_source = source
        self._last_plan = None
        if row is not None and mark_seen:
            self._mark_seen([row])
        self.post_message({"type": "archive", "path": path, "kind": kind, "name": os.path.basename(path), "parts": parts,
                           "added": self._is_added(path)})
        self._post_rows(config)

    def _current_parts(self, config):
        source = getattr(self, "_selected_source", None)
        if not source:
            raise ValueError(_t("err.select_source"))
        path, kind, root, catalog = source
        if kind == "zip":
            return _read_archive(path, root)
        return _read_loose_folder(path, root, catalog)

    # Files dropped or pasted into the page arrive in chunks: import_start, then per file
    # import_begin and import_chunk until the file is complete, then import_finish.
    def _import_start(self, data):
        self._import_abort()
        self._import = {"loose_count": int(data.get("loose", 0)), "first": str(data.get("first", "")),
                        "loose_dir": None, "dirs": {}, "sources": [], "files": 0, "file": None}

    def _import_abort(self):
        job = getattr(self, "_import", None)
        current = job and job.get("file")
        if current:
            current["stream"].close()
            try:
                os.remove(current["part"])
            except OSError:
                pass
        self._import = None

    def _import_target(self, job, name, top, rel):
        """Where a dropped file goes inside the folder of added files. Names are made safe for
        Orca's permission check; the original names are kept for the library list."""
        added = self._added_root()
        names = self._library().setdefault("names", {})

        def remember(path, original):
            if os.path.basename(path) != original:
                names[path] = original
            return path

        safe = _safe_name(name)
        if top:
            base = job["dirs"].get(top)
            if base is None:
                base = remember(_unique_path(added, _safe_name(top, "folder")), top)
                job["dirs"][top] = base
            folder = base
            for part in [p for p in rel.split("/")[:-1][:6] if p]:
                folder = remember(os.path.join(folder, _safe_name(part, "folder")), part)
        elif safe.lower().endswith(".zip"):
            folder = added
        else:
            if job["loose_dir"] is None:
                group = os.path.splitext(job["first"])[0] if job["loose_count"] == 1 else _t("import.group", time=time.strftime("%Y-%m-%d %H-%M"))
                job["loose_dir"] = remember(_unique_path(added, _safe_name(group, "files")), group)
            folder = job["loose_dir"]
        target = _unique_path(folder, safe)
        if len(target) > MAX_IMPORT_PATH:
            target = _unique_path(job["dirs"].get(top) or job["loose_dir"] or added, safe)
        if len(target) > MAX_IMPORT_PATH or not _safe_archive_path(target, added):
            raise ValueError(_t("err.import_path", name=name))
        return remember(target, name)

    def _import_begin(self, data):
        job = getattr(self, "_import", None)
        if job is None or job.get("file"):
            raise ValueError(_t("err.import_state"))
        name = os.path.basename(str(data.get("name", "")).replace("\\", "/"))
        size = int(data.get("size", -1))
        if os.path.splitext(name)[1].lower() not in IMPORT_EXTENSIONS:
            raise ValueError(_t("err.import_type", name=name))
        if not 0 <= size <= MAX_IMPORT_BYTES:
            raise ValueError(_t("err.import_size", name=name, mb=MAX_IMPORT_BYTES // (1024 * 1024)))
        target = self._import_target(job, name, str(data.get("top", "") or ""), str(data.get("rel", "") or name))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        job["file"] = {"target": target, "part": target + ".part", "size": size, "written": 0,
                       "stream": open(target + ".part", "wb")}
        self._import_chunk({"data": ""})

    def _import_chunk(self, data):
        job = getattr(self, "_import", None)
        current = job and job.get("file")
        if not current:
            raise ValueError(_t("err.import_state"))
        raw = base64.b64decode(str(data.get("data", "")), validate=True)
        if current["written"] + len(raw) > current["size"]:
            raise ValueError(_t("err.import_state"))
        current["stream"].write(raw)
        current["written"] += len(raw)
        done = current["written"] == current["size"]
        if done:
            current["stream"].close()
            os.replace(current["part"], current["target"])
            job["file"] = None
            job["files"] += 1
            source = current["target"] if current["target"].lower().endswith(".zip") else os.path.dirname(current["target"])
            if source not in job["sources"]:
                job["sources"].append(source)
        self.post_message({"type": "import_next", "written": current["written"], "done": done})

    def _import_finish(self, config):
        job = getattr(self, "_import", None)
        self._import = None
        if not job:
            return
        self._scan(config)
        if job["sources"]:
            first = job["sources"][0]
            self._open_source(config, first, "zip" if first.lower().endswith(".zip") else "folder", mark_seen=False)
        self._notice(_t("msg.imported", files=_plural("import.files", job["files"])), "ok")

    def _import_paths(self, config, text):
        """Paths pasted into the page, one per line as Explorer's "Copy as path" gives them:
        a folder is watched, a model file or archive is copied into PlatePilot."""
        folders, files = [], []
        for line in [l for l in str(text or "").splitlines() if l.strip()][:50]:
            path = _clean_folder_input(line)
            if not _orca_can_read(path):
                raise ValueError(_t("err.unsafe_path", path=path))
            if os.path.isdir(path):
                folders.append(path)
            elif os.path.isfile(path) and os.path.splitext(path)[1].lower() in IMPORT_EXTENSIONS:
                if os.path.getsize(path) > MAX_IMPORT_BYTES:
                    raise ValueError(_t("err.import_size", name=os.path.basename(path), mb=MAX_IMPORT_BYTES // (1024 * 1024)))
                files.append(path)
            else:
                raise ValueError(_t("err.paste_path", path=path))
        for folder in folders:
            self.post_message({"type": "folder_selected", "path": folder})
            self._add_folder(config, folder)
        if not files:
            return
        loose = [p for p in files if not p.lower().endswith(".zip")]
        self._import_start({"loose": len(loose), "first": os.path.basename(loose[0]) if loose else ""})
        job = self._import
        for path in files:
            name = os.path.basename(path)
            target = self._import_target(job, name, "", name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # One read of the user's file, which Orca asks about once; the copy is the plugin's own.
            with open(path, "rb") as source, open(target + ".part", "wb") as copy:
                shutil.copyfileobj(source, copy, 1024 * 1024)
            os.replace(target + ".part", target)
            job["files"] += 1
            source_path = target if target.lower().endswith(".zip") else os.path.dirname(target)
            if source_path not in job["sources"]:
                job["sources"].append(source_path)
        self._import_finish(config)

    def _delete_added(self, config, path):
        """Delete a source from the folder of added files; the user's own files are never touched."""
        added = self._added_root()
        row = self._row(path)
        if row is None or not row.get("added") or not _safe_archive_path(path, added) or _folder_key(path) == _folder_key(added):
            raise ValueError(_t("err.not_added"))
        if row["kind"] == "zip":
            os.remove(path)
        else:
            for name in row.get("files", []):
                try:
                    os.remove(os.path.join(path, name))
                except FileNotFoundError:
                    pass
            folder = path
            while _folder_key(folder) != _folder_key(added) and _safe_archive_path(folder, added) and not os.listdir(folder):
                os.rmdir(folder)
                folder = os.path.dirname(folder)
        source = getattr(self, "_selected_source", None)
        if source and _folder_key(source[0]) == _folder_key(path):
            self._selected_source = None
            self._last_plan = None
        self._scan(config)
        self._notice(_t("msg.deleted"), "ok")

    def _read_stl_blobs(self, source, parts, members):
        """The meshes of the selected parts as binary STL, read again from the current source with
        the safety checks; a 3MF object comes out of its file (read and converted once)."""
        by_member = {part.get("member"): part for part in parts if part.get("member")}
        blobs = {}
        if source[1] == "zip":
            if not _safe_archive_path(source[0], source[2]):
                raise ValueError(_t("err.archive_moved"))
            with zipfile.ZipFile(source[0], "r") as zf:
                for member in members:
                    if member not in by_member:
                        raise ValueError(_t("err.stl_left_archive"))
                    path, index = _split_member(member)
                    info = zf.getinfo(path)
                    if info.file_size > MAX_STL_BYTES:
                        raise ValueError(_t("err.stl_limit", mb=MAX_STL_BYTES // (1024 * 1024)))
                    if path.lower().endswith(".3mf"):
                        items = _threemf_cached(("zip", source[0], path, info.file_size, info.CRC), lambda info=info: zf.read(info))
                        blobs[member] = items[index or 0][1]
                    else:
                        blobs[member] = zf.read(info)
        else:
            for member in members:
                path, index = _split_member(member)
                if member not in by_member or not _safe_archive_path(path, source[2]):
                    raise ValueError(_t("err.stl_left_folder"))
                if path.lower().endswith(".3mf"):
                    size = os.path.getsize(path)
                    if size > MAX_STL_BYTES:
                        raise ValueError(_t("err.stl_limit", mb=MAX_STL_BYTES // (1024 * 1024)))

                    def read(path=path):
                        with open(path, "rb") as stream:
                            return stream.read()
                    blobs[member] = _threemf_cached(("file", path, size, os.path.getmtime(path)), read)[index or 0][1]
                    continue
                with open(path, "rb") as stream:
                    blobs[member] = stream.read(MAX_STL_BYTES + 1)
                if len(blobs[member]) > MAX_STL_BYTES:
                    raise ValueError(_t("err.stl_limit", mb=MAX_STL_BYTES // (1024 * 1024)))
        return blobs

    def _open_step_in_orca(self, config, index):
        """Hand a STEP part to this Orca window, which reads STEP itself and asks how finely to
        convert it. One inside an archive is taken out into PlatePilot's folder first."""
        source = getattr(self, "_selected_source", None)
        parts = self._current_parts(config)
        if index < 0 or index >= len(parts) or not parts[index].get("open_in_orca"):
            raise ValueError(_t("err.part_missing"))
        part = parts[index]
        if source[1] == "zip":
            target = os.path.join(self._storage("step"), _safe_name(part["name"]))
            with zipfile.ZipFile(source[0], "r") as zf:
                info = zf.getinfo(part["member"])
                if info.file_size > MAX_IMPORT_BYTES:
                    raise ValueError(_t("err.import_size", name=part["name"], mb=MAX_IMPORT_BYTES // (1024 * 1024)))
                with zf.open(info) as stream, open(target, "wb") as copy:
                    shutil.copyfileobj(stream, copy, 1024 * 1024)
            path = target
        else:
            path = part["member"]
            if not os.path.isfile(path):
                raise ValueError(_t("err.part_missing"))
        if os.name != "nt":
            raise OSError(_t("err.open_unsupported"))
        _send_to_own_window(path)
        self._notice(_t("msg.step_sent", name=part["name"]), "ok")

    def _start_native_arrange(self, config, included):
        if getattr(self, "_native_arrange_job", None):
            raise ValueError(_t("err.arrange_running"))
        source = getattr(self, "_selected_source", None)
        if not source:
            raise ValueError(_t("err.select_source"))
        included = set(str(value) for value in included)
        all_parts = self._current_parts(config)
        parts = [part for part in all_parts if part.get("filetype") == "stl" and part.get("member") in included]
        if not parts:
            raise ValueError(_t("err.select_stl"))
        if len(parts) > MAX_NATIVE_PARTS:
            raise ValueError(_t("err.too_many_parts", n=MAX_NATIVE_PARTS))
        profile = _get_profile()
        if not profile or not profile.get("size"):
            raise ValueError(_t("err.no_bed"))

        storage = orca.host.plugin.storage()
        run_dir = os.path.join(storage, "arrange-" + time.strftime("%y%m%d-%H%M%S"))
        input_dir = os.path.join(run_dir, "input")
        os.makedirs(input_dir, exist_ok=False)
        try:
            blobs = self._read_stl_blobs(source, all_parts, [part["member"] for part in parts])
            inputs = []
            names = {}
            for index, part in enumerate(parts, 1):
                path = self._write_native_input(input_dir, index, blobs[part["member"]])
                inputs.append(path)
                names[os.path.basename(path)] = part["name"]
            settings = _write_cli_settings(run_dir)
            project_settings = _project_settings()
            executable = _orca_executable()
            output = os.path.join(run_dir, "PlatePilot-AutoArrange.3mf")
            log_path = os.path.join(run_dir, "orca-cli.log")
            command = [executable, "--load-settings", ";".join(settings), "--arrange", "1"]
            # Orca arranges without turning parts unless told to; with it, it also tries 45°
            # and 135°, which is the only way a part longer than the bed side fits.
            bed_x, bed_y = float(profile["size"]["x"]), float(profile["size"]["y"])
            if any(not ((p["dims"][0] <= bed_x and p["dims"][1] <= bed_y) or (p["dims"][1] <= bed_x and p["dims"][0] <= bed_y))
                   for p in parts):
                command.append("--allow-rotations")
            command += ["--export-3mf", output, *inputs]
            with open(log_path, "w", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=run_dir, stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._native_arrange_job = {
                "process": process, "run_dir": run_dir, "input_dir": input_dir, "output": output,
                "log": log_path, "count": len(inputs), "started": time.time(), "project_settings": project_settings,
                "names": names,
            }
            self.post_message({"type": "native_arrange_started", "count": len(inputs)})
        except Exception:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

    @staticmethod
    def _write_native_input(input_dir, index, raw):
        # Short file names keep the path under Windows' 260-character limit even when the
        # plugin data folder is deep; the real part names are put back by _finish_cli_project().
        path = os.path.join(input_dir, f"{index:03d}.stl")
        with open(path, "wb") as stream:
            stream.write(raw)
        return path

    def _poll_native_arrange(self):
        job = getattr(self, "_native_arrange_job", None)
        if not job:
            return
        code = job["process"].poll()
        if code is None:
            if time.time() - job["started"] > 600:
                job["process"].kill()
            self.post_message({"type": "native_arrange_progress", "done": False})
            return
        self._native_arrange_job = None
        try:
            if code != 0 or not os.path.isfile(job["output"]):
                with open(job["log"], "r", encoding="utf-8", errors="replace") as stream:
                    detail = stream.read()[-800:].strip()
                raise RuntimeError(_t("err.cli_failed", detail=detail or _t("err.cli_exit", code=code)))
            shutil.rmtree(job["input_dir"], ignore_errors=True)
            _finish_cli_project(job["output"], job["project_settings"], job["names"])
            plate_count = _count_plates(job["output"])
            left_out = _cli_unplaced(job["run_dir"])
            _open_project(job["output"])
            message = _t("msg.arranged", parts=_plural("plan.parts", job["count"]), plates=_plural("plan.plates", max(1, plate_count)))
            if left_out:
                message = _t("msg.arranged_partly", n=min(left_out, job["count"]), parts=_plural("plan.parts", job["count"]))
            self.post_message({"type": "native_arrange_progress", "done": True, "ok": not left_out,
                               "message": message, "path": job["output"]})
        except Exception as exc:
            self.post_message({"type": "native_arrange_progress", "done": True, "ok": False,
                               "message": _t("err.arrange_failed", error=str(exc)[:500])})

    def on_message(self, data):
        try:
            kind = data.get("type")
            config = self._config()
            _set_language(config.get("language", "auto"))
            if kind == "init":
                platform = "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"
                marker = self._scan_marker()
                self.post_message({"type": "init", "config": config, "profile": _get_profile(), "strings": _ui_strings(),
                                   "language": _LANG, "platform": platform, "version": VERSION,
                                   "scan_interrupted": bool(marker) and os.path.exists(marker)})
            elif kind == "save_settings":
                incoming = data.get("config", {})
                for key in ("check_on_open", "recursive", "gpu_preview"):
                    config[key] = bool(incoming.get(key, config.get(key)))
                config["margin_mm"] = max(0.0, min(20.0, float(incoming.get("margin_mm", config.get("margin_mm", DEFAULT_MARGIN_MM)))))
                config["arrange_mode"] = "orca" if incoming.get("arrange_mode") == "orca" else "own"
                language = incoming.get("language", config.get("language", "auto"))
                config["language"] = language if language in ("auto",) + LANGUAGES else "auto"
                incoming_types = incoming.get("model_types", config.get("model_types", ["stl", "step", "3mf"]))
                if not isinstance(incoming_types, (list, tuple, set)):
                    incoming_types = ["stl", "step", "3mf"]
                config["model_types"] = [t for t in ("stl", "step", "3mf") if t in incoming_types]
                self._save(config)
                if getattr(self, "_rows", None) is not None:
                    self._post_rows(config)
            elif kind == "pick_folder":
                initial = str(data.get("initial", "") or "")
                try:
                    initial = _clean_folder_input(initial) if initial else ""
                except ValueError:
                    initial = ""
                selected = _pick_folder_windows(initial)
                if not selected:
                    self._notice(_t("msg.picker_cancelled"))
                    return
                folder = _clean_folder_input(selected)
                self.post_message({"type": "folder_selected", "path": folder})
                self._add_folder(config, folder)
            elif kind == "request_folder_add":
                folder = _clean_folder_input(data.get("path", ""))
                self.post_message({"type": "folder_selected", "path": folder})
                self._add_folder(config, folder)
            elif kind == "remove_folder":
                target = data.get("path")
                folders = list(config.get("folders", []))
                if target is None:
                    index = int(data.get("index", -1))
                    target = folders[index] if 0 <= index < len(folders) else None
                if target:
                    removed = _folder_key(target)
                    config["folders"] = [p for p in folders if _folder_key(p) != removed]
                    self._save(config)
                    self._publish_folders(config)
                    self._scan(config)
            elif kind == "scan":
                try:
                    self._scan(config)
                except Exception as exc:
                    raise ValueError(_t("err.scan_stopped", error=str(exc)))
                self.post_message({"type": "profile", "profile": _get_profile()})
            elif kind == "mark_seen":
                self._mark_seen(list(getattr(self, "_rows", [])))
                self._post_rows(config)
            elif kind == "open_model":
                self._open_source(config, str(data.get("path", "")), "zip" if data.get("kind") == "zip" else "folder")
            elif kind == "hide_source":
                path = str(data.get("path", ""))
                row = self._row(path)
                if row:
                    config.setdefault("hidden", {})[path] = row.get("fingerprint", "")
                    self._save(config)
                    self._post_rows(config)
                    self._notice(_t("msg.source_hidden"))
            elif kind == "delete_source":
                self._delete_added(config, str(data.get("path", "")))
            elif kind == "paste_path":
                self._import_paths(config, data.get("text", ""))
            elif kind == "import_start":
                self._import_start(data)
            elif kind == "import_begin":
                try:
                    self._import_begin(data)
                except Exception:
                    self._import_abort()
                    raise
            elif kind == "import_chunk":
                try:
                    self._import_chunk(data)
                except Exception:
                    self._import_abort()
                    raise
            elif kind == "import_finish":
                self._import_finish(config)
            elif kind == "import_cancel":
                self._import_abort()
            elif kind == "open_in_orca":
                self._open_step_in_orca(config, int(data.get("index", -1)))
            elif kind == "preview_part":
                source = getattr(self, "_selected_source", None)
                parts = self._current_parts(config)
                index = int(data.get("index", -1))
                if index < 0 or index >= len(parts):
                    raise ValueError(_t("err.part_missing"))
                member = parts[index].get("member")
                if not member or parts[index].get("filetype") not in ("stl", "3mf") or parts[index].get("blocked"):
                    raise ValueError(_t("err.no_preview"))
                if source[1] != "zip" and not _safe_archive_path(_split_member(member)[0], source[2]):
                    raise ValueError(_t("err.stl_outside"))
                raw = self._read_stl_blobs(source, parts, [member])[member]
                # The page asks for fewer facets when it draws without WebGL.
                limit = max(1000, min(MAX_PREVIEW_FACETS, int(data.get("max_facets") or MAX_PREVIEW_FACETS)))
                # Simplifying a large part takes about a second, so the last few previews are kept.
                previews = getattr(self, "_previews", None)
                if previews is None:
                    previews = self._previews = {}
                key = (hashlib.blake2b(raw, digest_size=16).digest(), limit)
                info = previews.pop(key, None) or _binary_stl_info(raw, include_preview=True, max_facets=limit)
                previews[key] = info
                # A full preview of 250 000 facets is 12 MB of text, so the cache is limited by size too.
                while len(previews) > 1 and (len(previews) > 12 or sum(len(v["vertices"] or "") for v in previews.values()) > 96 * 1024 * 1024):
                    previews.pop(next(iter(previews)))
                self.post_message({"type": "preview", "name": parts[index]["name"], "facets": info["facets"], "dims": info["dims"], "vertices": info["vertices"]})
            elif kind == "plan":
                source = getattr(self, "_selected_source", None)
                all_parts = self._current_parts(config)
                included_members = set(str(n) for n in data.get("included", []))
                selected = [p for p in all_parts if p.get("member") in included_members and p["included"]]
                profile = _get_profile()
                if not profile or not profile.get("size"):
                    raise ValueError(_t("err.no_printable_area"))
                margin = float(data.get("margin", DEFAULT_MARGIN_MM))
                bed_x, bed_y = profile["size"]["x"], profile["size"]["y"]
                gap = max(0.0, margin)
                too_wide = [p for p in selected if not ((p["dims"][0] <= bed_x - 2 * gap and p["dims"][1] <= bed_y - 2 * gap)
                                                        or (p["dims"][1] <= bed_x - 2 * gap and p["dims"][0] <= bed_y - 2 * gap))]
                if too_wide:
                    blobs = self._read_stl_blobs(source, all_parts, [p["member"] for p in too_wide])
                    selected = [dict(p, source_dims=p["dims"]) for p in selected]
                    selected = _turn_to_fit(selected, blobs, bed_x, bed_y, margin)
                try:
                    wanted = max(0, min(50, int(data.get("plates") or 0)))
                except (TypeError, ValueError):
                    wanted = 0
                pack = _pack_smart if data.get("smart") else _pack_into
                result = pack(selected, bed_x, bed_y, margin, wanted)
                max_z = profile.get("height")
                too_tall = [p["name"] for p in selected if max_z and p["dims"][2] > max_z]
                exportable = bool(result["plates"]) and not result["problems"] and not too_tall
                self.post_message({"type": "plan", "plates": result["plates"], "bed": profile["size"], "profile": profile,
                                   "total": result["total"], "problems": result["problems"], "too_tall": too_tall,
                                   "exclusions": profile.get("exclusions"), "exportable": exportable, "smart": result.get("smart", False),
                                   "min_plates": result.get("min_plates"), "count_note": result.get("count_note")})
                self._last_plan = {"source": source, "plates": result["plates"], "profile": profile, "exportable": exportable}
            elif kind == "export_project":
                plan = getattr(self, "_last_plan", None)
                source = getattr(self, "_selected_source", None)
                if not plan or not source or plan.get("source") != source:
                    raise ValueError(_t("err.plan_source"))
                if not plan.get("exportable"):
                    raise ValueError(_t("err.plan_not_exportable"))
                active_profile = _get_profile()
                planned_profile = plan["profile"]
                if (not active_profile or active_profile.get("name") != planned_profile.get("name") or
                    any(abs(float(active_profile.get("size", {}).get(axis, 0)) - float(planned_profile.get("size", {}).get(axis, 0))) > 0.001
                        for axis in ("x", "y", "origin_x", "origin_y"))):
                    raise ValueError(_t("err.printer_changed"))
                parts = self._current_parts(config)
                by_member = {part.get("member"): part for part in parts if part.get("member")}
                for plate in plan["plates"]:
                    for item in plate["items"]:
                        current = by_member.get(item.get("member"))
                        if current is None or any(abs(float(a) - float(b)) > 0.001 for a, b in zip(current.get("dims", []), item.get("source_dims", []))):
                            raise ValueError(_t("err.stl_changed"))
                members = {item.get("member") for plate in plan["plates"] for item in plate["items"]}
                blobs = self._read_stl_blobs(source, parts, members)
                storage = orca.host.plugin.storage()
                output = os.path.join(storage, "PlatePilot-" + time.strftime("%Y%m%d-%H%M%S") + ".3mf")
                _write_orca_project(output, plan["plates"], blobs, plan["profile"], _app_version(), _project_settings())
                try:
                    _open_project(output)
                    self.post_message({"type": "project_ready", "path": output, "opened": True})
                except Exception as exc:
                    self.post_message({"type": "project_ready", "path": output, "opened": False, "error": str(exc)[:180]})
            elif kind == "native_arrange":
                self._start_native_arrange(config, data.get("included", []))
            elif kind == "native_arrange_tick":
                self._poll_native_arrange()
            else:
                return
        except Exception as exc:
            self.post_message({"type": "error", "message": str(exc)})


@orca.plugin
class PlatePilotPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(PlatePilotPage)
