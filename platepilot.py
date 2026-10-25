# /// script
# requires-python = ">=3.9"
# [tool.orcaslicer.plugin]
# name = "PlatePilot"
# description = "Model library for STL bundles: preview parts on the active printer bed, arrange them on several plates and open the project in Prepare."
# author = "GNUGiminot"
# version = "0.4.0"
# ///

"""PlatePilot: an OrcaSlicer plugin that browses STL bundles and sends arranged multi-plate projects to Prepare."""

import base64
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
import zipfile

import orca


VERSION = "0.4.0"
MAX_ARCHIVES = 500
MAX_STL_BYTES = 30 * 1024 * 1024
MAX_TOTAL_STL_BYTES = 200 * 1024 * 1024
MAX_PREVIEW_FACETS = 30000
MAX_PROJECT_FACETS = 600000
MAX_NATIVE_PARTS = 200
DEFAULT_MARGIN_MM = 4.0
MAX_SCAN_DIRECTORIES = 20000
SCAN_DIRECTORIES_PER_TICK = 12
SCAN_ARCHIVES_PER_TICK = 2
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
    "folders": [], "folder_grants": [], "hidden": {},
    "auto_refresh": False, "poll_seconds": 30, "recursive": True,
    "batch_size": 10, "margin_mm": DEFAULT_MARGIN_MM,
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
    "unit.mm": ("mm", "мм"),
    "btn.plan": ("Calculate", "Рассчитать"),
    "btn.send": ("Open in Prepare", "В «Подготовку»"),
    "btn.arrange": ("Arrange in Orca", "Расставить в Orca"),
    "mode.own_note": ("PlatePilot packs the parts by their footprint on the active printer's bed, shows the plates and opens the project in Prepare.",
                      "PlatePilot раскладывает детали по габаритам на столе активного принтера, показывает столы и отправляет готовый проект в «Подготовку»."),
    "mode.orca_note": ("The parts go to Orca's own arrange: it uses the part outlines and the active printer and process profiles, adds plates as needed and opens the project.",
                       "Детали передаются в Orca: её авторасстановка учитывает контуры деталей и активные профили принтера и процесса, сама добавляет столы и открывает проект."),
    "drawer.title": ("Sources and refresh", "Источники и обновление"),
    "drawer.close": ("Close", "Закрыть"),
    "drawer.folders": ("Watched folders", "Отслеживаемые папки"),
    "drawer.folders_note": ("PlatePilot looks for ZIP archives and unpacked model folders in them.", "PlatePilot ищет в них ZIP-архивы и распакованные каталоги с моделями."),
    "drawer.pick": ("Choose folder…", "Выбрать папку…"),
    "drawer.paste": ("Or paste a folder path (quotes are fine, as from \"Copy as path\"):", "Или вставьте путь вручную (можно с кавычками, как из «Копировать как путь»):"),
    "drawer.paste_only": ("Paste the full path of a folder with models:", "Вставьте полный путь к папке с моделями:"),
    "drawer.path_ph": ("Full folder path", "Полный путь к папке"),
    "drawer.add": ("Add", "Добавить"),
    "drawer.formats": ("Formats", "Форматы"),
    "drawer.formats_note": ("Sources without the selected formats are hidden. Preview and layout work with STL.", "Источники без выбранных форматов скрываются. Просмотр и раскладка доступны для STL."),
    "drawer.scan": ("Scanning", "Сканирование"),
    "drawer.recursive": ("Search subfolders", "Искать в подпапках"),
    "drawer.auto": ("Check folders automatically", "Проверять папки автоматически"),
    "drawer.interval": ("Check interval, s", "Интервал проверки, сек"),
    "drawer.batch": ("Ask after every N new ZIP files", "Спрашивать после каждых N новых ZIP"),
    "drawer.audit_note": ("Orca may ask separately for access to files. That is Orca's own protection, and the plugin does not bypass it.",
                          "Orca может отдельно запросить доступ к файлам — это защита самой Orca, плагин её не обходит."),
    "drawer.language": ("Language", "Язык"),
    "drawer.lang_auto": ("Same as Orca", "Как в Orca"),
    "modal.confirm": ("Confirm", "Подтверждение"),
    "modal.cancel": ("Cancel", "Отмена"),
    "modal.continue": ("Continue", "Продолжить"),
    # page: dynamic text
    "st.choose_folder": ("Open Folders and choose a folder with models.", "Откройте «Папки» и выберите папку с моделями."),
    "st.press_refresh": ("Press Refresh to scan the folders.", "Нажмите «Обновить», чтобы просканировать папки."),
    "st.scanning": ("Scanning folders…", "Сканирую папки…"),
    "st.scanning_n": ("Scanning folders… found {n}", "Сканирую папки… найдено {n}"),
    "st.scanned": ("Folders checked · sources: {n}", "Папки проверены · источников: {n}"),
    "st.opening_picker": ("Opening the folder picker…", "Открываю выбор папки…"),
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
    "lib.add_folder": ("Add a folder with models under Folders.", "Добавьте папку с моделями в настройках «Папки»."),
    "src.choose": ("Choose a source", "Выберите источник"),
    "src.choose_note": ("Pick an archive or folder in the library. PlatePilot shows its parts and a preview on the bed.", "Архив или папку из библиотеки слева — PlatePilot покажет её детали и предпросмотр на столе."),
    "src.summary": ("{n} model files · {stl} STL · {path}", "{n} файлов моделей · {stl} STL · {path}"),
    "src.hide": ("Hide", "Скрыть"),
    "src.hide_tip": ("Hide from the list. The file stays on disk.", "Скрыть из списка, файл на диске останется"),
    "parts.meta": ("{dims} mm · {facets} facets", "{dims} мм · {facets} граней"),
    "parts.catalog_only": ("{type} · catalog only", "{type} · только в каталоге"),
    "parts.no_files": ("The source has no files of the selected formats.", "В источнике нет файлов выбранных форматов."),
    "parts.placeholder": ("Parts appear here.", "Детали появятся здесь."),
    "view.meta": ("{dims} mm · {facets} facets", "{dims} мм · {facets} граней"),
    "view.shown": (" · {n} shown", " · показано {n}"),
    "plan.parts": ("{n} part|{n} parts", "{n} деталь|{n} детали|{n} деталей"),
    "plan.plates": ("{n} plate|{n} plates", "{n} стол|{n} стола|{n} столов"),
    "plan.not_fit": ("does not fit: {name}", "не помещается: {name}"),
    "plan.too_tall": ("taller than Z: {name}", "выше Z: {name}"),
    "plan.zones": ("check excluded areas in Orca", "проверьте исключённые зоны в Orca"),
    "plan.plate": ("Plate {n}", "Стол {n}"),
    "plan.plate_meta": ("{parts} · {fill}%", "{parts} · {fill}%"),
    "plan.rotated": (" · rotated 90°", " · повёрнута на 90°"),
    "folders.none": ("No folders yet.", "Папки ещё не добавлены."),
    "folders.remove": ("Stop watching", "Перестать отслеживать"),
    "consent.title": ("Watch this folder?", "Отслеживать эту папку?"),
    "consent.body": ("PlatePilot will list the files in this folder and read ZIP, STL, STEP and 3MF files for the library:\n{path}\n\nOrca may also ask for file access separately.",
                     "PlatePilot будет просматривать файлы в этой папке и читать ZIP, STL, STEP и 3MF для каталога:\n{path}\n\nOrca также может отдельно спросить доступ к файлам."),
    "consent.body_recursive": ("PlatePilot will list the files in this folder and its subfolders and read ZIP, STL, STEP and 3MF files for the library:\n{path}\n\nOrca may also ask for file access separately.",
                               "PlatePilot будет просматривать файлы в этой папке и её подпапках и читать ZIP, STL, STEP и 3MF для каталога:\n{path}\n\nOrca также может отдельно спросить доступ к файлам."),
    "consent.yes": ("Watch", "Отслеживать"),
    "batch.title": ("Continue scanning?", "Продолжить сканирование?"),
    "batch.body": ("{n} new archives read. Read the next batch?", "Прочитано {n} новых архивов. Читать следующую партию?"),
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
    "warn.stl_only": ("{type}: preview and layout work with STL only", "{type}: просмотр и раскладка доступны только для STL"),
    "warn.too_big": ("file larger than 30 MB", "файл больше 30 MB"),
    "err.total_archive": ("the STL files in the archive exceed 200 MB in total", "суммарный размер STL в архиве превышает 200 MB"),
    "err.total_folder": ("the STL files in the folder exceed 200 MB in total", "суммарный размер STL в папке превышает 200 MB"),
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
    "err.stl_limit": ("the STL exceeds the 30 MB limit", "STL превышает лимит 30 MB"),
    "err.arrange_running": ("Orca auto-arrange is already running", "авторасстановка Orca уже выполняется"),
    "err.select_stl": ("select at least one STL part", "отметь хотя бы одну STL-деталь"),
    "err.too_many_parts": ("one run can arrange at most {n} STL files", "за один запуск можно расставить не более {n} STL"),
    "err.no_bed": ("could not read the printable area of the active printer", "не удалось прочитать рабочую область активного принтера"),
    "err.cli_failed": ("The Orca CLI did not create a project. {detail}", "CLI Orca не создал проект. {detail}"),
    "err.cli_exit": ("Exit code: {code}", "Код завершения: {code}"),
    "msg.arranged": ("Orca arranged {parts} on {plates} and is opening the project. If Prepare already has models, choose \"Open as project\" to keep the plates.",
                     "Orca расставила {parts} на {plates} и открывает проект. Если в «Подготовке» уже есть модели, выберите «Открыть как проект», чтобы сохранить столы."),
    "err.arrange_failed": ("Auto-arrange did not finish: {error}", "Авторасстановка не завершилась: {error}"),
    "msg.picker_cancelled": ("Folder selection cancelled.", "Выбор папки отменён."),
    "msg.folder_rejected": ("The folder was not added.", "Папка не добавлена."),
    "msg.source_hidden": ("The source is hidden from the list; the file on disk is kept.", "Источник скрыт из списка; файл на диске не удалён."),
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
button.icon{width:32px;padding:0}
body input,body select{font:inherit;color:var(--fg);background:var(--raised);border:1px solid var(--line);border-radius:var(--r-sm);height:32px;padding:0 10px;min-width:0}
body input:focus,body select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
input[type=number]{width:76px}
.muted{color:var(--muted)}.warn{color:var(--warn)}
/* app bar */
.appbar{display:flex;align-items:center;gap:14px;padding:10px 16px;border-bottom:1px solid var(--line);background:linear-gradient(100deg,color-mix(in srgb,var(--accent) 9%,var(--bg)),var(--bg) 46%)}
.brand{display:flex;align-items:center;gap:10px;min-width:0}.brand-mark{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;background:linear-gradient(145deg,color-mix(in srgb,#fff 18%,var(--accent)),color-mix(in srgb,#000 18%,var(--accent)));color:#fff;box-shadow:0 8px 18px -10px var(--accent)}
.brand-mark svg{width:20px;height:20px}.brand h1{font-size:16px;margin:0;letter-spacing:.2px}.brand small{display:block;color:var(--muted);font-size:11.5px;margin-top:-1px}
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
.chip{height:26px;padding:0 10px;border-radius:999px;font-size:12px;background:transparent}label.chip{display:inline-flex;align-items:center;border:1px solid var(--line);cursor:pointer;user-select:none}.chip.on{background:var(--accent-soft);border-color:var(--accent-line);color:var(--accent);font-weight:600}
.list{overflow:auto;flex:1;padding:0 6px 8px;border-top:1px solid var(--line)}
.source{display:flex;gap:10px;align-items:flex-start;padding:9px 8px;margin-top:4px;border-radius:var(--r-sm);cursor:pointer;border:1px solid transparent}
.source:hover{background:var(--surface-2)}.source.on{background:var(--accent-soft);border-color:var(--accent-line)}
.source .glyph{width:30px;height:30px;border-radius:8px;display:grid;place-items:center;flex:none;background:var(--surface-2);color:var(--muted)}.source.on .glyph{background:var(--accent);color:#fff}
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
.part.off{cursor:default;opacity:.62}.part .t{min-width:0;flex:1}.part .n{overflow-wrap:anywhere;line-height:1.3}.part .m{color:var(--muted);font-size:11.5px;margin-top:2px}
.check{appearance:none;width:18px;height:18px;margin:0;flex:none;border-radius:5px;border:1.5px solid color-mix(in srgb,var(--fg) 30%,transparent);background:var(--raised);display:grid;place-items:center;cursor:pointer;padding:0}
.check:checked{background:var(--accent);border-color:var(--accent)}.check:checked::after{content:"";width:9px;height:5px;border:2px solid #fff;border-top:0;border-right:0;transform:translateY(-1px) rotate(-45deg)}.check:disabled{opacity:.4;cursor:default}
.viewer{position:relative;overflow:hidden;background:radial-gradient(120% 90% at 50% 0%,color-mix(in srgb,var(--accent) 7%,var(--surface)),var(--surface) 60%)}
canvas{position:absolute;inset:0;display:block;width:100%;height:100%;cursor:grab;touch-action:none}canvas:active{cursor:grabbing}
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
.plates{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;margin-top:12px;max-height:34vh;overflow:auto;padding:2px}
.plate{border:1px solid var(--line);border-radius:10px;background:var(--raised);padding:9px}
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
 <symbol id="i-logo" viewBox="0 0 24 24"><path d="M3 15.5 12 20l9-4.5M3 11 12 15.5 21 11 12 6.5z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 3v3.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></symbol>
 <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M20 11a8 8 0 0 0-14.6-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.6 4.5L20 16m0 4v-4h-4"/></symbol>
 <symbol id="i-folder" viewBox="0 0 24 24"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l2 2h8.8A1.5 1.5 0 0 1 21 9.5v8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z"/></symbol>
 <symbol id="i-zip" viewBox="0 0 24 24"><path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4M11 4v2m0 2v2m0 2v2"/><rect x="9.5" y="14" width="3" height="3.5" rx=".8"/></symbol>
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
 <div class="appbar-actions"><button id="scan" class="primary"><svg class="ic"><use href="#i-refresh"/></svg><span data-i18n="btn.refresh">Refresh</span></button><button id="openSettings"><svg class="ic"><use href="#i-sliders"/></svg><span data-i18n="btn.folders">Folders</span></button></div>
</header>
<div class="progress"></div>

<main class="workspace">
 <section class="card library">
  <div class="card-head"><h2 data-i18n="lib.title">Library</h2><span id="libCount" class="count">0</span></div>
  <div class="search"><svg class="ic"><use href="#i-search"/></svg><input id="filter" data-i18n-ph="lib.search" placeholder="Search by name"></div>
  <div class="chips" id="kindChips"><button class="chip on" data-kind="all" data-i18n="lib.all">All</button><button class="chip" data-kind="zip">ZIP</button><button class="chip" data-kind="folder" data-i18n="lib.folders">Folders</button></div>
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
    <canvas id="view"></canvas>
    <div class="hud"><div class="t"><b id="viewTitle"></b><span id="viewMeta" class="muted"></span></div><button id="resetView" class="icon" data-i18n-title="view.reset" title="Reset view" hidden><svg class="ic"><use href="#i-target"/></svg></button></div>
    <div id="viewHint" class="hint" data-i18n="view.hint" hidden>Drag to rotate · wheel to zoom · double-click to reset</div>
    <div id="viewEmpty" class="empty"><svg class="ic"><use href="#i-cube"/></svg><div data-i18n="view.empty">Select an STL part to see it on the printer bed</div></div>
   </div>
  </div>
  <div class="card layout-panel">
   <div class="layout-row">
    <div class="seg" id="modeSeg"><button data-mode="own" class="on"><svg class="ic"><use href="#i-grid"/></svg><span data-i18n="mode.own">PlatePilot layout</span></button><button data-mode="orca"><svg class="ic"><use href="#i-wand"/></svg><span data-i18n="mode.orca">Orca auto-arrange</span></button></div>
    <label class="field" id="marginField"><span data-i18n="mode.gap">Gap</span> <input id="margin" type="number" min="0" max="20" step="0.5" value="4"> <span data-i18n="unit.mm">mm</span></label>
    <span class="spacer"></span>
    <button id="plan"><svg class="ic"><use href="#i-grid"/></svg><span data-i18n="btn.plan">Calculate</span></button>
    <button id="sendPrepare" class="primary" disabled><svg class="ic"><use href="#i-send"/></svg><span data-i18n="btn.send">Open in Prepare</span></button>
    <button id="nativeArrange" class="primary" hidden><svg class="ic"><use href="#i-wand"/></svg><span data-i18n="btn.arrange">Arrange in Orca</span></button>
   </div>
   <div id="modeNote" class="mode-note"></div>
   <div id="summary" class="summary"></div>
   <div id="plates" class="plates"></div>
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
   <button id="pickFolder" class="primary"><svg class="ic"><use href="#i-folder"/></svg><span data-i18n="drawer.pick">Choose folder…</span></button>
   <p id="pasteNote" style="margin:12px 0 6px" data-i18n="drawer.paste">Or paste a folder path:</p>
   <div class="row"><input id="folder" data-i18n-ph="drawer.path_ph" placeholder="Full folder path"><button id="addFolder"><svg class="ic"><use href="#i-plus"/></svg><span data-i18n="drawer.add">Add</span></button></div>
  </div>
  <div class="section"><h3 data-i18n="drawer.formats">Formats</h3><p data-i18n="drawer.formats_note">Sources without the selected formats are hidden.</p>
   <div class="chips" style="padding:0"><label class="chip on" data-type="stl"><input type="checkbox" id="modelStl" hidden checked>STL</label><label class="chip on" data-type="step"><input type="checkbox" id="modelStep" hidden checked>STEP / STP</label><label class="chip on" data-type="3mf"><input type="checkbox" id="model3mf" hidden checked>3MF</label></div>
  </div>
  <div class="section"><h3 data-i18n="drawer.scan">Scanning</h3>
   <label class="toggle"><span data-i18n="drawer.recursive">Search subfolders</span><input id="recursive" class="switch" type="checkbox" checked></label>
   <label class="toggle"><span data-i18n="drawer.auto">Check folders automatically</span><input id="autoRefresh" class="switch" type="checkbox"></label>
   <label class="toggle"><span data-i18n="drawer.interval">Check interval, s</span><input id="pollSeconds" type="number" min="30" max="3600" value="30"></label>
   <label class="toggle"><span data-i18n="drawer.batch">Ask after every N new ZIP files</span><input id="batchSize" type="number" min="1" max="100" value="10"></label>
   <p style="margin-top:8px" data-i18n="drawer.audit_note">Orca may ask separately for access to files.</p>
  </div>
  <div class="section"><h3 data-i18n="drawer.language">Language</h3>
   <select id="language"><option value="auto" data-i18n="drawer.lang_auto">Same as Orca</option><option value="en">English</option><option value="ru">Русский</option></select>
  </div>
 </div>
</aside>

<div id="modal" class="modal" hidden><div class="dialog"><h2 id="modalTitle" data-i18n="modal.confirm">Confirm</h2><div id="modalText" class="body"></div><div class="actions"><button id="modalNo" data-i18n="modal.cancel">Cancel</button><button id="modalYes" class="primary" data-i18n="modal.continue">Continue</button></div></div></div>

<script>
const $=id=>document.getElementById(id);
const S={archives:[],selected:null,selectedKind:null,selectedName:'',parts:[],profile:null,config:{},mesh:null,drag:null,busy:false,pending:null,timer:null,scanTick:null,nativeTick:null,awaitingBatch:false,hasScanned:false,mode:'own',kind:'all',plan:null,previewIndex:-1,strings:{},lang:'en',platform:'windows'};
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
function setBusy(b){S.busy=b;document.body.classList.toggle('busy',b);$('scan').disabled=b;updateActions()}
function selectedStl(){return S.parts.filter(p=>p.included&&p.filetype==='stl'&&p.member)}
function updateActions(){const n=selectedStl().length;$('plan').disabled=S.busy||!n;$('sendPrepare').disabled=S.busy||!(S.plan&&S.plan.exportable);$('nativeArrange').disabled=S.busy||!n;$('partCount').textContent=S.parts.length?`${n} / ${S.parts.length}`:'0'}
function scheduleScanTick(delay=160){if(!S.busy||S.awaitingBatch||S.scanTick)return;S.scanTick=setTimeout(()=>{S.scanTick=null;if(S.busy&&!S.awaitingBatch)send('scan_tick')},delay)}
function stopScanTicks(){if(S.scanTick)clearTimeout(S.scanTick);S.scanTick=null}
function scheduleNativeTick(){if(S.nativeTick)return;S.nativeTick=setTimeout(()=>{S.nativeTick=null;send('native_arrange_tick')},700)}
function stopNativeTicks(){if(S.nativeTick)clearTimeout(S.nativeTick);S.nativeTick=null}
function requestScan(){if(S.busy)return;stopScanTicks();S.awaitingBatch=false;S.hasScanned=true;setBusy(true);status(t('st.scanning'));resetTimer();send('scan')}
function ask(title,body,kind,payload={},yes){S.pending={kind,payload};$('modalTitle').textContent=title;$('modalText').textContent=body;$('modalYes').textContent=yes||t('modal.continue');$('modal').hidden=false;$('modalYes').focus()}
function finishAsk(yes){const p=S.pending;S.pending=null;$('modal').hidden=true;if(!p)return;if(p.kind==='folder')send(yes?'confirm_folder':'reject_folder',{path:p.payload.path});else if(p.kind==='batch'){if(yes){S.awaitingBatch=false;send('continue_batch');scheduleScanTick()}else send('cancel_batch')}}
function openDrawer(open){$('drawer').classList.toggle('open',open);$('scrim').classList.toggle('open',open)}

/* settings */
function applyConfig(c){S.config=c||{};$('autoRefresh').checked=!!S.config.auto_refresh;$('pollSeconds').value=S.config.poll_seconds||30;$('recursive').checked=S.config.recursive!==false;$('batchSize').value=S.config.batch_size||10;const types=S.config.model_types||['stl','step','3mf'];for(const [k,id] of [['stl','modelStl'],['step','modelStep'],['3mf','model3mf']]){$(id).checked=types.includes(k);$(id).parentElement.classList.toggle('on',types.includes(k))}if(S.config.margin_mm!=null)$('margin').value=S.config.margin_mm;$('language').value=S.config.language||'auto';setMode(S.config.arrange_mode||'own',false);renderFolders();resetTimer()}
function renderFolders(){const folders=S.config.folders||[];$('folderList').innerHTML=folders.length?folders.map(p=>`<div class="folder">${icon('folder')}<span class="p">${esc(p)}</span><button class="icon ghost" title="${esc(t('folders.remove'))}" data-remove="${esc(p)}">${icon('x')}</button></div>`).join(''):`<div class="muted">${esc(t('folders.none'))}</div>`;document.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>send('remove_folder',{path:b.dataset.remove}))}
function saveUiSettings(){S.config.auto_refresh=$('autoRefresh').checked;S.config.poll_seconds=Math.max(30,Math.min(3600,Number($('pollSeconds').value)||30));$('pollSeconds').value=S.config.poll_seconds;S.config.recursive=$('recursive').checked;S.config.batch_size=Math.max(1,Math.min(100,Number($('batchSize').value)||10));S.config.model_types=['stl','step','3mf'].filter(k=>$(k==='stl'?'modelStl':k==='step'?'modelStep':'model3mf').checked);S.config.margin_mm=Math.max(0,Math.min(20,Number($('margin').value)||0));S.config.arrange_mode=S.mode;S.config.language=$('language').value;send('save_settings',{config:S.config});resetTimer()}
function resetTimer(){if(S.timer)clearInterval(S.timer);S.timer=null;if(S.config.auto_refresh&&S.hasScanned){S.timer=setInterval(()=>{if(!S.busy&&(S.config.folders||[]).length)requestScan()},Math.max(30,Number(S.config.poll_seconds)||30)*1000)}}

/* printer */
function setProfile(p){S.profile=p||null;if(!p||p.error){$('printer').innerHTML=`<span class="pill warn">${esc(t('printer.unreadable'))}</span><span class="name muted">${esc(p&&p.error||'')}</span>`;return}const s=p.size||{};$('printer').innerHTML=`<span class="name" title="${esc(p.name)}">${esc(p.name||'—')}</span><span class="spec"><span class="pill accent">${fmt(s.x)} × ${fmt(s.y)} ${esc(t('unit.mm'))}</span><span class="pill">Z ${fmt(p.height)}</span>${p.exclusions?`<span class="pill warn" title="${esc(t('printer.zones_tip'))}">${esc(t('printer.zones'))}</span>`:''}</span>`;if(S.mesh)renderMesh()}

/* library */
function renderArchives(){const q=$('filter').value.trim().toLowerCase();const list=S.archives.filter(x=>x.name.toLowerCase().includes(q)&&(S.kind==='all'||x.kind===S.kind));$('libCount').textContent=S.archives.length;if(S.selected&&!S.archives.some(x=>x.path===S.selected))clearSource(t('st.removed_source'));$('archives').innerHTML=list.length?list.map(a=>{const badges=Object.entries(a.type_counts||{}).map(([k,v])=>`<span class="pill">${esc(k)} ${v}</span>`).join('');return `<div class="source ${S.selected===a.path?'on':''}" data-path="${esc(a.path)}" data-kind="${esc(a.kind)}"><div class="glyph">${icon(a.kind==='folder'?'folder':'zip')}</div><div class="t"><div class="n">${esc(a.name)}</div><div class="m">${badges}<span class="pill">${esc(a.size)}</span>${a.warning?`<span class="pill warn">${esc(a.warning)}</span>`:''}</div></div></div>`}).join(''):`<div class="empty">${icon('folder')}<div>${esc(S.archives.length?t('lib.nothing_found'):(S.config.folders||[]).length?t('lib.press_refresh'):t('lib.add_folder'))}</div></div>`;document.querySelectorAll('.source').forEach(el=>el.onclick=()=>send('open_model',{path:el.dataset.path,kind:el.dataset.kind}))}
function clearSource(note){S.selected=null;S.parts=[];S.plan=null;S.mesh=null;S.previewIndex=-1;renderSourceHead();renderParts(note);renderPlan(null);drawEmptyView()}
function renderSourceHead(){if(!S.selected){$('sourceHead').innerHTML=`<div class="glyph">${icon('cube')}</div><div class="grow"><h2>${esc(t('src.choose'))}</h2><div class="sub">${esc(t('src.choose_note'))}</div></div>`;return}const stl=S.parts.filter(p=>p.filetype==='stl').length;$('sourceHead').innerHTML=`<div class="glyph">${icon(S.selectedKind==='folder'?'folder':'zip')}</div><div class="grow"><h2>${esc(S.selectedName)}</h2><div class="sub">${esc(t('src.summary',{n:S.parts.length,stl,path:S.selected}))}</div></div><button id="hideSelected" class="ghost" title="${esc(t('src.hide_tip'))}">${icon('eye-off')}${esc(t('src.hide'))}</button>`;$('hideSelected').onclick=()=>send('hide_source',{path:S.selected})}
function partMeta(p){if(Array.isArray(p.dims)&&p.dims.length===3&&p.filetype==='stl')return esc(t('parts.meta',{dims:p.dims.map(v=>fmt(v)).join(' × '),facets:Number(p.facets||0).toLocaleString(locale())}));return esc(t('parts.catalog_only',{type:(p.filetype||'model').toUpperCase()}))}
function renderParts(note){if(!S.parts.length){$('parts').innerHTML=`<div class="empty">${icon('cube')}<div>${esc(note||(S.selected?t('parts.no_files'):t('parts.placeholder')))}</div></div>`;updateActions();return}$('parts').innerHTML=S.parts.map((p,i)=>{const usable=p.filetype==='stl'&&p.member&&!p.blocked;return `<div class="part ${usable?'':'off'} ${S.previewIndex===i?'on':''}" data-index="${i}"><input class="check" type="checkbox" data-check="${i}" ${p.included?'checked':''} ${usable?'':'disabled'}><div class="t"><div class="n">${esc(p.name)}</div><div class="m">${partMeta(p)}${p.warning?` · <span class="warn">${esc(p.warning)}</span>`:''}</div></div></div>`}).join('');document.querySelectorAll('[data-check]').forEach(el=>{el.onclick=e=>e.stopPropagation();el.onchange=()=>{S.parts[Number(el.dataset.check)].included=el.checked;invalidatePlan();updateActions()}});document.querySelectorAll('.part:not(.off)').forEach(el=>el.onclick=()=>{S.previewIndex=Number(el.dataset.index);document.querySelectorAll('.part').forEach(x=>x.classList.toggle('on',x===el));send('preview_part',{index:S.previewIndex})});updateActions()}
function invalidatePlan(){if(S.plan){S.plan=null;renderPlan(null)}}

/* 3D preview */
function accentRgb(){const v=getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();const m=/^#([0-9a-f]{6})$/i.exec(v);if(m){const n=parseInt(m[1],16);return [n>>16&255,n>>8&255,n&255]}const r=/rgba?\((\d+)[ ,]+(\d+)[ ,]+(\d+)/.exec(v);return r?[+r[1],+r[2],+r[3]]:[0,150,136]}
function drawEmptyView(){const c=$('view');const ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);$('viewEmpty').hidden=false;$('viewHint').hidden=true;$('resetView').hidden=true;$('viewTitle').textContent='';$('viewMeta').textContent=''}
function fitZoom(lo,hi){const bed=Math.max(Number(S.profile?.size?.x)||250,Number(S.profile?.size?.y)||250),part=Math.max(1,...hi.map((v,i)=>v-lo[i]));return Math.max(1,Math.min(2.4,.5*bed/part))}
function drawMesh(data){if(!data||!data.vertices){S.mesh=null;drawEmptyView();return}const arr=Uint8Array.from(atob(data.vertices),x=>x.charCodeAt(0)),f=new Float32Array(arr.buffer,arr.byteOffset,Math.floor(arr.byteLength/4));let lo=[Infinity,Infinity,Infinity],hi=[-Infinity,-Infinity,-Infinity];for(let i=0;i<f.length;i+=3)for(let a=0;a<3;a++){lo[a]=Math.min(lo[a],f[i+a]);hi[a]=Math.max(hi[a],f[i+a])}S.mesh={f,lo,hi,yaw:.62,pitch:1.02,zoom:fitZoom(lo,hi),dims:data.dims,data};$('viewEmpty').hidden=true;$('viewHint').hidden=false;$('resetView').hidden=false;$('viewTitle').textContent=data.name;const shown=data.facets>f.length/9?t('view.shown',{n:Number(f.length/9).toLocaleString(locale())}):'';$('viewMeta').textContent=t('view.meta',{dims:data.dims.map(v=>fmt(v)).join(' × '),facets:Number(data.facets).toLocaleString(locale())})+shown;renderMesh()}
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
const L=[.35,-.55,.76],ln=Math.hypot(...L),lx=L[0]/ln,ly=L[1]/ln,lz=L[2]/ln,tris=[];
for(let i=0;i+8<f.length;i+=9){const a=rot(f[i]-cx,f[i+1]-cy,f[i+2]-z0),b=rot(f[i+3]-cx,f[i+4]-cy,f[i+5]-z0),d=rot(f[i+6]-cx,f[i+7]-cy,f[i+8]-z0);const ux=b[0]-a[0],uy=b[1]-a[1],uz=b[2]-a[2],vx=d[0]-a[0],vy=d[1]-a[1],vz=d[2]-a[2];let nx=uy*vz-uz*vy,ny=uz*vx-ux*vz,nz=ux*vy-uy*vx;const nl=Math.hypot(nx,ny,nz)||1;nx/=nl;ny/=nl;nz/=nl;const lit=Math.abs(nx*lx+ny*ly+nz*lz);tris.push({a:P(a),b:P(b),d:P(d),z:a[2]+b[2]+d[2],k:.32+.68*lit,rim:Math.pow(1-Math.abs(nz),3)})}
tris.sort((p,q)=>p.z-q.z);for(const tr of tris){const k=tr.k,r=Math.min(255,ar*k+40*tr.rim+(dark?14:0)),g=Math.min(255,ag*k+40*tr.rim+(dark?14:0)),b=Math.min(255,ab*k+48*tr.rim+(dark?18:0));ctx.beginPath();ctx.moveTo(...tr.a);ctx.lineTo(...tr.b);ctx.lineTo(...tr.d);ctx.closePath();ctx.fillStyle=`rgb(${r|0},${g|0},${b|0})`;ctx.fill();ctx.strokeStyle=ctx.fillStyle;ctx.lineWidth=.5;ctx.stroke()}
ctx.fillStyle=dark?'rgba(255,255,255,.55)':'rgba(30,40,50,.55)';ctx.font='11px system-ui,sans-serif';ctx.textAlign='right';ctx.fillText(`${fmt(bx)} × ${fmt(by)} ${t('unit.mm')}`,w-12,h-12)}

/* plan */
function renderPlan(plan){S.plan=plan;updateActions();if(!plan){$('summary').innerHTML='';$('plates').innerHTML='';return}const bx=plan.bed.x,by=plan.bed.y;const pills=[`<span class="pill accent">${esc(plural('plan.parts',plan.total))}</span>`,`<span class="pill accent">${esc(plural('plan.plates',plan.plates.length))}</span>`].concat((plan.problems||[]).map(p=>`<span class="pill warn">${esc(t('plan.not_fit',{name:p}))}</span>`)).concat((plan.too_tall||[]).map(p=>`<span class="pill warn">${esc(t('plan.too_tall',{name:p}))}</span>`));if(plan.exclusions)pills.push(`<span class="pill warn">${esc(t('plan.zones'))}</span>`);$('summary').innerHTML=pills.join('');
$('plates').innerHTML=plan.plates.map((p,pi)=>{const used=p.items.reduce((s,it)=>s+it.w*it.h,0)/(bx*by)*100;let grid='';for(let x=50;x<bx;x+=50)grid+=`<line x1="${x}" y1="0" x2="${x}" y2="${by}"/>`;for(let y=50;y<by;y+=50)grid+=`<line x1="0" y1="${y}" x2="${bx}" y2="${y}"/>`;const items=p.items.map((it,ii)=>{const col=PALETTE[(pi*3+ii)%PALETTE.length],y=by-it.y-it.h,fs=Math.max(5,Math.min(it.w/7,it.h/2.6,13));const label=it.w>22&&it.h>10?`<text x="${it.x+it.w/2}" y="${y+it.h/2}" font-size="${fs}" text-anchor="middle" dominant-baseline="central" fill="currentColor">${esc(it.name.replace(/\.stl$/i,'').slice(0,Math.max(3,Math.floor(it.w/fs*1.7))))}</text>`:'';return `<g><title>${esc(it.name)} · ${fmt(it.w)} × ${fmt(it.h)} ${esc(t('unit.mm'))}${it.rotated?esc(t('plan.rotated')):''}</title><rect x="${it.x}" y="${y}" width="${it.w}" height="${it.h}" rx="2.5" fill="${col}" fill-opacity=".26" stroke="${col}" stroke-width="1.2"/>${label}</g>`}).join('');return `<div class="plate"><div class="plate-head"><b>${esc(t('plan.plate',{n:pi+1}))}</b><span>${esc(t('plan.plate_meta',{parts:plural('plan.parts',p.items.length),fill:fmt(used,0)}))}</span></div><svg viewBox="-2 -2 ${bx+4} ${by+4}"><rect x="0" y="0" width="${bx}" height="${by}" rx="4" fill="var(--surface-2)" stroke="var(--accent-line)" stroke-width="1.2"/><g stroke="var(--line)" stroke-width=".6">${grid}</g>${items}</svg></div>`}).join('')}
function setMode(mode,save=true){S.mode=mode==='orca'?'orca':'own';document.querySelectorAll('#modeSeg button').forEach(b=>b.classList.toggle('on',b.dataset.mode===S.mode));const own=S.mode==='own';$('marginField').hidden=!own;$('plan').hidden=!own;$('sendPrepare').hidden=!own;$('nativeArrange').hidden=own;$('modeNote').textContent=t(own?'mode.own_note':'mode.orca_note');if(!own)renderPlan(null);if(save)saveUiSettings()}

/* wiring */
$('openSettings').onclick=()=>openDrawer(true);$('closeSettings').onclick=()=>openDrawer(false);$('scrim').onclick=()=>openDrawer(false);
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(!$('modal').hidden)finishAsk(false);else openDrawer(false)}});
$('pickFolder').onclick=()=>{status(t('st.opening_picker'));send('pick_folder',{initial:$('folder').value.trim()||(S.config.folders||[])[0]||''})};
$('addFolder').onclick=()=>{const path=$('folder').value.trim();if(path)send('request_folder_add',{path})};$('folder').onkeydown=e=>{if(e.key==='Enter')$('addFolder').click()};
$('scan').onclick=requestScan;$('filter').oninput=renderArchives;
document.querySelectorAll('#kindChips .chip').forEach(b=>b.onclick=()=>{S.kind=b.dataset.kind;document.querySelectorAll('#kindChips .chip').forEach(x=>x.classList.toggle('on',x===b));renderArchives()});
for(const id of ['autoRefresh','pollSeconds','recursive','batchSize'])$(id).onchange=saveUiSettings;
for(const id of ['modelStl','modelStep','model3mf'])$(id).onchange=()=>{$(id).parentElement.classList.toggle('on',$(id).checked);saveUiSettings()};
$('language').onchange=()=>{saveUiSettings();send('init')};
$('margin').onchange=()=>{invalidatePlan();saveUiSettings()};
$('all').onclick=()=>{S.parts.forEach(p=>{if(p.filetype==='stl'&&p.member&&!p.blocked)p.included=true});invalidatePlan();renderParts()};$('none').onclick=()=>{S.parts.forEach(p=>p.included=false);invalidatePlan();renderParts()};
document.querySelectorAll('#modeSeg button').forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
$('plan').onclick=()=>{renderPlan(null);send('plan',{margin:Number($('margin').value)||0,included:selectedStl().map(p=>p.member)})};
$('sendPrepare').onclick=()=>{if(!S.busy){status(t('st.building'));send('export_project')}};
$('nativeArrange').onclick=()=>{if(!S.busy)send('native_arrange',{included:selectedStl().map(p=>p.member)})};
$('modalYes').onclick=()=>finishAsk(true);$('modalNo').onclick=()=>finishAsk(false);
const view=$('view');view.onpointerdown=e=>{if(!S.mesh)return;S.drag={x:e.clientX,y:e.clientY};view.setPointerCapture(e.pointerId)};view.onpointermove=e=>{if(!S.drag||!S.mesh)return;S.mesh.yaw+=(e.clientX-S.drag.x)*.01;S.mesh.pitch=Math.max(.05,Math.min(1.55,S.mesh.pitch+(e.clientY-S.drag.y)*.01));S.drag={x:e.clientX,y:e.clientY};renderMesh()};view.onpointerup=()=>S.drag=null;
view.onwheel=e=>{if(!S.mesh)return;e.preventDefault();S.mesh.zoom=Math.max(.4,Math.min(6,S.mesh.zoom*(e.deltaY<0?1.12:1/1.12)));renderMesh()};
const resetView=()=>{if(S.mesh){S.mesh.yaw=.62;S.mesh.pitch=1.02;S.mesh.zoom=fitZoom(S.mesh.lo,S.mesh.hi);renderMesh()}};view.ondblclick=resetView;$('resetView').onclick=resetView;
window.onresize=()=>renderMesh();new ResizeObserver(()=>renderMesh()).observe(view);

orca.onMessage(msg=>{const k=msg.type;
 if(k==='init'){S.strings=msg.strings||{};S.lang=msg.language||'en';S.platform=msg.platform||'windows';applyStrings();const win=S.platform==='windows';$('pickFolder').hidden=!win;$('pasteNote').textContent=t(win?'drawer.paste':'drawer.paste_only');applyConfig(msg.config||{});setProfile(msg.profile);renderSourceHead();renderParts();renderArchives();if(S.plan)renderPlan(S.plan);if(S.mesh)drawMesh(S.mesh.data);else drawEmptyView();status(t((msg.config?.folders||[]).length?'st.press_refresh':'st.choose_folder'))}
 else if(k==='config'){applyConfig(msg.config||{})}
 else if(k==='profile'){setProfile(msg.profile)}
 else if(k==='scan_started'){setBusy(true);status(t('st.scanning'));scheduleScanTick()}
 else if(k==='archives'){S.archives=msg.archives||[];renderArchives();if(msg.complete){stopScanTicks();S.awaitingBatch=false;setBusy(false);status(t('st.scanned',{n:S.archives.length}),'ok')}else{setBusy(true);status(t('st.scanning_n',{n:S.archives.length}));scheduleScanTick()}}
 else if(k==='folder_selected'){if(msg.path)$('folder').value=msg.path}
 else if(k==='folder_consent'){stopScanTicks();setBusy(true);ask(t('consent.title'),t(msg.recursive?'consent.body_recursive':'consent.body',{path:msg.path}),'folder',{path:msg.path},t('consent.yes'))}
 else if(k==='batch_consent'){stopScanTicks();setBusy(true);S.awaitingBatch=true;ask(t('batch.title'),t('batch.body',{n:msg.processed}),'batch')}
 else if(k==='folder_list'){S.config.folders=msg.folders||[];renderFolders();renderArchives()}
 else if(k==='archive'){S.selected=msg.path;S.selectedKind=msg.kind;S.selectedName=msg.name;S.parts=msg.parts||[];S.previewIndex=-1;S.mesh=null;renderSourceHead();renderArchives();renderParts();renderPlan(null);drawEmptyView();status(msg.error||t('st.opened_source',{name:msg.name}),msg.error?'warn':'info')}
 else if(k==='preview'){drawMesh(msg)}
 else if(k==='plan'){setProfile(msg.profile);renderPlan(msg);status(t(msg.exportable?'st.plan_ready':'st.plan_problems'),msg.exportable?'ok':'warn')}
 else if(k==='project_ready'){setBusy(false);status(msg.opened?t('st.project_opened'):t('st.project_saved',{path:msg.path,error:msg.error||''}),msg.opened?'ok':'warn')}
 else if(k==='native_arrange_started'){setBusy(true);status(t('st.arranging',{n:msg.count}));scheduleNativeTick()}
 else if(k==='native_arrange_progress'){if(msg.done){stopNativeTicks();setBusy(false);status(msg.message||t('st.arrange_done'),msg.ok?'ok':'error')}else scheduleNativeTick()}
 else if(k==='notice'){if(!S.awaitingBatch&&!S.scanTick&&!S.nativeTick)setBusy(false);status(msg.message||'',msg.level||'info')}
 else if(k==='error'){stopScanTicks();stopNativeTicks();S.awaitingBatch=false;setBusy(false);status(msg.message||t('st.plugin_error'),'error')}
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


def _binary_stl_info(raw, include_preview=False):
    if len(raw) < 84:
        raise ValueError(_t("err.stl_short"))
    count = struct.unpack_from("<I", raw, 80)[0]
    if count > 2_000_000 or 84 + count * 50 != len(raw):
        return _ascii_stl_info(raw, include_preview)
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    vertex_bytes = bytearray()
    preview_count = min(count, MAX_PREVIEW_FACETS)
    for i in range(count):
        off = 84 + i * 50 + 12
        tri = struct.unpack_from("<9f", raw, off)
        for j in range(0, 9, 3):
            for axis in range(3):
                value = tri[j + axis]
                if not math.isfinite(value):
                    raise ValueError(_t("err.stl_coords"))
                lo[axis] = min(lo[axis], value)
                hi[axis] = max(hi[axis], value)
        if include_preview and i < preview_count:
            vertex_bytes.extend(struct.pack("<9f", *tri))
    dims = [round(hi[i] - lo[i], 3) for i in range(3)]
    return {"facets": count, "dims": dims, "vertices": base64.b64encode(vertex_bytes).decode("ascii") if include_preview else None}


def _ascii_stl_info(raw, include_preview=False):
    text = raw.decode("utf-8", "replace")
    verts = []
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for match in re.finditer(r"\bvertex\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)", text, re.I):
        xyz = [float(v) for v in match.groups()]
        if not all(math.isfinite(v) for v in xyz):
            continue
        for axis, value in enumerate(xyz):
            lo[axis] = min(lo[axis], value)
            hi[axis] = max(hi[axis], value)
        if include_preview and len(verts) < MAX_PREVIEW_FACETS * 9:
            verts.extend(xyz)
    facets = len(re.findall(r"\bfacet\s+normal\b", text, re.I))
    if lo[0] == float("inf"):
        raise ValueError(_t("err.stl_vertices"))
    return {"facets": facets or len(verts) // 9, "dims": [round(hi[i] - lo[i], 3) for i in range(3)], "vertices": base64.b64encode(struct.pack("<%sf" % len(verts), *verts)).decode("ascii") if include_preview and verts else None}


def _safe_archive_path(path, root):
    try:
        full = os.path.realpath(path)
        base = os.path.realpath(root)
        return os.path.commonpath([full, base]) == base
    except (OSError, ValueError):
        return False


def _fingerprint(path):
    stat = os.stat(path)
    return f"{stat.st_size}:{getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1e9))}"


def _iter_sources(folders, recursive=True):
    rows = 0
    directories = 0
    for folder in folders:
        folder = os.path.realpath(os.path.expanduser(str(folder)))
        if not os.path.isdir(folder):
            continue
        walker = os.walk(folder, followlinks=False)
        for current, dirs, files in walker:
            directories += 1
            if directories > MAX_SCAN_DIRECTORIES:
                return
            yield {"kind": "scan_tick"}
            dirs[:] = sorted([d for d in dirs if not d.startswith(".")], key=str.casefold) if recursive else []
            zips = sorted([n for n in files if n.lower().endswith(".zip")], key=str.casefold)
            model_files = sorted([n for n in files if _model_type_for_name(n)], key=str.casefold)
            for name in zips:
                path = os.path.join(current, name)
                try:
                    yield {"kind": "zip", "name": name, "path": path, "root": folder,
                           "fingerprint": _fingerprint(path), "size": _fmt_size(os.path.getsize(path)), "count": None}
                    rows += 1
                except OSError:
                    continue
                if rows >= MAX_ARCHIVES:
                    return
            if model_files:
                path = os.path.realpath(current)
                if _safe_archive_path(path, folder):
                    total = 0
                    try:
                        for name in model_files:
                            total += os.path.getsize(os.path.join(current, name))
                        counts = _type_counts(model_files)
                        yield {"kind": "folder", "name": os.path.relpath(current, folder) if current != folder else os.path.basename(folder),
                               "path": path, "root": folder, "fingerprint": "", "size": _fmt_size(total),
                               "count": len(model_files), "type_counts": counts}
                        rows += 1
                    except OSError:
                        pass
                    if rows >= MAX_ARCHIVES:
                        return


def _inspect_zip(row):
    with zipfile.ZipFile(row["path"], "r") as zf:
        counts = _type_counts(entry.filename for entry in zf.infolist() if not entry.is_dir())
    row["type_counts"] = counts
    row["count"] = sum(counts.values())
    if not row["count"]:
        row["warning"] = _t("warn.zip_empty")
    return row


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
            if model_type != "stl":
                parts.append({"name": os.path.basename(info.filename), "member": info.filename,
                              "filetype": model_type, "dims": [], "facets": 0,
                              "included": False, "warning": _t("warn.stl_only", type=MODEL_TYPE_LABELS[model_type])})
                continue
            if info.file_size > MAX_STL_BYTES:
                parts.append({"name": os.path.basename(info.filename), "filetype": "stl", "blocked": True, "dims": [0, 0, 0], "facets": 0, "included": False, "warning": _t("warn.too_big")})
                continue
            total += info.file_size
            if total > MAX_TOTAL_STL_BYTES:
                raise ValueError(_t("err.total_archive"))
            raw = zf.read(info)
            try:
                stats = _binary_stl_info(raw)
                dims = stats["dims"]
                warning = "" if all(v > 0 for v in dims) else _t("warn.empty_model")
                parts.append({"name": os.path.basename(info.filename), "member": info.filename, "filetype": "stl", "dims": dims, "facets": stats["facets"], "included": True, "warning": warning})
            except Exception as exc:
                parts.append({"name": os.path.basename(info.filename), "member": info.filename, "filetype": "stl", "blocked": True, "dims": [0, 0, 0], "facets": 0, "included": False, "warning": _t("warn.stl_error", error=str(exc)[:100])})
    return parts


def _read_loose_folder(path, root):
    if not _safe_archive_path(path, root):
        raise ValueError(_t("err.folder_outside"))
    parts = []
    total = 0
    for name in sorted(os.listdir(path), key=str.casefold):
        model_type = _model_type_for_name(name)
        if not model_type:
            continue
        member = os.path.join(path, name)
        size = os.path.getsize(member)
        if model_type != "stl":
            parts.append({"name": name, "member": member, "filetype": model_type, "dims": [], "facets": 0,
                          "included": False, "warning": _t("warn.stl_only", type=MODEL_TYPE_LABELS[model_type])})
            continue
        if size > MAX_STL_BYTES:
            parts.append({"name": name, "member": member, "filetype": "stl", "blocked": True, "dims": [0, 0, 0], "facets": 0, "included": False, "warning": _t("warn.too_big")})
            continue
        total += size
        if total > MAX_TOTAL_STL_BYTES:
            raise ValueError(_t("err.total_folder"))
        with open(member, "rb") as stream:
            raw = stream.read()
        try:
            stats = _binary_stl_info(raw)
            parts.append({"name": name, "member": member, "filetype": "stl", "dims": stats["dims"], "facets": stats["facets"], "included": True, "warning": ""})
        except Exception as exc:
            parts.append({"name": name, "member": member, "filetype": "stl", "blocked": True, "dims": [0, 0, 0], "facets": 0, "included": False, "warning": _t("warn.stl_error", error=str(exc)[:100])})
    return parts


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
        items.append({"name": part["name"], "member": part.get("member"), "dims": [float(v) for v in dims[:3]], "w": w, "h": h, "z": float(dims[2]), "facets": int(part.get("facets", 0))})

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
                "w": w - gap, "h": h - gap, "z": item["z"], "rotated": rotated,
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


def _iter_stl_triangles(raw):
    """Yield every triangle from a validated binary or ASCII STL."""
    if len(raw) >= 84:
        count = struct.unpack_from("<I", raw, 80)[0]
        if count <= 2_000_000 and 84 + count * 50 == len(raw):
            for i in range(count):
                yield struct.unpack_from("<9f", raw, 84 + i * 50 + 12)
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
    index = {}
    vertices = []
    triangles = []
    for triangle in _iter_stl_triangles(raw):
        corners = []
        for j in (0, 3, 6):
            point = triangle[j:j + 3]
            if not all(math.isfinite(value) for value in point):
                raise ValueError(_t("err.stl_coords"))
            slot = index.get(point)
            if slot is None:
                slot = index[point] = len(vertices)
                vertices.append(point)
            corners.append(slot)
        # Degenerate facets would only produce mesh errors in Orca.
        if corners[0] != corners[1] and corners[1] != corners[2] and corners[0] != corners[2]:
            triangles.append(corners)
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
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
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
                    lo = [min(v[axis] for v in vertices) for axis in range(3)]
                    hi = [max(v[axis] for v in vertices) for axis in range(3)]
                    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
                    rotated = bool(item.get("rotated"))
                    chunks = [f'<object id="{object_id}" name={quoteattr(str(item["name"]))} type="model"><mesh><vertices>']
                    for x, y, z in vertices:
                        x -= cx
                        y -= cy
                        if rotated:
                            x, y = -y, x
                        chunks.append(f'<vertex x="{x:.5f}" y="{y:.5f}" z="{z - lo[2]:.5f}"/>')
                    chunks.append('</vertices><triangles>')
                    chunks.extend(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in triangles)
                    chunks.append('</triangles></mesh></object>')
                    emit(''.join(chunks))
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


def _replace_project_settings(path, settings):
    """Swap the configuration the CLI embeds (flattened "PlatePilot machine/process"
    presets) for the active Orca configuration, so opening keeps the selected presets."""
    temp = path + ".tmp"
    with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            if info.filename in ("Metadata/project_settings.config", "Metadata/slice_info.config"):
                continue
            target.writestr(info, source.read(info))
        target.writestr("Metadata/project_settings.config", settings)
    os.replace(temp, path)


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
        config["folder_grants"] = [p for p in config.get("folder_grants", []) if isinstance(p, str)]
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

    def _add_folder(self, config, folder):
        """Add a granted folder, skipping duplicates and folders already covered by another one."""
        if not os.path.isdir(folder):
            raise ValueError(_t("err.folder_missing", path=folder))
        key = _folder_key(folder)
        recursive = config.get("recursive", True)
        if key not in {_folder_key(p) for p in config["folder_grants"]}:
            config["folder_grants"].append(folder)
        for existing in config["folders"]:
            if _folder_key(existing) == key:
                self._save(config)
                self._publish_folders(config)
                self._notice(_t("msg.already_watched", path=existing))
                return
            if recursive and _is_inside(folder, existing):
                self._save(config)
                self._publish_folders(config)
                self._notice(_t("msg.inside_watched", path=existing))
                return
        covered = [p for p in config["folders"] if recursive and _is_inside(p, folder)]
        config["folders"] = [p for p in config["folders"] if p not in covered] + [folder]
        self._save(config)
        self._publish_folders(config)
        if covered:
            self._notice(_t("msg.replaced_nested", paths=", ".join(covered)))
        self._scan_start(config)

    def _visible_rows(self, state):
        rows = []
        hidden = state["config"].get("hidden", {})
        enabled_types = set(state["config"].get("model_types", ["stl", "step", "3mf"]))
        for row in state["rows"]:
            if hidden.get(row["path"]) == row.get("fingerprint"):
                continue
            counts = row.get("type_counts", {"stl": row.get("count", 0)})
            if not any(counts.get(t, 0) for t in enabled_types):
                continue
            visible_counts = {MODEL_TYPE_LABELS[t]: counts.get(t, 0) for t in MODEL_TYPE_LABELS if t in enabled_types and counts.get(t, 0)}
            item = {k: row.get(k) for k in ("kind", "name", "path", "count", "size", "root", "warning")}
            item["type_counts"] = visible_counts
            item["type_summary"] = _type_summary(counts)
            rows.append(item)
        self.post_message({"type": "archives", "archives": rows, "complete": state.get("complete", False)})

    def _scan_start(self, config):
        folders = list(config.get("folders", []))
        if not folders:
            self._scan_state = None
            self.post_message({"type": "archives", "archives": [], "complete": True})
            return
        grants = {_folder_key(p) for p in config.get("folder_grants", [])}
        for folder in folders:
            if _folder_key(folder) not in grants:
                self._scan_state = None
                self._awaiting_folder = folder
                self.post_message({"type": "folder_consent", "path": folder, "recursive": config.get("recursive", True)})
                return
        if not hasattr(self, "_seen_sources"):
            self._seen_sources = {}
        self._scan_state = {
            "rows": [], "iterator": iter(_iter_sources(folders, config.get("recursive", True))),
            "present_zip_paths": set(), "processed": 0, "batch_processed": 0,
            "batch_size": max(1, min(100, int(config.get("batch_size", 10)))),
            "config": config, "complete": False, "waiting": False, "pending": None,
        }
        self.post_message({"type": "scan_started"})

    def _scan_step(self):
        state = self._scan_state
        if not state or state.get("complete") or state.get("waiting"):
            return
        inspected = 0
        visited = 0
        hidden = state["config"].get("hidden", {})
        while visited < SCAN_DIRECTORIES_PER_TICK and inspected < SCAN_ARCHIVES_PER_TICK:
            row = state.get("pending")
            if row is None:
                try:
                    row = next(state["iterator"])
                except StopIteration:
                    state["complete"] = True
                    state["rows"] = [r for r in state["rows"] if r["kind"] != "zip" or r["path"] in state["present_zip_paths"]]
                    self._seen_sources = {p: r for p, r in self._seen_sources.items() if p in state["present_zip_paths"]}
                    self._visible_rows(state)
                    self.post_message({"type": "profile", "profile": _get_profile()})
                    return
                visited += 1
            else:
                state["pending"] = None

            if row["kind"] == "scan_tick":
                continue
            if row["kind"] == "folder":
                if hidden.get(row["path"]) != row.get("fingerprint", ""):
                    state["rows"].append(row)
                continue

            state["present_zip_paths"].add(row["path"])
            cached = self._seen_sources.get(row["path"])
            if cached and cached.get("fingerprint") == row["fingerprint"]:
                if hidden.get(row["path"]) != row.get("fingerprint"):
                    state["rows"].append(cached)
                continue

            if state["batch_processed"] >= state["batch_size"]:
                state["pending"] = row
                state["waiting"] = True
                self._visible_rows(state)
                self.post_message({"type": "batch_consent", "processed": state["processed"]})
                return

            try:
                checked = _inspect_zip(dict(row))
            except Exception as exc:
                checked = dict(row)
                checked.update({"count": 0, "warning": _t("warn.zip_unreadable", error=str(exc)[:100])})
            self._seen_sources[row["path"]] = checked
            if hidden.get(row["path"]) != row.get("fingerprint"):
                state["rows"].append(checked)
            state["processed"] += 1
            state["batch_processed"] += 1
            inspected += 1

        self._visible_rows(state)

    def _selected_root(self, path, folders):
        for folder in folders:
            if _safe_archive_path(path, folder):
                return os.path.realpath(folder)
        raise ValueError(_t("err.source_gone"))

    def _current_parts(self, config):
        source = getattr(self, "_selected_source", None)
        if not source:
            raise ValueError(_t("err.select_source"))
        path, kind, root = source
        if kind == "zip":
            return _read_archive(path, root)
        return _read_loose_folder(path, root)

    def _read_stl_blobs(self, source, parts, members):
        """Read the selected STL files again from the current source, with the safety checks."""
        by_member = {part.get("member"): part for part in parts if part.get("member")}
        blobs = {}
        if source[1] == "zip":
            if not _safe_archive_path(source[0], source[2]):
                raise ValueError(_t("err.archive_moved"))
            with zipfile.ZipFile(source[0], "r") as zf:
                for member in members:
                    if member not in by_member:
                        raise ValueError(_t("err.stl_left_archive"))
                    info = zf.getinfo(member)
                    if info.file_size > MAX_STL_BYTES:
                        raise ValueError(_t("err.stl_limit"))
                    blobs[member] = zf.read(info)
        else:
            for member in members:
                if member not in by_member or not _safe_archive_path(member, source[2]):
                    raise ValueError(_t("err.stl_left_folder"))
                with open(member, "rb") as stream:
                    blobs[member] = stream.read(MAX_STL_BYTES + 1)
                if len(blobs[member]) > MAX_STL_BYTES:
                    raise ValueError(_t("err.stl_limit"))
        return blobs

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
        run_dir = os.path.join(storage, "OrcaAutoArrange-" + time.strftime("%Y%m%d-%H%M%S"))
        input_dir = os.path.join(run_dir, "input")
        os.makedirs(input_dir, exist_ok=False)
        try:
            blobs = self._read_stl_blobs(source, all_parts, [part["member"] for part in parts])
            inputs = []
            used_names = set()
            for index, part in enumerate(parts, 1):
                inputs.append(self._write_native_input(input_dir, part, index, blobs[part["member"]], used_names))
            settings = _write_cli_settings(run_dir)
            project_settings = _project_settings()
            executable = _orca_executable()
            output = os.path.join(run_dir, "PlatePilot-AutoArrange.3mf")
            log_path = os.path.join(run_dir, "orca-cli.log")
            command = [executable, "--load-settings", ";".join(settings), "--arrange", "1",
                       "--export-3mf", output, *inputs]
            with open(log_path, "w", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=run_dir, stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._native_arrange_job = {
                "process": process, "run_dir": run_dir, "input_dir": input_dir, "output": output,
                "log": log_path, "count": len(inputs), "started": time.time(), "project_settings": project_settings,
            }
            self.post_message({"type": "native_arrange_started", "count": len(inputs)})
        except Exception:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

    @staticmethod
    def _write_native_input(input_dir, part, index, raw, used_names):
        stem = re.sub(r"[^\w.-]+", "_", os.path.splitext(os.path.basename(part["name"]))[0]).strip("._-")
        stem = stem[:64] or f"part-{index}"
        base = stem
        suffix = 2
        while stem.casefold() in used_names:
            stem = f"{base}-{suffix}"
            suffix += 1
        used_names.add(stem.casefold())
        path = os.path.join(input_dir, f"{index:03d}-{stem}.stl")
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
            _replace_project_settings(job["output"], job["project_settings"])
            plate_count = _count_plates(job["output"])
            _open_project(job["output"])
            message = _t("msg.arranged", parts=_plural("plan.parts", job["count"]), plates=_plural("plan.plates", max(1, plate_count)))
            self.post_message({"type": "native_arrange_progress", "done": True, "ok": True,
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
                self.post_message({"type": "init", "config": config, "profile": _get_profile(), "strings": _ui_strings(),
                                   "language": _LANG, "platform": platform, "version": VERSION})
            elif kind == "save_settings":
                incoming = data.get("config", {})
                for key in ("auto_refresh", "recursive"):
                    config[key] = bool(incoming.get(key, config.get(key)))
                config["poll_seconds"] = max(30, min(3600, int(incoming.get("poll_seconds", 30))))
                config["batch_size"] = max(1, min(100, int(incoming.get("batch_size", 10))))
                config["margin_mm"] = max(0.0, min(20.0, float(incoming.get("margin_mm", config.get("margin_mm", DEFAULT_MARGIN_MM)))))
                config["arrange_mode"] = "orca" if incoming.get("arrange_mode") == "orca" else "own"
                language = incoming.get("language", config.get("language", "auto"))
                config["language"] = language if language in ("auto",) + LANGUAGES else "auto"
                incoming_types = incoming.get("model_types", config.get("model_types", ["stl", "step", "3mf"]))
                if not isinstance(incoming_types, (list, tuple, set)):
                    incoming_types = ["stl", "step", "3mf"]
                config["model_types"] = [t for t in ("stl", "step", "3mf") if t in incoming_types]
                self._save(config)
                state = getattr(self, "_scan_state", None)
                if state:
                    state["config"] = config
                    self._visible_rows(state)
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
                # Choosing the folder in the system dialog is the explicit consent.
                self._add_folder(config, folder)
            elif kind == "request_folder_add":
                folder = _clean_folder_input(data.get("path", ""))
                if not os.path.isdir(folder):
                    raise ValueError(_t("err.folder_missing", path=folder))
                self.post_message({"type": "folder_selected", "path": folder})
                if _folder_key(folder) in {_folder_key(p) for p in config.get("folder_grants", [])}:
                    self._add_folder(config, folder)
                else:
                    self._awaiting_folder = folder
                    self.post_message({"type": "folder_consent", "path": folder, "recursive": config.get("recursive", True)})
            elif kind == "confirm_folder":
                self._add_folder(config, _clean_folder_input(data.get("path", "")))
            elif kind == "reject_folder":
                folder = _clean_folder_input(data.get("path", ""))
                config["folders"] = [p for p in config.get("folders", []) if _folder_key(p) != _folder_key(folder)]
                self._save(config)
                self._publish_folders(config)
                self._notice(_t("msg.folder_rejected"))
            elif kind == "remove_folder":
                target = data.get("path")
                folders = list(config.get("folders", []))
                if target is None:
                    index = int(data.get("index", -1))
                    target = folders[index] if 0 <= index < len(folders) else None
                if target:
                    removed = _folder_key(target)
                    config["folders"] = [p for p in folders if _folder_key(p) != removed]
                    config["folder_grants"] = [p for p in config.get("folder_grants", []) if _folder_key(p) != removed]
                    self._save(config)
                    self._publish_folders(config)
                    self._scan_start(config)
            elif kind == "scan":
                self._scan_start(config)
            elif kind == "scan_tick":
                if getattr(self, "_scan_state", None):
                    try:
                        self._scan_step()
                    except Exception as exc:
                        self._scan_state = None
                        self.post_message({"type": "error", "message": _t("err.scan_stopped", error=str(exc))})
            elif kind == "continue_batch":
                state = getattr(self, "_scan_state", None)
                if state and state.get("waiting"):
                    state["waiting"] = False
                    state["batch_processed"] = 0
            elif kind == "cancel_batch":
                state = getattr(self, "_scan_state", None)
                if state:
                    state["complete"] = True
                    state["waiting"] = False
                    state["iterator"] = iter(())
                    self._visible_rows(state)
                    self.post_message({"type": "profile", "profile": _get_profile()})
            elif kind == "open_model":
                path = str(data.get("path", ""))
                model_kind = str(data.get("kind", "zip"))
                root = self._selected_root(path, config.get("folders", []))
                self._selected_source = (path, model_kind, root)
                self._last_plan = None
                parts = _read_archive(path, root) if model_kind == "zip" else _read_loose_folder(path, root)
                self.post_message({"type": "archive", "path": path, "kind": model_kind, "name": os.path.basename(path), "parts": parts})
            elif kind == "hide_source":
                path = str(data.get("path", ""))
                row = getattr(self, "_seen_sources", {}).get(path)
                state = getattr(self, "_scan_state", None)
                if row is None and state:
                    row = next((r for r in state["rows"] if r["path"] == path), None)
                if row:
                    config.setdefault("hidden", {})[path] = row.get("fingerprint", "")
                    self._save(config)
                    if state:
                        state["rows"] = [r for r in state["rows"] if r["path"] != path]
                        self._visible_rows(state)
                    self._notice(_t("msg.source_hidden"))
            elif kind == "preview_part":
                source = getattr(self, "_selected_source", None)
                parts = self._current_parts(config)
                index = int(data.get("index", -1))
                if index < 0 or index >= len(parts):
                    raise ValueError(_t("err.part_missing"))
                member = parts[index].get("member")
                if not member or parts[index].get("filetype") != "stl":
                    raise ValueError(_t("err.no_preview"))
                if source[1] == "zip":
                    with zipfile.ZipFile(source[0], "r") as zf:
                        raw = zf.read(member)
                else:
                    if not _safe_archive_path(member, source[2]):
                        raise ValueError(_t("err.stl_outside"))
                    with open(member, "rb") as stream:
                        raw = stream.read(MAX_STL_BYTES + 1)
                info = _binary_stl_info(raw, include_preview=True)
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
                result = _pack(selected, profile["size"]["x"], profile["size"]["y"], margin)
                max_z = profile.get("height")
                too_tall = [p["name"] for p in selected if max_z and p["dims"][2] > max_z]
                exportable = bool(result["plates"]) and not result["problems"] and not too_tall
                self.post_message({"type": "plan", "plates": result["plates"], "bed": profile["size"], "profile": profile,
                                   "total": result["total"], "problems": result["problems"], "too_tall": too_tall,
                                   "exclusions": profile.get("exclusions"), "exportable": exportable})
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
