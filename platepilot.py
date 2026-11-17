# /// script
# requires-python = ">=3.9"
# [tool.orcaslicer.plugin]
# name = "PlatePilot"
# description = "Model library for STL bundles: preview parts on the active printer bed, arrange them on several plates and open the project in Prepare."
# author = "GNUGiminot"
# version = "0.10.2"
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
import webbrowser
from xml.sax.saxutils import escape, quoteattr
import xml.etree.ElementTree as ElementTree
import zipfile

import orca


VERSION = "0.10.2"
SUPPORT_URL = "https://t.me/GIMINOT"   # the author's Telegram: Support opens it in the browser
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
    "folders": [], "hidden": {}, "check_on_open": True, "recursive": True, "gpu_preview": True, "orca_orient": False,
    "margin_mm": DEFAULT_MARGIN_MM,
    "model_types": ["stl", "step", "3mf"], "arrange_mode": "own", "language": "auto", "theme": "orca",
}
LANGUAGES = ("en", "ru", "de", "fr", "es", "pt_BR", "it", "tr", "ja", "zh_CN")
# Languages in STRINGS' own columns; the others are in EXTRA_STRINGS, English where a key is missing.
TABLE_LANGUAGES = ("en", "ru")
# Interface themes besides Orca's own colours ("orca"); the page holds their colours.
THEMES = ("orca", "parchment", "terracotta", "sage", "blueprint", "dusk", "workshop", "night")

# Every user-facing string, as (English, Russian). Plural forms are separated by "|":
# English "one|other", Russian "one|few|many". The page receives the active language's column.
STRINGS = {
    # page: static text
    "app.tagline": ("Model library and plate planner", "Библиотека моделей и раскладка по столам"),
    "app.loading_profile": ("Loading the printer profile…", "Загружаю профиль принтера…"),
    "btn.refresh": ("Refresh", "Обновить"),
    "btn.folders": ("Settings", "Настройки"),
    "btn.add_files": ("Add files", "Добавить файлы"),
    "btn.support": ("Support", "Поддержать"),
    "btn.support_tip": ("Support PlatePilot with Telegram Stars: opens t.me/GIMINOT in your browser", "Поддержать PlatePilot звёздами Telegram: откроется t.me/GIMINOT в браузере"),
    "err.support_open": ("The browser did not open. The author's Telegram: {url}", "Браузер не открылся. Telegram автора: {url}"),
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
    "mode.orient": ("Turn parts (Orca auto-orient)", "Поворачивать детали (автоповорот Orca)"),
    "mode.orient_tip": ("Orca turns every part the way its Auto orient button would before arranging: less overhang, a steady base. Off: parts lie as their files have them, as their designers meant.",
                        "Перед расстановкой Orca поворачивает каждую деталь, как кнопка «Автоповорот»: меньше нависаний, устойчивое основание. Выключено — детали лежат как в файлах, как задумал автор."),
    "orient.checking": ("Checking how the parts lie… {done}/{total}", "Проверяю, как лежат детали… {done}/{total}"),
    "orient.done": ("{n} part could lie better: see Turn in the list|{n} parts could lie better: see Turn in the list",
                    "{n} деталь лучше повернуть: см. «Повернуть» в списке|{n} детали лучше повернуть: см. «Повернуть» в списке|{n} деталей лучше повернуть: см. «Повернуть» в списке"),
    "orient.advice": ("lies better turned: {pct}% less overhang", "лучше повернуть: нависаний меньше на {pct}%"),
    "orient.turned": ("turned: {pct}% less overhang", "повёрнута: нависаний меньше на {pct}%"),
    "btn.turn": ("Turn", "Повернуть"),
    "btn.unturn": ("Undo turn", "Вернуть"),
    "btn.turn_all": ("Turn all suggested ({n})", "Повернуть все предложенные ({n})"),
    "btn.turn_all_tip": ("Turn every part PlatePilot found lying worse than it could: upside down, on a narrow edge, with avoidable overhangs",
                         "Повернуть все детали, которые лежат хуже, чем могли бы: вверх ногами, на узкой грани, с лишними нависаниями"),
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
    "drawer.title": ("Settings", "Настройки"),
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
    "drawer.theme": ("Theme", "Тема"),
    "theme.orca": ("Same as Orca", "Как в Orca"),
    "theme.parchment": ("Parchment", "Пергамент"),
    "theme.terracotta": ("Terracotta", "Терракота"),
    "theme.sage": ("Sage", "Шалфей"),
    "theme.blueprint": ("Blueprint", "Чертёж"),
    "theme.dusk": ("Dusk", "Сумерки"),
    "theme.workshop": ("Workshop (dark)", "Мастерская (тёмная)"),
    "theme.night": ("Night shift (dark)", "Ночная смена (тёмная)"),
    "drawer.language": ("Language", "Язык"),
    "drawer.lang_auto": ("Same as Orca", "Как в Orca"),
    "modal.confirm": ("Confirm", "Подтверждение"),
    "modal.cancel": ("Cancel", "Отмена"),
    "modal.continue": ("Continue", "Продолжить"),
    # page: dynamic text
    "st.choose_folder": ("Drop ZIP or STL files here, or open Settings and choose a folder with models.", "Перетащите сюда ZIP или STL либо откройте «Настройки» и выберите папку с моделями."),
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
    "lib.add_folder": ("Drop ZIP or STL files here, or add a folder with models in Settings.", "Перетащите сюда ZIP или STL либо добавьте папку с моделями в «Настройках»."),
    "lib.new": ("New", "Новые"),
    "lib.added": ("Added", "Добавленные"),
    "lib.all_folders": ("All folders", "Все папки"),
    "lib.folder_tip": ("Show the sources of one folder only", "Показать источники только из одной папки"),
    "lib.other_show": ("More archives whose names don't look like models: {n}", "Ещё архивов, не похожих на модели по названию: {n}"),
    "lib.other_hide": ("Hide the archives whose names don't look like models", "Скрыть архивы, не похожие на модели"),
    "lib.other_note": ("PlatePilot judges by the name, because looking inside every archive would make Orca ask about each one. Open an archive to check it: with models it moves up, without them it disappears. Search looks through these too.",
                       "PlatePilot судит по названию: чтобы заглянуть в каждый архив, Orca спрашивала бы разрешение на каждый. Откройте архив, чтобы проверить: с моделями он поднимется в список, без них исчезнет. Поиск находит и эти архивы."),
    "lib.mark_seen": ("Mark all as seen", "Отметить все как просмотренные"),
    "lib.no_new": ("No new sources since the last check.", "Новых источников с прошлой проверки нет."),
    "drop.title": ("Drop to add to PlatePilot", "Отпустите, чтобы добавить в PlatePilot"),
    "drop.note": ("ZIP, STL, STEP, 3MF or a folder with them. The files are copied into PlatePilot's own folder, so Orca does not ask for permission. To watch a folder instead, add it in Settings or paste its path with Ctrl+V.",
                  "ZIP, STL, STEP, 3MF или папка с ними. Файлы копируются в собственную папку PlatePilot, поэтому Orca не спрашивает разрешения. Чтобы отслеживать папку, добавьте её в «Настройках» или вставьте её путь через Ctrl+V."),
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
    "plan.view3d": ("3D", "3D"),
    "plan.view3d_all": ("Plates in 3D", "Столы в 3D"),
    "plan.view3d_all_tip": ("See the plates in 3D in the preview, with every part as the project will have it",
                            "Показать столы в 3D в окне просмотра: детали стоят так, как будут в проекте"),
    "view.plate_button": ("Plate {n} of {total} in 3D", "Стол {n} из {total} в 3D"),
    "view.plate_prev": ("Previous plate", "Предыдущий стол"),
    "view.plate_next": ("Next plate", "Следующий стол"),
    "plan.view3d_tip": ("See this plate in 3D in the preview", "Показать этот стол в 3D в окне просмотра"),
    "view.plate_meta": ("{parts} · bed {bed} · simplified", "{parts} · стол {bed} · упрощённо"),
    "st.plate_shown": ("{name} in 3D: drag to turn it.", "{name} в 3D: потяните, чтобы повернуть."),
    "st.plate_loading": ("{name} in 3D: part {i} of {n}…", "{name} в 3D: деталь {i} из {n}…"),
    "basket.add": ("To basket", "В корзину"),
    "basket.add_tip": ("Put the ticked parts in the basket, where parts from several sources are laid out together",
                       "Положить отмеченные детали в корзину: в ней детали из разных источников раскладываются вместе"),
    "basket.show": ("Basket · {n}", "Корзина · {n}"),
    "basket.back": ("Back to the source", "К деталям источника"),
    "basket.empty": ("The basket is empty. Tick parts in any source and press To basket; parts from several sources are laid out together here.",
                     "Корзина пуста. Отметьте детали в любом источнике и нажмите «В корзину»: здесь детали из разных источников раскладываются вместе."),
    "basket.from": ("from {name}", "из «{name}»"),
    "basket.copies": ("Copies of this part", "Сколько копий этой детали"),
    "basket.remove": ("Take out of the basket", "Убрать из корзины"),
    "basket.clear": ("Empty the basket", "Очистить корзину"),
    "basket.added": ("In the basket: {n}", "В корзине: {n}"),
    "basket.title": ("Basket", "Корзина"),
    "basket.and_more": ("{name} and {n} more", "{name} и ещё {n}"),
    "err.basket_empty": ("the basket is empty", "корзина пуста"),
    "basket.turned": ("turned", "повёрнута"),
    "err.basket_gone": ("\"{name}\" from the basket is no longer where it was (moved, changed or deleted); take it out and add it again",
                        "«{name}» из корзины уже не на месте (перемещён, изменён или удалён): уберите его из корзины и добавьте заново"),
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
    "folders.other": ("archives that don't look like models: {n}", "не похожих на модели архивов: {n}"),
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
    "err.3mf_transform": ("an object's placement in the file is broken", "у объекта в файле испорчено положение"),
    "err.3mf_nesting": ("objects are nested too deep", "объекты вложены слишком глубоко"),
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

# The interface in the languages besides English and Russian: language -> key -> text. A key missing
# here shows in English. Names of Orca's own tabs and buttons follow Orca's translations.
EXTRA_STRINGS = {
    "de": {
        "btn.support": "Unterstützen",
        "btn.support_tip": "PlatePilot mit Telegram Stars unterstützen: öffnet t.me/GIMINOT im Browser",
        "err.support_open": "Der Browser hat sich nicht geöffnet. Telegram des Autors: {url}",
        "err.3mf_transform": "die Lage eines Objekts in der Datei ist beschädigt",
        "err.3mf_nesting": "Objekte sind zu tief verschachtelt",
        "app.tagline": "Modellbibliothek und Plattenplaner",
        "app.loading_profile": "Druckerprofil wird geladen…",
        "btn.refresh": "Aktualisieren",
        "btn.folders": "Einstellungen",
        "btn.add_files": "Dateien hinzufügen",
        "btn.add_files_tip": "ZIP-, STL-, STEP- oder 3MF-Dateien hinzufügen. Sie werden in PlatePilot kopiert, daher fragt Orca nicht nach einer Berechtigung.",
        "lib.title": "Bibliothek",
        "lib.search": "Nach Namen suchen",
        "lib.all": "Alle",
        "lib.folders": "Ordner",
        "parts.title": "Teile",
        "parts.all": "Alle",
        "parts.none": "Keine",
        "view.reset": "Ansicht zurücksetzen",
        "view.hint": "Ziehen zum Drehen · Mausrad zum Zoomen · Doppelklick zum Zurücksetzen",
        "view.empty": "Wählen Sie ein STL-Teil, um es auf dem Druckbett zu sehen",
        "mode.own": "PlatePilot-Anordnung",
        "mode.orca": "Orca-Anordnung",
        "mode.gap": "Abstand",
        "mode.orient": "Teile drehen (Orca-Ausrichtung)",
        "mode.orient_tip": "Orca dreht jedes Teil vor dem Anordnen so, wie es „Automatisch ausrichten“ tun würde: weniger Überhang, eine stabile Auflage. Aus: Die Teile liegen so, wie sie in ihren Dateien liegen, wie ihre Designer es gedacht haben.",
        "orient.checking": "Prüfe, wie die Teile liegen… {done}/{total}",
        "orient.done": "{n} Teil könnte besser liegen: siehe „Drehen“ in der Liste|{n} Teile könnten besser liegen: siehe „Drehen“ in der Liste",
        "orient.advice": "liegt gedreht besser: {pct} % weniger Überhang",
        "orient.turned": "gedreht: {pct} % weniger Überhang",
        "btn.turn": "Drehen",
        "btn.unturn": "Zurückdrehen",
        "btn.turn_all": "Alle vorgeschlagenen drehen ({n})",
        "btn.turn_all_tip": "Jedes Teil drehen, das PlatePilot schlechter liegend gefunden hat, als es könnte: kopfüber, auf einer schmalen Kante, mit vermeidbaren Überhängen",
        "mode.plates": "Platten",
        "mode.plates_auto": "auto",
        "mode.plates_tip": "Auf wie viele Platten die Teile verteilt werden. Leer: so wenige, wie nötig. Mehr als das verteilt sie gleichmäßig, auch wenn sie auf eine passen würden.",
        "unit.mm": "mm",
        "btn.plan": "Berechnen",
        "btn.plan_smart": "Smarte Anordnung",
        "btn.plan_smart_tip": "Beta: Die Teile jeder Platte werden in der Mitte gruppiert, kleine am nächsten zur Mitte, weg von den Bettkanten und gleichmäßig verteilt; die Platten werden gleichmäßig gefüllt",
        "btn.send": "In Vorbereiten öffnen",
        "btn.arrange": "In Orca anordnen",
        "mode.own_note": "PlatePilot packt die Teile nach ihrer Grundfläche auf das Bett des aktiven Druckers, zeigt die Platten und öffnet das Projekt in „Vorbereiten“.",
        "mode.orca_note": "Die Teile gehen an Orcas eigene Anordnung: Sie nutzt die Umrisse der Teile und die aktiven Drucker- und Prozessprofile, fügt bei Bedarf Platten hinzu und öffnet das Projekt.",
        "drawer.title": "Einstellungen",
        "drawer.close": "Schließen",
        "drawer.folders": "Überwachte Ordner",
        "drawer.folders_note": "PlatePilot findet ZIP-Archive und Ordner mit Modelldateien anhand ihrer Namen. Eine Datei wird erst gelesen, wenn Sie sie öffnen.",
        "drawer.pick": "Ordner wählen…",
        "drawer.paste": "Oder einen Ordnerpfad einfügen (Anführungszeichen wie bei „Als Pfad kopieren“ sind in Ordnung):",
        "drawer.paste_only": "Den vollständigen Pfad eines Ordners mit Modellen einfügen:",
        "drawer.path_ph": "Vollständiger Ordnerpfad",
        "drawer.add": "Hinzufügen",
        "drawer.formats": "Formate",
        "drawer.formats_note": "Quellen ohne die gewählten Formate werden ausgeblendet. Vorschau und Anordnung funktionieren mit STL.",
        "drawer.scan": "Durchsuchen",
        "drawer.recursive": "Unterordner durchsuchen",
        "drawer.check_on_open": "Ordner beim Öffnen von PlatePilot prüfen",
        "drawer.audit_note": "Orca fragt einmal, bevor PlatePilot in einen überwachten Ordner schaut, und einmal für jedes Archiv oder jeden Modellordner, den Sie öffnen. In PlatePilot gezogene Dateien brauchen keine Berechtigung.",
        "drawer.preview": "3D-Vorschau",
        "drawer.gpu_preview": "Mit der Grafikkarte zeichnen (WebGL)",
        "drawer.gpu_note": "Zeigt Teile mit bis zu 250 000 Facetten vollständig und dreht sie flüssig. Wenn die Vorschau falsch aussieht, schalten Sie es aus: Der einfache Modus zeichnet bis zu 30 000 Facetten.",
        "drawer.theme": "Design",
        "theme.orca": "Wie in Orca",
        "theme.parchment": "Pergament",
        "theme.terracotta": "Terrakotta",
        "theme.sage": "Salbei",
        "theme.blueprint": "Blaupause",
        "theme.dusk": "Dämmerung",
        "theme.workshop": "Werkstatt (dunkel)",
        "theme.night": "Nachtschicht (dunkel)",
        "drawer.language": "Sprache",
        "drawer.lang_auto": "Wie in Orca",
        "modal.confirm": "Bestätigen",
        "modal.cancel": "Abbrechen",
        "modal.continue": "Fortsetzen",
        "st.choose_folder": "ZIP- oder STL-Dateien hierher ziehen oder die Einstellungen öffnen und einen Ordner mit Modellen wählen.",
        "st.press_refresh": "„Aktualisieren“ drücken, um die Ordner zu durchsuchen.",
        "st.scanning": "Ordner werden durchsucht…",
        "st.scanned": "Ordner geprüft · {sources}",
        "st.scanned_new": "Ordner geprüft · {sources} · {n} neu",
        "st.importing": "Füge {name} hinzu · {i} von {n} · {pct} %",
        "st.import_none": "Nichts hinzuzufügen: ZIP-, STL-, STEP- oder 3MF-Dateien oder einen Ordner damit hierher ziehen.",
        "st.import_skipped": " Andere Dateien übersprungen: {n}.",
        "st.opening": "Wird geöffnet…",
        "st.opening_picker": "Ordnerauswahl wird geöffnet…",
        "st.opened_source": "„{name}“ geöffnet.",
        "st.plan_ready": "Die Anordnung ist fertig. Öffnen Sie sie in „Vorbereiten“.",
        "st.plan_problems": "Einige Teile passen nicht auf das Bett. Ändern Sie die Auswahl.",
        "st.building": "Projekt für Orca wird erstellt…",
        "st.project_opened": "Das Projekt wird in Orca geöffnet. Wenn „Vorbereiten“ schon Modelle enthält, fragt Orca, was zu tun ist: „Als Projekt öffnen“ behält die Platten, „Nur Geometrie importieren“ fügt die Teile zum aktuellen Projekt hinzu.",
        "st.project_saved": "3MF gespeichert: {path}. Öffnen Sie es mit Datei > Projekt öffnen. {error}",
        "st.arranging": "Orca ordnet {n} Teile an und fügt bei Bedarf Platten hinzu…",
        "st.arrange_done": "Anordnung abgeschlossen.",
        "st.plugin_error": "Plugin-Fehler.",
        "st.removed_source": "Die Quelle wurde von der Festplatte gelöscht oder ausgeblendet.",
        "printer.unreadable": "Profil nicht gelesen",
        "printer.zones": "Zonen",
        "printer.zones_tip": "Das Druckerprofil hat ausgeschlossene Bettbereiche. Prüfen Sie die Anordnung in Orca.",
        "lib.nothing_found": "Nichts passt zum Filter.",
        "lib.press_refresh": "„Aktualisieren“ drücken, um Modelle in den Ordnern zu finden.",
        "lib.add_folder": "ZIP- oder STL-Dateien hierher ziehen oder in den Einstellungen einen Ordner mit Modellen hinzufügen.",
        "lib.new": "Neu",
        "lib.added": "Hinzugefügt",
        "lib.all_folders": "Alle Ordner",
        "lib.folder_tip": "Nur die Quellen eines Ordners zeigen",
        "lib.other_show": "{n} weitere Archive, deren Namen nicht nach Modellen aussehen",
        "lib.other_hide": "Archive ausblenden, deren Namen nicht nach Modellen aussehen",
        "lib.other_note": "PlatePilot urteilt nach dem Namen, weil Orca sonst für jedes Archiv einzeln fragen würde. Öffnen Sie ein Archiv, um es zu prüfen: mit Modellen rückt es nach oben, ohne verschwindet es. Die Suche durchsucht auch diese.",
        "lib.mark_seen": "Alle als gesehen markieren",
        "lib.no_new": "Keine neuen Quellen seit der letzten Prüfung.",
        "drop.title": "Ablegen, um zu PlatePilot hinzuzufügen",
        "drop.note": "ZIP, STL, STEP, 3MF oder ein Ordner damit. Die Dateien werden in den eigenen Ordner von PlatePilot kopiert, daher fragt Orca nicht nach einer Berechtigung. Um stattdessen einen Ordner zu überwachen, fügen Sie ihn in den Einstellungen hinzu oder fügen Sie seinen Pfad mit Strg+V ein.",
        "src.choose": "Quelle wählen",
        "src.choose_note": "Wählen Sie ein Archiv oder einen Ordner in der Bibliothek. PlatePilot zeigt seine Teile und eine Vorschau auf dem Bett.",
        "src.summary": "{n} Modelldateien · {stl} STL · {path}",
        "src.hide": "Ausblenden",
        "src.hide_tip": "Aus der Liste ausblenden. Die Datei bleibt auf der Festplatte.",
        "src.new": "neu",
        "src.added": "hinzugefügt",
        "src.unopened": "noch nicht geöffnet",
        "src.delete": "Löschen",
        "src.delete_tip": "Die zu PlatePilot hinzugefügte Kopie löschen",
        "delete.title": "Aus PlatePilot löschen?",
        "delete.body": "Die Kopie von „{name}“ im Ordner von PlatePilot wird gelöscht. Die Dateien, aus denen Sie gezogen oder eingefügt haben, bleiben unberührt.",
        "parts.meta": "{dims} mm · {facets} Facetten",
        "parts.catalog_only": "{type} · nur im Katalog",
        "parts.no_files": "Die Quelle enthält keine Dateien der gewählten Formate.",
        "parts.placeholder": "Hier erscheinen die Teile.",
        "view.meta": "{dims} mm · {facets} Facetten",
        "view.shown": " · {n} angezeigt",
        "plan.parts": "{n} Teil|{n} Teile",
        "plan.plates": "{n} Platte|{n} Platten",
        "plan.not_fit": "passt nicht: {name}",
        "plan.at_least": "braucht mindestens {plates}",
        "plan.at_most": "höchstens eine Platte pro Teil",
        "plan.too_tall": "höher als Z: {name}",
        "plan.zones": "ausgeschlossene Bereiche in Orca prüfen",
        "plan.smart": "smarte Anordnung β",
        "plan.plate": "Platte {n}",
        "plan.view3d": "3D",
        "plan.view3d_all": "Platten in 3D",
        "plan.view3d_all_tip": "Die Platten in 3D in der Vorschau zeigen, jedes Teil so, wie es im Projekt sein wird",
        "view.plate_button": "Platte {n} von {total} in 3D",
        "view.plate_prev": "Vorherige Platte",
        "view.plate_next": "Nächste Platte",
        "plan.view3d_tip": "Diese Platte in 3D in der Vorschau zeigen",
        "view.plate_meta": "{parts} · Bett {bed} · vereinfacht",
        "st.plate_shown": "{name} in 3D: zum Drehen ziehen.",
        "st.plate_loading": "{name} in 3D: Teil {i} von {n}…",
        "basket.add": "In den Korb",
        "basket.add_tip": "Die markierten Teile in den Korb legen, in dem Teile aus mehreren Quellen gemeinsam angeordnet werden",
        "basket.show": "Korb · {n}",
        "basket.back": "Zurück zur Quelle",
        "basket.empty": "Der Korb ist leer. Markieren Sie Teile in einer beliebigen Quelle und drücken Sie „In den Korb“; hier werden Teile aus mehreren Quellen gemeinsam angeordnet.",
        "basket.from": "aus {name}",
        "basket.copies": "Kopien dieses Teils",
        "basket.remove": "Aus dem Korb nehmen",
        "basket.clear": "Korb leeren",
        "basket.added": "Im Korb: {n}",
        "basket.title": "Korb",
        "basket.and_more": "{name} und {n} weitere",
        "err.basket_empty": "der Korb ist leer",
        "basket.turned": "gedreht",
        "err.basket_gone": "„{name}“ aus dem Korb ist nicht mehr dort, wo es war (verschoben, geändert oder gelöscht); nehmen Sie es heraus und fügen Sie es erneut hinzu",
        "plan.plate_meta": "{parts} · {fill} %",
        "plan.rotated": " · um {n}° gedreht",
        "plan.zoom_hint": "Auf eine Platte klicken, um sie groß zu sehen",
        "plan.zoom_title": "Platte {n} von {total}",
        "plan.prev": "Vorherige Platte",
        "plan.next": "Nächste Platte",
        "plan.close": "Schließen",
        "folders.none": "Noch keine Ordner.",
        "folders.remove": "Nicht mehr überwachen",
        "folders.sources": "{n} Quelle|{n} Quellen",
        "folders.stats": "{sources} · geprüft {time}",
        "folders.new": "{n} neu",
        "folders.other": "{n} Archive, die nicht nach Modellen aussehen",
        "folders.empty": "{n} ohne Modelle ausgeblendet",
        "folders.never": "Noch nicht geprüft",
        "folders.missing": "Ordner nicht gefunden",
        "folders.skipped": "{n} Unterordner übersprungen: Orca kann sie für Plugins nicht öffnen",
        "folders.unreadable": "Orca kann diesen Ordner für Plugins nicht öffnen: Sein Pfad enthält nicht-lateinische Buchstaben oder einen Ordner namens config, cert oder secret",
        "folders.truncated": "nur die ersten {n} werden gezeigt",
        "folders.added": "Zu PlatePilot hinzugefügt",
        "folders.added_note": "Gezogene oder eingefügte Dateien werden hierher kopiert.",
        "err.path_empty": "einen Ordnerpfad eingeben",
        "err.path_relative": "den vollständigen Ordnerpfad eingeben, zum Beispiel {example}",
        "err.folder_missing": "Ordner nicht gefunden: {path}",
        "err.not_filesystem": "das gewählte Element ist kein Ordner auf der Festplatte (zum Beispiel „Bibliotheken“); wählen Sie einen normalen Ordner",
        "err.picker_windows": "Die Ordnerauswahl gibt es unter Windows; fügen Sie stattdessen den Pfad ein.",
        "picker.title": "Ordner mit Modellen für PlatePilot",
        "picker.ok": "Ordner überwachen",
        "err.no_main_window": "das Orca-Hauptfenster wurde nicht gefunden",
        "err.open_unsupported": "das Projekt in diesem Orca-Fenster zu öffnen geht nur unter Windows",
        "err.no_executable": "die laufende OrcaSlicer-Programmdatei wurde nicht gefunden",
        "err.no_settings": "die Einstellungen des aktiven Orca-Profils konnten nicht gelesen werden",
        "err.no_printer_preset": "in Orca ist kein Druckerprofil gewählt",
        "err.no_process_preset": "in Orca ist kein Prozessprofil gewählt",
        "warn.zip_empty": "Kein STL, STEP oder 3MF im Archiv",
        "warn.zip_unreadable": "Archiv nicht gelesen: {error}",
        "err.archive_outside": "das Archiv muss in einem überwachten Ordner liegen",
        "err.folder_outside": "der Modellordner muss in einem überwachten Ordner liegen",
        "warn.step_open": "STEP: keine Vorschau oder Anordnung; öffnen Sie es in Orca, das es selbst umwandelt",
        "warn.3mf_error": "3MF konnte nicht gelesen werden: {error}",
        "warn.3mf_empty": "3MF enthält kein Modell",
        "btn.open_in_orca": "In Orca öffnen",
        "msg.step_sent": "{name} an Orca gesendet: Orca öffnet es in „Vorbereiten“.",
        "warn.unsafe_name": "Name mit nicht-lateinischen Buchstaben: Diese Orca-Version kann ihn für Plugins nicht öffnen (OrcaSlicer 2.5.0 kann es); ziehen Sie die Datei in PlatePilot",
        "err.unsafe_path": "{path}: Orca lässt Plugins keine Pfade mit einem Ordner namens config, cert oder secret öffnen, und Versionen vor OrcaSlicer 2.5.0 keine mit nicht-lateinischen Buchstaben. Ziehen Sie die Dateien in PlatePilot oder benennen Sie den Ordner um.",
        "st.no_answer": "Das Plugin hat nicht geantwortet. „Aktualisieren“ drücken, um es erneut zu versuchen.",
        "st.scan_interrupted": "Die letzte Ordnerprüfung wurde nicht beendet (Orca wurde geschlossen), daher wurde sie nicht automatisch wiederholt. „Aktualisieren“ drücken, um die Ordner zu prüfen.",
        "warn.too_big": "Datei größer als {mb} MB",
        "err.total_archive": "die STL-Dateien im Archiv überschreiten insgesamt {mb} MB",
        "err.total_folder": "die STL-Dateien im Ordner überschreiten insgesamt {mb} MB",
        "warn.empty_model": "leeres Modell",
        "warn.stl_error": "STL-Fehler: {error}",
        "err.stl_short": "STL-Datei ist zu kurz",
        "err.stl_coords": "die STL hat ungültige Koordinaten",
        "err.stl_vertices": "die Eckpunkte der STL konnten nicht gelesen werden",
        "err.stl_facets_limit": "die STL überschreitet 2 Millionen Facetten",
        "err.stl_geometry": "die STL hat keine gültige Geometrie",
        "err.stl_no_triangles": "die STL hat keine Dreiecke",
        "err.gap_too_big": "der Abstand ist größer als das Bett",
        "err.plan_first": "zuerst die Anordnung berechnen",
        "err.reread": "die STL konnte nicht erneut gelesen werden: {name}",
        "err.facets_limit": "ein Projekt ist auf {n} Facetten begrenzt; wählen Sie weniger Teile oder nutzen Sie die Orca-Anordnung",
        "msg.already_watched": "Dieser Ordner wird bereits überwacht: {path}",
        "msg.inside_watched": "Der Ordner liegt bereits im überwachten Ordner {path} (Unterordnersuche ist an).",
        "msg.replaced_nested": "Ordner hinzugefügt; diese verschachtelten Ordner werden nicht mehr einzeln gebraucht: {paths}",
        "err.scan_stopped": "Durchsuchen abgebrochen: {error}",
        "err.source_gone": "die Quelle liegt nicht mehr in einem überwachten Ordner",
        "err.select_source": "zuerst ein ZIP oder einen Ordner mit STL-Dateien wählen",
        "err.archive_moved": "das Archiv liegt nicht mehr in einem überwachten Ordner",
        "err.stl_left_archive": "die STL ist nicht mehr im gewählten Archiv",
        "err.stl_left_folder": "die STL ist nicht mehr im gewählten Ordner",
        "err.stl_limit": "die STL überschreitet die Grenze von {mb} MB",
        "err.arrange_running": "die Orca-Anordnung läuft bereits",
        "err.select_stl": "mindestens ein STL-Teil wählen",
        "err.too_many_parts": "ein Durchlauf kann höchstens {n} STL-Dateien anordnen",
        "err.no_bed": "der druckbare Bereich des aktiven Druckers konnte nicht gelesen werden",
        "err.cli_failed": "Die Orca-Kommandozeile hat kein Projekt erstellt. {detail}",
        "err.cli_exit": "Exitcode: {code}",
        "msg.arranged": "Orca hat {parts} auf {plates} angeordnet und öffnet das Projekt. Wenn „Vorbereiten“ schon Modelle enthält, wählen Sie „Als Projekt öffnen“, um die Platten zu behalten.",
        "msg.arranged_partly": "Orca konnte {n} von {parts} nicht auf den Platten unterbringen, auch nicht gedreht: Sie liegen neben den Platten. Prüfen Sie ihre Größe im Vergleich zum Bett oder nutzen Sie die PlatePilot-Anordnung, um zu sehen, welche es sind.",
        "err.arrange_failed": "Die Anordnung wurde nicht beendet: {error}",
        "msg.picker_cancelled": "Ordnerauswahl abgebrochen.",
        "msg.source_hidden": "Die Quelle ist aus der Liste ausgeblendet; die Datei auf der Festplatte bleibt erhalten.",
        "msg.deleted": "Aus PlatePilot gelöscht.",
        "msg.imported": "Zu PlatePilot hinzugefügt: {files}.",
        "import.files": "{n} Datei|{n} Dateien",
        "import.group": "Dateien {time}",
        "err.import_type": "{name}: nur ZIP-, STL-, STEP- und 3MF-Dateien können hinzugefügt werden",
        "err.import_size": "{name} ist größer als {mb} MB",
        "err.import_state": "die Dateiübertragung wurde unterbrochen; fügen Sie die Dateien erneut hinzu",
        "err.import_path": "der Name von {name} ist zu lang für den Ordner von PlatePilot",
        "err.no_storage": "der Datenordner von PlatePilot ist nicht verfügbar: {error}",
        "err.not_added": "nur zu PlatePilot hinzugefügte Quellen können gelöscht werden",
        "err.paste_path": "{path} ist weder ein Ordner noch eine ZIP-, STL-, STEP- oder 3MF-Datei",
        "err.part_missing": "Teil nicht gefunden",
        "err.no_preview": "für dieses Teil gibt es keine Vorschaudaten",
        "err.stl_outside": "die STL liegt außerhalb des überwachten Ordners",
        "err.no_printable_area": "printable_area des aktiven Profils konnte nicht gelesen werden. Laden Sie das Druckerprofil neu und versuchen Sie es erneut.",
        "err.plan_source": "zuerst die Anordnung der gewählten Quelle berechnen",
        "err.plan_not_exportable": "zuerst die Teile korrigieren, die nicht auf das Bett passen oder höher als der Drucker sind",
        "err.printer_changed": "der aktive Drucker hat sich nach der Berechnung geändert; berechnen Sie die Anordnung erneut",
        "err.stl_changed": "eine STL hat sich nach der Berechnung geändert; berechnen Sie die Anordnung erneut"
    },
    "fr": {
        "btn.support": "Soutenir",
        "btn.support_tip": "Soutenir PlatePilot avec des étoiles Telegram : ouvre t.me/GIMINOT dans le navigateur",
        "err.support_open": "Le navigateur ne s’est pas ouvert. Telegram de l’auteur : {url}",
        "err.3mf_transform": "la position d’un objet dans le fichier est corrompue",
        "err.3mf_nesting": "les objets sont imbriqués trop profondément",
        "app.tagline": "Bibliothèque de modèles et planificateur de plaques",
        "app.loading_profile": "Chargement du profil d’imprimante…",
        "btn.refresh": "Actualiser",
        "btn.folders": "Paramètres",
        "btn.add_files": "Ajouter des fichiers",
        "btn.add_files_tip": "Ajouter des fichiers ZIP, STL, STEP ou 3MF. Ils sont copiés dans PlatePilot, donc Orca ne demande pas d’autorisation.",
        "lib.title": "Bibliothèque",
        "lib.search": "Rechercher par nom",
        "lib.all": "Tout",
        "lib.folders": "Dossiers",
        "parts.title": "Pièces",
        "parts.all": "Tout",
        "parts.none": "Aucune",
        "view.reset": "Réinitialiser la vue",
        "view.hint": "Glisser pour tourner · molette pour zoomer · double-clic pour réinitialiser",
        "view.empty": "Sélectionnez une pièce STL pour la voir sur le plateau",
        "mode.own": "Disposition PlatePilot",
        "mode.orca": "Disposition Orca",
        "mode.gap": "Écart",
        "mode.orient": "Tourner les pièces (orientation Orca)",
        "mode.orient_tip": "Avant de disposer, Orca tourne chaque pièce comme le ferait son « Orientation automatique » : moins de surplomb, une base stable. Désactivé : les pièces restent posées comme dans leurs fichiers, comme leurs concepteurs l’ont voulu.",
        "orient.checking": "Vérification de la pose des pièces… {done}/{total}",
        "orient.done": "{n} pièce pourrait être mieux posée : voir « Tourner » dans la liste|{n} pièces pourraient être mieux posées : voir « Tourner » dans la liste",
        "orient.advice": "mieux posée tournée : {pct} % de surplomb en moins",
        "orient.turned": "tournée : {pct} % de surplomb en moins",
        "btn.turn": "Tourner",
        "btn.unturn": "Annuler la rotation",
        "btn.turn_all": "Tourner toutes les suggestions ({n})",
        "btn.turn_all_tip": "Tourner chaque pièce que PlatePilot a trouvée mal posée : à l’envers, sur une arête étroite, avec des surplombs évitables",
        "mode.plates": "Plaques",
        "mode.plates_auto": "auto",
        "mode.plates_tip": "Sur combien de plaques répartir les pièces. Vide : le moins possible. Au-delà, elles sont réparties uniformément, même si elles tiendraient sur une seule.",
        "unit.mm": "mm",
        "btn.plan": "Calculer",
        "btn.plan_smart": "Disposition intelligente",
        "btn.plan_smart_tip": "Bêta : les pièces de chaque plaque sont regroupées au milieu, les petites au plus près du centre, loin des bords du plateau et espacées régulièrement ; les plaques sont remplies uniformément",
        "btn.send": "Ouvrir dans Préparer",
        "btn.arrange": "Disposer dans Orca",
        "mode.own_note": "PlatePilot range les pièces selon leur emprise sur le plateau de l’imprimante active, montre les plaques et ouvre le projet dans « Préparer ».",
        "mode.orca_note": "Les pièces passent à la disposition d’Orca : elle utilise les contours des pièces et les profils d’imprimante et de traitement actifs, ajoute des plaques si besoin et ouvre le projet.",
        "drawer.title": "Paramètres",
        "drawer.close": "Fermer",
        "drawer.folders": "Dossiers surveillés",
        "drawer.folders_note": "PlatePilot trouve les archives ZIP et les dossiers de fichiers de modèles d’après leur nom. Un fichier n’est lu que lorsque vous l’ouvrez.",
        "drawer.pick": "Choisir un dossier…",
        "drawer.paste": "Ou collez le chemin d’un dossier (les guillemets de « Copier en tant que chemin d’accès » sont acceptés) :",
        "drawer.paste_only": "Collez le chemin complet d’un dossier de modèles :",
        "drawer.path_ph": "Chemin complet du dossier",
        "drawer.add": "Ajouter",
        "drawer.formats": "Formats",
        "drawer.formats_note": "Les sources sans les formats choisis sont masquées. L’aperçu et la disposition fonctionnent avec le STL.",
        "drawer.scan": "Analyse",
        "drawer.recursive": "Chercher dans les sous-dossiers",
        "drawer.check_on_open": "Vérifier les dossiers à l’ouverture de PlatePilot",
        "drawer.audit_note": "Orca demande une fois avant que PlatePilot regarde dans un dossier surveillé, et une fois pour chaque archive ou dossier de modèles que vous ouvrez. Les fichiers déposés dans PlatePilot ne demandent aucune autorisation.",
        "drawer.preview": "Aperçu 3D",
        "drawer.gpu_preview": "Dessiner avec la carte graphique (WebGL)",
        "drawer.gpu_note": "Affiche entièrement les pièces jusqu’à 250 000 facettes et les tourne en douceur. Si l’aperçu semble faux, désactivez-le : le mode simple dessine jusqu’à 30 000 facettes.",
        "drawer.theme": "Thème",
        "theme.orca": "Comme dans Orca",
        "theme.parchment": "Parchemin",
        "theme.terracotta": "Terre cuite",
        "theme.sage": "Sauge",
        "theme.blueprint": "Plan bleu",
        "theme.dusk": "Crépuscule",
        "theme.workshop": "Atelier (sombre)",
        "theme.night": "Équipe de nuit (sombre)",
        "drawer.language": "Langue",
        "drawer.lang_auto": "Comme dans Orca",
        "modal.confirm": "Confirmer",
        "modal.cancel": "Annuler",
        "modal.continue": "Continuer",
        "st.choose_folder": "Déposez des fichiers ZIP ou STL ici, ou ouvrez les Paramètres et choisissez un dossier de modèles.",
        "st.press_refresh": "Appuyez sur « Actualiser » pour analyser les dossiers.",
        "st.scanning": "Analyse des dossiers…",
        "st.scanned": "Dossiers vérifiés · {sources}",
        "st.scanned_new": "Dossiers vérifiés · {sources} · {n} nouveaux",
        "st.importing": "Ajout de {name} · {i} sur {n} · {pct} %",
        "st.import_none": "Rien à ajouter : déposez des fichiers ZIP, STL, STEP ou 3MF ou un dossier qui en contient.",
        "st.import_skipped": " Autres fichiers ignorés : {n}.",
        "st.opening": "Ouverture…",
        "st.opening_picker": "Ouverture du sélecteur de dossier…",
        "st.opened_source": "« {name} » ouvert.",
        "st.plan_ready": "La disposition est prête. Ouvrez-la dans « Préparer ».",
        "st.plan_problems": "Certaines pièces ne tiennent pas sur le plateau. Modifiez la sélection.",
        "st.building": "Création du projet pour Orca…",
        "st.project_opened": "Le projet s’ouvre dans Orca. Si « Préparer » contient déjà des modèles, Orca demande quoi faire : « Ouvrir en tant que projet » garde les plaques, « Importer la géométrie uniquement » ajoute les pièces au projet actuel.",
        "st.project_saved": "3MF enregistré : {path}. Ouvrez-le avec Fichier > Ouvrir un projet. {error}",
        "st.arranging": "Orca dispose {n} pièces et ajoute des plaques si besoin…",
        "st.arrange_done": "Disposition terminée.",
        "st.plugin_error": "Erreur du plugin.",
        "st.removed_source": "La source a été supprimée du disque ou masquée.",
        "printer.unreadable": "Profil non lu",
        "printer.zones": "zones",
        "printer.zones_tip": "Le profil d’imprimante a des zones exclues sur le plateau. Vérifiez la disposition dans Orca.",
        "lib.nothing_found": "Rien ne correspond au filtre.",
        "lib.press_refresh": "Appuyez sur « Actualiser » pour trouver des modèles dans les dossiers.",
        "lib.add_folder": "Déposez des fichiers ZIP ou STL ici, ou ajoutez un dossier de modèles dans les Paramètres.",
        "lib.new": "Nouveaux",
        "lib.added": "Ajoutés",
        "lib.all_folders": "Tous les dossiers",
        "lib.folder_tip": "Afficher seulement les sources d’un dossier",
        "lib.other_show": "{n} autres archives dont le nom ne ressemble pas à des modèles",
        "lib.other_hide": "Masquer les archives dont le nom ne ressemble pas à des modèles",
        "lib.other_note": "PlatePilot juge d’après le nom, car regarder dans chaque archive ferait demander Orca pour chacune. Ouvrez une archive pour la vérifier : avec des modèles elle remonte, sans modèles elle disparaît. La recherche les parcourt aussi.",
        "lib.mark_seen": "Tout marquer comme vu",
        "lib.no_new": "Aucune nouvelle source depuis la dernière vérification.",
        "drop.title": "Déposer pour ajouter à PlatePilot",
        "drop.note": "ZIP, STL, STEP, 3MF ou un dossier qui en contient. Les fichiers sont copiés dans le dossier de PlatePilot, donc Orca ne demande pas d’autorisation. Pour surveiller plutôt un dossier, ajoutez-le dans les Paramètres ou collez son chemin avec Ctrl+V.",
        "src.choose": "Choisissez une source",
        "src.choose_note": "Choisissez une archive ou un dossier dans la bibliothèque. PlatePilot montre ses pièces et un aperçu sur le plateau.",
        "src.summary": "{n} fichiers de modèles · {stl} STL · {path}",
        "src.hide": "Masquer",
        "src.hide_tip": "Masquer de la liste. Le fichier reste sur le disque.",
        "src.new": "nouveau",
        "src.added": "ajouté",
        "src.unopened": "pas encore ouvert",
        "src.delete": "Supprimer",
        "src.delete_tip": "Supprimer la copie ajoutée à PlatePilot",
        "delete.title": "Supprimer de PlatePilot ?",
        "delete.body": "La copie de « {name} » dans le dossier de PlatePilot est supprimée. Les fichiers que vous avez déposés ou collés ne sont pas touchés.",
        "parts.meta": "{dims} mm · {facets} facettes",
        "parts.catalog_only": "{type} · catalogue seulement",
        "parts.no_files": "La source n’a aucun fichier des formats choisis.",
        "parts.placeholder": "Les pièces apparaissent ici.",
        "view.meta": "{dims} mm · {facets} facettes",
        "view.shown": " · {n} affichées",
        "plan.parts": "{n} pièce|{n} pièces",
        "plan.plates": "{n} plaque|{n} plaques",
        "plan.not_fit": "ne tient pas : {name}",
        "plan.at_least": "il faut au moins {plates}",
        "plan.at_most": "au plus une plaque par pièce",
        "plan.too_tall": "plus haute que Z : {name}",
        "plan.zones": "vérifiez les zones exclues dans Orca",
        "plan.smart": "disposition intelligente β",
        "plan.plate": "Plaque {n}",
        "plan.view3d": "3D",
        "plan.view3d_all": "Plaques en 3D",
        "plan.view3d_all_tip": "Voir les plaques en 3D dans l’aperçu, chaque pièce comme dans le projet",
        "view.plate_button": "Plaque {n} sur {total} en 3D",
        "view.plate_prev": "Plaque précédente",
        "view.plate_next": "Plaque suivante",
        "plan.view3d_tip": "Voir cette plaque en 3D dans l’aperçu",
        "view.plate_meta": "{parts} · plateau {bed} · simplifié",
        "st.plate_shown": "{name} en 3D : faites glisser pour la tourner.",
        "st.plate_loading": "{name} en 3D : pièce {i} sur {n}…",
        "basket.add": "Au panier",
        "basket.add_tip": "Mettre les pièces cochées dans le panier, où les pièces de plusieurs sources sont disposées ensemble",
        "basket.show": "Panier · {n}",
        "basket.back": "Retour à la source",
        "basket.empty": "Le panier est vide. Cochez des pièces dans n’importe quelle source et appuyez sur « Au panier » ; ici, les pièces de plusieurs sources sont disposées ensemble.",
        "basket.from": "de {name}",
        "basket.copies": "Copies de cette pièce",
        "basket.remove": "Retirer du panier",
        "basket.clear": "Vider le panier",
        "basket.added": "Dans le panier : {n}",
        "basket.title": "Panier",
        "basket.and_more": "{name} et {n} autres",
        "err.basket_empty": "le panier est vide",
        "basket.turned": "tournée",
        "err.basket_gone": "« {name} » du panier n’est plus à sa place (déplacé, modifié ou supprimé) ; retirez-le et ajoutez-le de nouveau",
        "plan.plate_meta": "{parts} · {fill} %",
        "plan.rotated": " · tournée de {n}°",
        "plan.zoom_hint": "Cliquez sur une plaque pour la voir en grand",
        "plan.zoom_title": "Plaque {n} sur {total}",
        "plan.prev": "Plaque précédente",
        "plan.next": "Plaque suivante",
        "plan.close": "Fermer",
        "folders.none": "Pas encore de dossiers.",
        "folders.remove": "Ne plus surveiller",
        "folders.sources": "{n} source|{n} sources",
        "folders.stats": "{sources} · vérifié {time}",
        "folders.new": "{n} nouveaux",
        "folders.other": "{n} archives qui ne ressemblent pas à des modèles",
        "folders.empty": "{n} sans modèles masqués",
        "folders.never": "Pas encore vérifié",
        "folders.missing": "Dossier introuvable",
        "folders.skipped": "{n} sous-dossiers ignorés : Orca ne peut pas les ouvrir pour les plugins",
        "folders.unreadable": "Orca ne peut pas ouvrir ce dossier pour les plugins : son chemin contient des lettres non latines ou un dossier nommé config, cert ou secret",
        "folders.truncated": "seuls les {n} premiers sont affichés",
        "folders.added": "Ajoutés à PlatePilot",
        "folders.added_note": "Les fichiers déposés ou collés sont copiés ici.",
        "err.path_empty": "saisissez le chemin d’un dossier",
        "err.path_relative": "saisissez le chemin complet du dossier, par exemple {example}",
        "err.folder_missing": "dossier introuvable : {path}",
        "err.not_filesystem": "l’élément choisi n’est pas un dossier sur le disque (par exemple « Bibliothèques ») ; choisissez un dossier normal",
        "err.picker_windows": "Le sélecteur de dossier existe sous Windows ; collez plutôt le chemin.",
        "picker.title": "Dossier de modèles pour PlatePilot",
        "picker.ok": "Surveiller le dossier",
        "err.no_main_window": "la fenêtre principale d’Orca est introuvable",
        "err.open_unsupported": "ouvrir le projet dans cette fenêtre d’Orca n’est possible que sous Windows",
        "err.no_executable": "l’exécutable d’OrcaSlicer en cours est introuvable",
        "err.no_settings": "impossible de lire les réglages du profil Orca actif",
        "err.no_printer_preset": "aucun profil d’imprimante n’est choisi dans Orca",
        "err.no_process_preset": "aucun profil de traitement n’est choisi dans Orca",
        "warn.zip_empty": "Aucun STL, STEP ou 3MF dans l’archive",
        "warn.zip_unreadable": "Archive non lue : {error}",
        "err.archive_outside": "l’archive doit être dans un dossier surveillé",
        "err.folder_outside": "le dossier de modèles doit être dans un dossier surveillé",
        "warn.step_open": "STEP : pas d’aperçu ni de disposition ; ouvrez-le dans Orca, qui le convertit lui-même",
        "warn.3mf_error": "Le 3MF n’a pas pu être lu : {error}",
        "warn.3mf_empty": "Le 3MF ne contient aucun modèle",
        "btn.open_in_orca": "Ouvrir dans Orca",
        "msg.step_sent": "{name} envoyé à Orca : il l’ouvre dans « Préparer ».",
        "warn.unsafe_name": "nom avec des lettres non latines : cette version d’Orca ne peut pas l’ouvrir pour les plugins (OrcaSlicer 2.5.0 le peut) ; faites-le glisser dans PlatePilot",
        "err.unsafe_path": "{path} : Orca ne laisse pas les plugins ouvrir des chemins avec un dossier nommé config, cert ou secret, et les versions avant OrcaSlicer 2.5.0 aucun avec des lettres non latines. Faites glisser les fichiers dans PlatePilot ou renommez le dossier.",
        "st.no_answer": "Le plugin n’a pas répondu. Appuyez sur « Actualiser » pour réessayer.",
        "st.scan_interrupted": "La dernière vérification des dossiers ne s’est pas terminée (Orca a été fermé), elle n’a donc pas été relancée automatiquement. Appuyez sur « Actualiser » pour vérifier les dossiers.",
        "warn.too_big": "fichier de plus de {mb} Mo",
        "err.total_archive": "les fichiers STL de l’archive dépassent {mb} Mo au total",
        "err.total_folder": "les fichiers STL du dossier dépassent {mb} Mo au total",
        "warn.empty_model": "modèle vide",
        "warn.stl_error": "Erreur STL : {error}",
        "err.stl_short": "le fichier STL est trop court",
        "err.stl_coords": "le STL a des coordonnées invalides",
        "err.stl_vertices": "impossible de lire les sommets du STL",
        "err.stl_facets_limit": "le STL dépasse 2 millions de facettes",
        "err.stl_geometry": "le STL n’a pas de géométrie valide",
        "err.stl_no_triangles": "le STL n’a pas de triangles",
        "err.gap_too_big": "l’écart est plus grand que le plateau",
        "err.plan_first": "calculez d’abord la disposition",
        "err.reread": "impossible de relire le STL : {name}",
        "err.facets_limit": "un projet est limité à {n} facettes ; choisissez moins de pièces ou utilisez la disposition Orca",
        "msg.already_watched": "Ce dossier est déjà surveillé : {path}",
        "msg.inside_watched": "Le dossier est déjà dans le dossier surveillé {path} (la recherche dans les sous-dossiers est activée).",
        "msg.replaced_nested": "Dossier ajouté ; ces dossiers imbriqués ne sont plus nécessaires séparément : {paths}",
        "err.scan_stopped": "Analyse interrompue : {error}",
        "err.source_gone": "la source n’est plus dans un dossier surveillé",
        "err.select_source": "choisissez d’abord un ZIP ou un dossier avec des fichiers STL",
        "err.archive_moved": "l’archive n’est plus dans un dossier surveillé",
        "err.stl_left_archive": "le STL n’est plus dans l’archive choisie",
        "err.stl_left_folder": "le STL n’est plus dans le dossier choisi",
        "err.stl_limit": "le STL dépasse la limite de {mb} Mo",
        "err.arrange_running": "la disposition Orca est déjà en cours",
        "err.select_stl": "choisissez au moins une pièce STL",
        "err.too_many_parts": "un passage peut disposer au plus {n} fichiers STL",
        "err.no_bed": "impossible de lire la zone imprimable de l’imprimante active",
        "err.cli_failed": "La ligne de commande d’Orca n’a pas créé de projet. {detail}",
        "err.cli_exit": "Code de sortie : {code}",
        "msg.arranged": "Orca a disposé {parts} sur {plates} et ouvre le projet. Si « Préparer » contient déjà des modèles, choisissez « Ouvrir en tant que projet » pour garder les plaques.",
        "msg.arranged_partly": "Orca n’a pas pu placer {n} sur {parts} sur les plaques, même tournées : elles restent à côté des plaques. Comparez leur taille au plateau ou utilisez la disposition PlatePilot pour voir lesquelles.",
        "err.arrange_failed": "La disposition ne s’est pas terminée : {error}",
        "msg.picker_cancelled": "Choix du dossier annulé.",
        "msg.source_hidden": "La source est masquée de la liste ; le fichier sur le disque est conservé.",
        "msg.deleted": "Supprimé de PlatePilot.",
        "msg.imported": "Ajouté à PlatePilot : {files}.",
        "import.files": "{n} fichier|{n} fichiers",
        "import.group": "Fichiers {time}",
        "err.import_type": "{name} : seuls les fichiers ZIP, STL, STEP et 3MF peuvent être ajoutés",
        "err.import_size": "{name} dépasse {mb} Mo",
        "err.import_state": "le transfert des fichiers a été interrompu ; ajoutez-les de nouveau",
        "err.import_path": "le nom de {name} est trop long pour le dossier de PlatePilot",
        "err.no_storage": "le dossier de données de PlatePilot n’est pas disponible : {error}",
        "err.not_added": "seules les sources ajoutées à PlatePilot peuvent être supprimées",
        "err.paste_path": "{path} n’est ni un dossier ni un fichier ZIP, STL, STEP ou 3MF",
        "err.part_missing": "pièce introuvable",
        "err.no_preview": "cette pièce n’a pas de données d’aperçu",
        "err.stl_outside": "le STL est en dehors du dossier surveillé",
        "err.no_printable_area": "Impossible de lire printable_area du profil actif. Rechargez le profil d’imprimante et réessayez.",
        "err.plan_source": "calculez d’abord la disposition de la source choisie",
        "err.plan_not_exportable": "corrigez d’abord les pièces qui ne tiennent pas sur le plateau ou qui sont plus hautes que l’imprimante",
        "err.printer_changed": "l’imprimante active a changé après le calcul ; recalculez la disposition",
        "err.stl_changed": "un STL a changé après le calcul ; recalculez la disposition"
    },
    "es": {
        "btn.support": "Apoyar",
        "btn.support_tip": "Apoya PlatePilot con estrellas de Telegram: abre t.me/GIMINOT en el navegador",
        "err.support_open": "El navegador no se abrió. Telegram del autor: {url}",
        "err.3mf_transform": "la posición de un objeto en el archivo está dañada",
        "err.3mf_nesting": "los objetos están anidados demasiado",
        "app.tagline": "Biblioteca de modelos y planificador de placas",
        "app.loading_profile": "Cargando el perfil de la impresora…",
        "btn.refresh": "Actualizar",
        "btn.folders": "Ajustes",
        "btn.add_files": "Añadir archivos",
        "btn.add_files_tip": "Añade archivos ZIP, STL, STEP o 3MF. Se copian en PlatePilot, así que Orca no pide permiso.",
        "lib.title": "Biblioteca",
        "lib.search": "Buscar por nombre",
        "lib.all": "Todo",
        "lib.folders": "Carpetas",
        "parts.title": "Piezas",
        "parts.all": "Todas",
        "parts.none": "Ninguna",
        "view.reset": "Restablecer vista",
        "view.hint": "Arrastra para girar · rueda para hacer zoom · doble clic para restablecer",
        "view.empty": "Selecciona una pieza STL para verla en la cama de la impresora",
        "mode.own": "Distribución PlatePilot",
        "mode.orca": "Organización de Orca",
        "mode.gap": "Separación",
        "mode.orient": "Girar piezas (orientación de Orca)",
        "mode.orient_tip": "Antes de organizar, Orca gira cada pieza como lo haría su «Orientar automáticamente»: menos voladizo, una base estable. Desactivado: las piezas quedan como en sus archivos, como las pensaron sus diseñadores.",
        "orient.checking": "Comprobando cómo están las piezas… {done}/{total}",
        "orient.done": "{n} pieza podría estar mejor colocada: mira «Girar» en la lista|{n} piezas podrían estar mejor colocadas: mira «Girar» en la lista",
        "orient.advice": "mejor girada: {pct} % menos voladizo",
        "orient.turned": "girada: {pct} % menos voladizo",
        "btn.turn": "Girar",
        "btn.unturn": "Deshacer giro",
        "btn.turn_all": "Girar todas las sugeridas ({n})",
        "btn.turn_all_tip": "Girar cada pieza que PlatePilot encontró peor colocada de lo posible: boca abajo, sobre un borde estrecho, con voladizos evitables",
        "mode.plates": "Placas",
        "mode.plates_auto": "auto",
        "mode.plates_tip": "En cuántas placas repartir las piezas. Vacío: las mínimas en que quepan. Más que eso las reparte por igual, aunque cupieran en una.",
        "unit.mm": "mm",
        "btn.plan": "Calcular",
        "btn.plan_smart": "Distribución inteligente",
        "btn.plan_smart_tip": "Beta: las piezas de cada placa se agrupan en el centro, las pequeñas más cerca del centro, lejos de los bordes de la cama y con separación uniforme; las placas se llenan por igual",
        "btn.send": "Abrir en Preparar",
        "btn.arrange": "Organizar en Orca",
        "mode.own_note": "PlatePilot coloca las piezas según su huella en la cama de la impresora activa, muestra las placas y abre el proyecto en «Preparar».",
        "mode.orca_note": "Las piezas pasan a la organización propia de Orca: usa los contornos de las piezas y los perfiles activos de impresora y proceso, añade placas si hace falta y abre el proyecto.",
        "drawer.title": "Ajustes",
        "drawer.close": "Cerrar",
        "drawer.folders": "Carpetas vigiladas",
        "drawer.folders_note": "PlatePilot encuentra archivos ZIP y carpetas con archivos de modelos por su nombre. Un archivo solo se lee cuando lo abres.",
        "drawer.pick": "Elegir carpeta…",
        "drawer.paste": "O pega la ruta de una carpeta (sirven las comillas de «Copiar como ruta de acceso»):",
        "drawer.paste_only": "Pega la ruta completa de una carpeta con modelos:",
        "drawer.path_ph": "Ruta completa de la carpeta",
        "drawer.add": "Añadir",
        "drawer.formats": "Formatos",
        "drawer.formats_note": "Las fuentes sin los formatos elegidos se ocultan. La vista previa y la distribución funcionan con STL.",
        "drawer.scan": "Búsqueda",
        "drawer.recursive": "Buscar en subcarpetas",
        "drawer.check_on_open": "Revisar las carpetas al abrir PlatePilot",
        "drawer.audit_note": "Orca pregunta una vez antes de que PlatePilot mire en una carpeta vigilada, y una vez por cada archivo o carpeta de modelos que abras. Los archivos que sueltas en PlatePilot no necesitan permiso.",
        "drawer.preview": "Vista previa 3D",
        "drawer.gpu_preview": "Dibujar con la tarjeta gráfica (WebGL)",
        "drawer.gpu_note": "Muestra completas las piezas de hasta 250 000 facetas y las gira con fluidez. Si la vista previa se ve mal, desactívalo: el modo simple dibuja hasta 30 000 facetas.",
        "drawer.theme": "Tema",
        "theme.orca": "Como en Orca",
        "theme.parchment": "Pergamino",
        "theme.terracotta": "Terracota",
        "theme.sage": "Salvia",
        "theme.blueprint": "Plano",
        "theme.dusk": "Anochecer",
        "theme.workshop": "Taller (oscuro)",
        "theme.night": "Turno de noche (oscuro)",
        "drawer.language": "Idioma",
        "drawer.lang_auto": "Como en Orca",
        "modal.confirm": "Confirmar",
        "modal.cancel": "Cancelar",
        "modal.continue": "Continuar",
        "st.choose_folder": "Suelta aquí archivos ZIP o STL, o abre Ajustes y elige una carpeta con modelos.",
        "st.press_refresh": "Pulsa «Actualizar» para revisar las carpetas.",
        "st.scanning": "Revisando carpetas…",
        "st.scanned": "Carpetas revisadas · {sources}",
        "st.scanned_new": "Carpetas revisadas · {sources} · {n} nuevas",
        "st.importing": "Añadiendo {name} · {i} de {n} · {pct} %",
        "st.import_none": "Nada que añadir: suelta archivos ZIP, STL, STEP o 3MF o una carpeta con ellos.",
        "st.import_skipped": " Otros archivos omitidos: {n}.",
        "st.opening": "Abriendo…",
        "st.opening_picker": "Abriendo el selector de carpetas…",
        "st.opened_source": "Abierto «{name}».",
        "st.plan_ready": "La distribución está lista. Ábrela en «Preparar».",
        "st.plan_problems": "Algunas piezas no caben en la cama. Cambia la selección.",
        "st.building": "Creando el proyecto para Orca…",
        "st.project_opened": "El proyecto se está abriendo en Orca. Si «Preparar» ya tiene modelos, Orca pregunta qué hacer: «Abrir como proyecto» conserva las placas, «Importar geometría solo» añade las piezas al proyecto actual.",
        "st.project_saved": "3MF guardado: {path}. Ábrelo con Archivo > Abrir proyecto. {error}",
        "st.arranging": "Orca está organizando {n} piezas y añadiendo placas si hace falta…",
        "st.arrange_done": "Organización terminada.",
        "st.plugin_error": "Error del plugin.",
        "st.removed_source": "La fuente se eliminó del disco o se ocultó.",
        "printer.unreadable": "Perfil no leído",
        "printer.zones": "zonas",
        "printer.zones_tip": "El perfil de la impresora tiene zonas excluidas en la cama. Revisa la distribución en Orca.",
        "lib.nothing_found": "Nada coincide con el filtro.",
        "lib.press_refresh": "Pulsa «Actualizar» para encontrar modelos en las carpetas.",
        "lib.add_folder": "Suelta aquí archivos ZIP o STL, o añade una carpeta con modelos en Ajustes.",
        "lib.new": "Nuevas",
        "lib.added": "Añadidas",
        "lib.all_folders": "Todas las carpetas",
        "lib.folder_tip": "Mostrar solo las fuentes de una carpeta",
        "lib.other_show": "{n} archivos más cuyo nombre no parece de modelos",
        "lib.other_hide": "Ocultar los archivos cuyo nombre no parece de modelos",
        "lib.other_note": "PlatePilot juzga por el nombre, porque mirar dentro de cada archivo haría que Orca preguntara por cada uno. Abre un archivo para comprobarlo: con modelos sube, sin ellos desaparece. La búsqueda también los recorre.",
        "lib.mark_seen": "Marcar todo como visto",
        "lib.no_new": "No hay fuentes nuevas desde la última revisión.",
        "drop.title": "Suelta para añadir a PlatePilot",
        "drop.note": "ZIP, STL, STEP, 3MF o una carpeta con ellos. Los archivos se copian en la carpeta propia de PlatePilot, así que Orca no pide permiso. Para vigilar una carpeta en su lugar, añádela en Ajustes o pega su ruta con Ctrl+V.",
        "src.choose": "Elige una fuente",
        "src.choose_note": "Elige un archivo o una carpeta en la biblioteca. PlatePilot muestra sus piezas y una vista previa en la cama.",
        "src.summary": "{n} archivos de modelos · {stl} STL · {path}",
        "src.hide": "Ocultar",
        "src.hide_tip": "Ocultar de la lista. El archivo sigue en el disco.",
        "src.new": "nueva",
        "src.added": "añadida",
        "src.unopened": "aún no abierta",
        "src.delete": "Eliminar",
        "src.delete_tip": "Eliminar la copia añadida a PlatePilot",
        "delete.title": "¿Eliminar de PlatePilot?",
        "delete.body": "Se elimina la copia de «{name}» en la carpeta de PlatePilot. Los archivos que soltaste o pegaste no se tocan.",
        "parts.meta": "{dims} mm · {facets} facetas",
        "parts.catalog_only": "{type} · solo en catálogo",
        "parts.no_files": "La fuente no tiene archivos de los formatos elegidos.",
        "parts.placeholder": "Aquí aparecen las piezas.",
        "view.meta": "{dims} mm · {facets} facetas",
        "view.shown": " · {n} mostradas",
        "plan.parts": "{n} pieza|{n} piezas",
        "plan.plates": "{n} placa|{n} placas",
        "plan.not_fit": "no cabe: {name}",
        "plan.at_least": "necesita al menos {plates}",
        "plan.at_most": "como mucho una placa por pieza",
        "plan.too_tall": "más alta que Z: {name}",
        "plan.zones": "revisa las zonas excluidas en Orca",
        "plan.smart": "distribución inteligente β",
        "plan.plate": "Placa {n}",
        "plan.view3d": "3D",
        "plan.view3d_all": "Placas en 3D",
        "plan.view3d_all_tip": "Ver las placas en 3D en la vista previa, cada pieza como quedará en el proyecto",
        "view.plate_button": "Placa {n} de {total} en 3D",
        "view.plate_prev": "Placa anterior",
        "view.plate_next": "Placa siguiente",
        "plan.view3d_tip": "Ver esta placa en 3D en la vista previa",
        "view.plate_meta": "{parts} · cama {bed} · simplificado",
        "st.plate_shown": "{name} en 3D: arrastra para girarla.",
        "st.plate_loading": "{name} en 3D: pieza {i} de {n}…",
        "basket.add": "A la cesta",
        "basket.add_tip": "Poner las piezas marcadas en la cesta, donde las piezas de varias fuentes se distribuyen juntas",
        "basket.show": "Cesta · {n}",
        "basket.back": "Volver a la fuente",
        "basket.empty": "La cesta está vacía. Marca piezas en cualquier fuente y pulsa «A la cesta»; aquí las piezas de varias fuentes se distribuyen juntas.",
        "basket.from": "de {name}",
        "basket.copies": "Copias de esta pieza",
        "basket.remove": "Sacar de la cesta",
        "basket.clear": "Vaciar la cesta",
        "basket.added": "En la cesta: {n}",
        "basket.title": "Cesta",
        "basket.and_more": "{name} y {n} más",
        "err.basket_empty": "la cesta está vacía",
        "basket.turned": "girada",
        "err.basket_gone": "«{name}» de la cesta ya no está donde estaba (movido, cambiado o eliminado); sácalo y vuelve a añadirlo",
        "plan.plate_meta": "{parts} · {fill} %",
        "plan.rotated": " · girada {n}°",
        "plan.zoom_hint": "Haz clic en una placa para verla de cerca",
        "plan.zoom_title": "Placa {n} de {total}",
        "plan.prev": "Placa anterior",
        "plan.next": "Placa siguiente",
        "plan.close": "Cerrar",
        "folders.none": "Aún no hay carpetas.",
        "folders.remove": "Dejar de vigilar",
        "folders.sources": "{n} fuente|{n} fuentes",
        "folders.stats": "{sources} · revisada {time}",
        "folders.new": "{n} nuevas",
        "folders.other": "{n} archivos que no parecen de modelos",
        "folders.empty": "{n} sin modelos ocultas",
        "folders.never": "Aún no revisada",
        "folders.missing": "Carpeta no encontrada",
        "folders.skipped": "{n} subcarpetas omitidas: Orca no puede abrirlas para los plugins",
        "folders.unreadable": "Orca no puede abrir esta carpeta para los plugins: su ruta tiene letras no latinas o una carpeta llamada config, cert o secret",
        "folders.truncated": "solo se muestran las primeras {n}",
        "folders.added": "Añadidas a PlatePilot",
        "folders.added_note": "Los archivos que sueltas o pegas se copian aquí.",
        "err.path_empty": "escribe la ruta de una carpeta",
        "err.path_relative": "escribe la ruta completa de la carpeta, por ejemplo {example}",
        "err.folder_missing": "carpeta no encontrada: {path}",
        "err.not_filesystem": "el elemento elegido no es una carpeta del disco (por ejemplo, «Bibliotecas»); elige una carpeta normal",
        "err.picker_windows": "El selector de carpetas está disponible en Windows; pega la ruta en su lugar.",
        "picker.title": "Carpeta con modelos para PlatePilot",
        "picker.ok": "Vigilar carpeta",
        "err.no_main_window": "no se encontró la ventana principal de Orca",
        "err.open_unsupported": "abrir el proyecto en esta ventana de Orca solo es posible en Windows",
        "err.no_executable": "no se encontró el ejecutable de OrcaSlicer en marcha",
        "err.no_settings": "no se pudieron leer los ajustes del perfil activo de Orca",
        "err.no_printer_preset": "no hay ningún perfil de impresora elegido en Orca",
        "err.no_process_preset": "no hay ningún perfil de proceso elegido en Orca",
        "warn.zip_empty": "No hay STL, STEP ni 3MF en el archivo",
        "warn.zip_unreadable": "Archivo no leído: {error}",
        "err.archive_outside": "el archivo debe estar dentro de una carpeta vigilada",
        "err.folder_outside": "la carpeta de modelos debe estar dentro de una carpeta vigilada",
        "warn.step_open": "STEP: sin vista previa ni distribución; ábrelo en Orca, que lo convierte por sí mismo",
        "warn.3mf_error": "No se pudo leer el 3MF: {error}",
        "warn.3mf_empty": "El 3MF no contiene ningún modelo",
        "btn.open_in_orca": "Abrir en Orca",
        "msg.step_sent": "{name} enviado a Orca: lo abre en «Preparar».",
        "warn.unsafe_name": "nombre con letras no latinas: esta versión de Orca no puede abrirlo para los plugins (OrcaSlicer 2.5.0 sí puede); arrástralo a PlatePilot",
        "err.unsafe_path": "{path}: Orca no deja a los plugins abrir rutas con una carpeta llamada config, cert o secret, y las versiones anteriores a OrcaSlicer 2.5.0 ninguna con letras no latinas. Arrastra los archivos a PlatePilot o cambia el nombre de la carpeta.",
        "st.no_answer": "El plugin no respondió. Pulsa «Actualizar» para intentarlo de nuevo.",
        "st.scan_interrupted": "La última revisión de carpetas no terminó (Orca se cerró), así que no se repitió automáticamente. Pulsa «Actualizar» para revisar las carpetas.",
        "warn.too_big": "archivo de más de {mb} MB",
        "err.total_archive": "los archivos STL del archivo superan {mb} MB en total",
        "err.total_folder": "los archivos STL de la carpeta superan {mb} MB en total",
        "warn.empty_model": "modelo vacío",
        "warn.stl_error": "Error de STL: {error}",
        "err.stl_short": "el archivo STL es demasiado corto",
        "err.stl_coords": "el STL tiene coordenadas no válidas",
        "err.stl_vertices": "no se pudieron leer los vértices del STL",
        "err.stl_facets_limit": "el STL supera los 2 millones de facetas",
        "err.stl_geometry": "el STL no tiene geometría válida",
        "err.stl_no_triangles": "el STL no tiene triángulos",
        "err.gap_too_big": "la separación es mayor que la cama",
        "err.plan_first": "calcula primero la distribución",
        "err.reread": "no se pudo volver a leer el STL: {name}",
        "err.facets_limit": "un proyecto está limitado a {n} facetas; elige menos piezas o usa la organización de Orca",
        "msg.already_watched": "Esta carpeta ya está vigilada: {path}",
        "msg.inside_watched": "La carpeta ya está dentro de la carpeta vigilada {path} (la búsqueda en subcarpetas está activada).",
        "msg.replaced_nested": "Carpeta añadida; estas carpetas anidadas ya no hacen falta por separado: {paths}",
        "err.scan_stopped": "Búsqueda detenida: {error}",
        "err.source_gone": "la fuente ya no está dentro de una carpeta vigilada",
        "err.select_source": "elige primero un ZIP o una carpeta con archivos STL",
        "err.archive_moved": "el archivo ya no está dentro de una carpeta vigilada",
        "err.stl_left_archive": "el STL ya no está en el archivo elegido",
        "err.stl_left_folder": "el STL ya no está en la carpeta elegida",
        "err.stl_limit": "el STL supera el límite de {mb} MB",
        "err.arrange_running": "la organización de Orca ya está en marcha",
        "err.select_stl": "elige al menos una pieza STL",
        "err.too_many_parts": "una pasada puede organizar como mucho {n} archivos STL",
        "err.no_bed": "no se pudo leer el área imprimible de la impresora activa",
        "err.cli_failed": "La línea de comandos de Orca no creó un proyecto. {detail}",
        "err.cli_exit": "Código de salida: {code}",
        "msg.arranged": "Orca organizó {parts} en {plates} y está abriendo el proyecto. Si «Preparar» ya tiene modelos, elige «Abrir como proyecto» para conservar las placas.",
        "msg.arranged_partly": "Orca no pudo colocar {n} de {parts} en las placas, ni siquiera girándolas: quedan junto a las placas. Compara su tamaño con la cama o usa la distribución PlatePilot para ver cuáles son.",
        "err.arrange_failed": "La organización no terminó: {error}",
        "msg.picker_cancelled": "Selección de carpeta cancelada.",
        "msg.source_hidden": "La fuente se ocultó de la lista; el archivo del disco se conserva.",
        "msg.deleted": "Eliminado de PlatePilot.",
        "msg.imported": "Añadido a PlatePilot: {files}.",
        "import.files": "{n} archivo|{n} archivos",
        "import.group": "Archivos {time}",
        "err.import_type": "{name}: solo se pueden añadir archivos ZIP, STL, STEP y 3MF",
        "err.import_size": "{name} supera {mb} MB",
        "err.import_state": "la transferencia de archivos se interrumpió; vuelve a añadirlos",
        "err.import_path": "el nombre de {name} es demasiado largo para la carpeta de PlatePilot",
        "err.no_storage": "la carpeta de datos de PlatePilot no está disponible: {error}",
        "err.not_added": "solo se pueden eliminar las fuentes añadidas a PlatePilot",
        "err.paste_path": "{path} no es una carpeta ni un archivo ZIP, STL, STEP o 3MF",
        "err.part_missing": "pieza no encontrada",
        "err.no_preview": "esta pieza no tiene datos de vista previa",
        "err.stl_outside": "el STL está fuera de la carpeta vigilada",
        "err.no_printable_area": "No se pudo leer printable_area del perfil activo. Recarga el perfil de la impresora y vuelve a intentarlo.",
        "err.plan_source": "calcula primero la distribución de la fuente elegida",
        "err.plan_not_exportable": "corrige primero las piezas que no caben en la cama o que son más altas que la impresora",
        "err.printer_changed": "la impresora activa cambió después del cálculo; vuelve a calcular la distribución",
        "err.stl_changed": "un STL cambió después del cálculo; vuelve a calcular la distribución"
    },
    "pt_BR": {
        "btn.support": "Apoiar",
        "btn.support_tip": "Apoie o PlatePilot com estrelas do Telegram: abre t.me/GIMINOT no navegador",
        "err.support_open": "O navegador não abriu. Telegram do autor: {url}",
        "err.3mf_transform": "a posição de um objeto no arquivo está corrompida",
        "err.3mf_nesting": "os objetos estão aninhados demais",
        "app.tagline": "Biblioteca de modelos e planejador de placas",
        "app.loading_profile": "Carregando o perfil da impressora…",
        "btn.refresh": "Atualizar",
        "btn.folders": "Configurações",
        "btn.add_files": "Adicionar arquivos",
        "btn.add_files_tip": "Adicione arquivos ZIP, STL, STEP ou 3MF. Eles são copiados para o PlatePilot, então o Orca não pede permissão.",
        "lib.title": "Biblioteca",
        "lib.search": "Buscar pelo nome",
        "lib.all": "Tudo",
        "lib.folders": "Pastas",
        "parts.title": "Peças",
        "parts.all": "Todas",
        "parts.none": "Nenhuma",
        "view.reset": "Redefinir vista",
        "view.hint": "Arraste para girar · roda para zoom · clique duplo para redefinir",
        "view.empty": "Selecione uma peça STL para vê-la na mesa da impressora",
        "mode.own": "Layout do PlatePilot",
        "mode.orca": "Organização do Orca",
        "mode.gap": "Espaço",
        "mode.orient": "Girar peças (orientação do Orca)",
        "mode.orient_tip": "Antes de organizar, o Orca gira cada peça como faria o seu «Auto-Orientar»: menos saliência, uma base firme. Desligado: as peças ficam como nos arquivos, como os designers quiseram.",
        "orient.checking": "Verificando como as peças estão… {done}/{total}",
        "orient.done": "{n} peça poderia ficar melhor: veja «Girar» na lista|{n} peças poderiam ficar melhor: veja «Girar» na lista",
        "orient.advice": "fica melhor girada: {pct}% menos saliência",
        "orient.turned": "girada: {pct}% menos saliência",
        "btn.turn": "Girar",
        "btn.unturn": "Desfazer giro",
        "btn.turn_all": "Girar todas as sugeridas ({n})",
        "btn.turn_all_tip": "Girar cada peça que o PlatePilot achou pior posicionada do que poderia: de cabeça para baixo, sobre uma borda estreita, com saliências evitáveis",
        "mode.plates": "Placas",
        "mode.plates_auto": "auto",
        "mode.plates_tip": "Em quantas placas distribuir as peças. Vazio: o mínimo em que caibam. Mais do que isso distribui por igual, mesmo que caibam em uma.",
        "unit.mm": "mm",
        "btn.plan": "Calcular",
        "btn.plan_smart": "Layout inteligente",
        "btn.plan_smart_tip": "Beta: as peças de cada placa ficam agrupadas no meio, as pequenas mais perto do centro, longe das bordas da mesa e com espaçamento uniforme; as placas são preenchidas por igual",
        "btn.send": "Abrir em Preparar",
        "btn.arrange": "Organizar no Orca",
        "mode.own_note": "O PlatePilot arruma as peças pela área de apoio na mesa da impressora ativa, mostra as placas e abre o projeto em «Preparar».",
        "mode.orca_note": "As peças vão para a organização do próprio Orca: ela usa os contornos das peças e os perfis ativos de impressora e processo, adiciona placas quando preciso e abre o projeto.",
        "drawer.title": "Configurações",
        "drawer.close": "Fechar",
        "drawer.folders": "Pastas monitoradas",
        "drawer.folders_note": "O PlatePilot encontra arquivos ZIP e pastas com arquivos de modelos pelo nome. Um arquivo só é lido quando você o abre.",
        "drawer.pick": "Escolher pasta…",
        "drawer.paste": "Ou cole o caminho de uma pasta (aspas como as de «Copiar como caminho» são aceitas):",
        "drawer.paste_only": "Cole o caminho completo de uma pasta com modelos:",
        "drawer.path_ph": "Caminho completo da pasta",
        "drawer.add": "Adicionar",
        "drawer.formats": "Formatos",
        "drawer.formats_note": "Fontes sem os formatos escolhidos ficam ocultas. A pré-visualização e o layout funcionam com STL.",
        "drawer.scan": "Busca",
        "drawer.recursive": "Buscar em subpastas",
        "drawer.check_on_open": "Verificar as pastas ao abrir o PlatePilot",
        "drawer.audit_note": "O Orca pergunta uma vez antes de o PlatePilot olhar uma pasta monitorada, e uma vez para cada arquivo compactado ou pasta de modelos que você abrir. Arquivos soltos no PlatePilot não precisam de permissão.",
        "drawer.preview": "Pré-visualização 3D",
        "drawer.gpu_preview": "Desenhar com a placa de vídeo (WebGL)",
        "drawer.gpu_note": "Mostra completas as peças de até 250 000 facetas e gira com suavidade. Se a pré-visualização parecer errada, desligue: o modo simples desenha até 30 000 facetas.",
        "drawer.theme": "Tema",
        "theme.orca": "Como no Orca",
        "theme.parchment": "Pergaminho",
        "theme.terracotta": "Terracota",
        "theme.sage": "Sálvia",
        "theme.blueprint": "Planta",
        "theme.dusk": "Crepúsculo",
        "theme.workshop": "Oficina (escuro)",
        "theme.night": "Turno da noite (escuro)",
        "drawer.language": "Idioma",
        "drawer.lang_auto": "Como no Orca",
        "modal.confirm": "Confirmar",
        "modal.cancel": "Cancelar",
        "modal.continue": "Continuar",
        "st.choose_folder": "Solte arquivos ZIP ou STL aqui, ou abra as Configurações e escolha uma pasta com modelos.",
        "st.press_refresh": "Clique em «Atualizar» para verificar as pastas.",
        "st.scanning": "Verificando pastas…",
        "st.scanned": "Pastas verificadas · {sources}",
        "st.scanned_new": "Pastas verificadas · {sources} · {n} novas",
        "st.importing": "Adicionando {name} · {i} de {n} · {pct}%",
        "st.import_none": "Nada a adicionar: solte arquivos ZIP, STL, STEP ou 3MF ou uma pasta com eles.",
        "st.import_skipped": " Outros arquivos ignorados: {n}.",
        "st.opening": "Abrindo…",
        "st.opening_picker": "Abrindo o seletor de pastas…",
        "st.opened_source": "«{name}» aberto.",
        "st.plan_ready": "O layout está pronto. Abra-o em «Preparar».",
        "st.plan_problems": "Algumas peças não cabem na mesa. Mude a seleção.",
        "st.building": "Montando o projeto para o Orca…",
        "st.project_opened": "O projeto está abrindo no Orca. Se «Preparar» já tem modelos, o Orca pergunta o que fazer: «Abrir como projeto» mantém as placas, «Importar apenas a geometria» adiciona as peças ao projeto atual.",
        "st.project_saved": "3MF salvo: {path}. Abra-o com Arquivo > Abrir Projeto. {error}",
        "st.arranging": "O Orca está organizando {n} peças e adicionando placas quando preciso…",
        "st.arrange_done": "Organização concluída.",
        "st.plugin_error": "Erro do plugin.",
        "st.removed_source": "A fonte foi excluída do disco ou ocultada.",
        "printer.unreadable": "Perfil não lido",
        "printer.zones": "zonas",
        "printer.zones_tip": "O perfil da impressora tem áreas excluídas na mesa. Confira o layout no Orca.",
        "lib.nothing_found": "Nada corresponde ao filtro.",
        "lib.press_refresh": "Clique em «Atualizar» para encontrar modelos nas pastas.",
        "lib.add_folder": "Solte arquivos ZIP ou STL aqui, ou adicione uma pasta com modelos nas Configurações.",
        "lib.new": "Novas",
        "lib.added": "Adicionadas",
        "lib.all_folders": "Todas as pastas",
        "lib.folder_tip": "Mostrar só as fontes de uma pasta",
        "lib.other_show": "Mais {n} arquivos compactados cujo nome não parece de modelos",
        "lib.other_hide": "Ocultar os arquivos compactados cujo nome não parece de modelos",
        "lib.other_note": "O PlatePilot julga pelo nome, porque olhar dentro de cada arquivo faria o Orca perguntar sobre cada um. Abra um arquivo para conferir: com modelos ele sobe, sem eles desaparece. A busca também procura neles.",
        "lib.mark_seen": "Marcar tudo como visto",
        "lib.no_new": "Nenhuma fonte nova desde a última verificação.",
        "drop.title": "Solte para adicionar ao PlatePilot",
        "drop.note": "ZIP, STL, STEP, 3MF ou uma pasta com eles. Os arquivos são copiados para a pasta do próprio PlatePilot, então o Orca não pede permissão. Para monitorar uma pasta em vez disso, adicione-a nas Configurações ou cole o caminho com Ctrl+V.",
        "src.choose": "Escolha uma fonte",
        "src.choose_note": "Escolha um arquivo compactado ou uma pasta na biblioteca. O PlatePilot mostra as peças e uma pré-visualização na mesa.",
        "src.summary": "{n} arquivos de modelos · {stl} STL · {path}",
        "src.hide": "Ocultar",
        "src.hide_tip": "Ocultar da lista. O arquivo continua no disco.",
        "src.new": "nova",
        "src.added": "adicionada",
        "src.unopened": "ainda não aberta",
        "src.delete": "Excluir",
        "src.delete_tip": "Excluir a cópia adicionada ao PlatePilot",
        "delete.title": "Excluir do PlatePilot?",
        "delete.body": "A cópia de «{name}» na pasta do PlatePilot é excluída. Os arquivos que você soltou ou colou não são alterados.",
        "parts.meta": "{dims} mm · {facets} facetas",
        "parts.catalog_only": "{type} · só no catálogo",
        "parts.no_files": "A fonte não tem arquivos dos formatos escolhidos.",
        "parts.placeholder": "As peças aparecem aqui.",
        "view.meta": "{dims} mm · {facets} facetas",
        "view.shown": " · {n} mostradas",
        "plan.parts": "{n} peça|{n} peças",
        "plan.plates": "{n} placa|{n} placas",
        "plan.not_fit": "não cabe: {name}",
        "plan.at_least": "precisa de pelo menos {plates}",
        "plan.at_most": "no máximo uma placa por peça",
        "plan.too_tall": "mais alta que Z: {name}",
        "plan.zones": "confira as áreas excluídas no Orca",
        "plan.smart": "layout inteligente β",
        "plan.plate": "Placa {n}",
        "plan.view3d": "3D",
        "plan.view3d_all": "Placas em 3D",
        "plan.view3d_all_tip": "Ver as placas em 3D na pré-visualização, cada peça como ficará no projeto",
        "view.plate_button": "Placa {n} de {total} em 3D",
        "view.plate_prev": "Placa anterior",
        "view.plate_next": "Próxima placa",
        "plan.view3d_tip": "Ver esta placa em 3D na pré-visualização",
        "view.plate_meta": "{parts} · mesa {bed} · simplificado",
        "st.plate_shown": "{name} em 3D: arraste para girar.",
        "st.plate_loading": "{name} em 3D: peça {i} de {n}…",
        "basket.add": "Para a cesta",
        "basket.add_tip": "Colocar as peças marcadas na cesta, onde peças de várias fontes são organizadas juntas",
        "basket.show": "Cesta · {n}",
        "basket.back": "Voltar à fonte",
        "basket.empty": "A cesta está vazia. Marque peças em qualquer fonte e clique em «Para a cesta»; aqui as peças de várias fontes são organizadas juntas.",
        "basket.from": "de {name}",
        "basket.copies": "Cópias desta peça",
        "basket.remove": "Tirar da cesta",
        "basket.clear": "Esvaziar a cesta",
        "basket.added": "Na cesta: {n}",
        "basket.title": "Cesta",
        "basket.and_more": "{name} e mais {n}",
        "err.basket_empty": "a cesta está vazia",
        "basket.turned": "girada",
        "err.basket_gone": "«{name}» da cesta não está mais onde estava (movido, alterado ou excluído); tire-o e adicione de novo",
        "plan.plate_meta": "{parts} · {fill}%",
        "plan.rotated": " · girada {n}°",
        "plan.zoom_hint": "Clique em uma placa para vê-la de perto",
        "plan.zoom_title": "Placa {n} de {total}",
        "plan.prev": "Placa anterior",
        "plan.next": "Próxima placa",
        "plan.close": "Fechar",
        "folders.none": "Ainda não há pastas.",
        "folders.remove": "Parar de monitorar",
        "folders.sources": "{n} fonte|{n} fontes",
        "folders.stats": "{sources} · verificada {time}",
        "folders.new": "{n} novas",
        "folders.other": "{n} arquivos compactados que não parecem de modelos",
        "folders.empty": "{n} sem modelos ocultas",
        "folders.never": "Ainda não verificada",
        "folders.missing": "Pasta não encontrada",
        "folders.skipped": "{n} subpastas ignoradas: o Orca não consegue abri-las para os plugins",
        "folders.unreadable": "O Orca não consegue abrir esta pasta para os plugins: o caminho tem letras não latinas ou uma pasta chamada config, cert ou secret",
        "folders.truncated": "só as primeiras {n} são mostradas",
        "folders.added": "Adicionadas ao PlatePilot",
        "folders.added_note": "Arquivos soltos ou colados são copiados para cá.",
        "err.path_empty": "digite o caminho de uma pasta",
        "err.path_relative": "digite o caminho completo da pasta, por exemplo {example}",
        "err.folder_missing": "pasta não encontrada: {path}",
        "err.not_filesystem": "o item escolhido não é uma pasta do disco (por exemplo, «Bibliotecas»); escolha uma pasta comum",
        "err.picker_windows": "O seletor de pastas existe no Windows; cole o caminho em vez disso.",
        "picker.title": "Pasta com modelos para o PlatePilot",
        "picker.ok": "Monitorar pasta",
        "err.no_main_window": "a janela principal do Orca não foi encontrada",
        "err.open_unsupported": "abrir o projeto nesta janela do Orca só funciona no Windows",
        "err.no_executable": "o executável do OrcaSlicer em execução não foi encontrado",
        "err.no_settings": "não foi possível ler as configurações do perfil ativo do Orca",
        "err.no_printer_preset": "nenhum perfil de impressora está escolhido no Orca",
        "err.no_process_preset": "nenhum perfil de processo está escolhido no Orca",
        "warn.zip_empty": "Nenhum STL, STEP ou 3MF no arquivo",
        "warn.zip_unreadable": "Arquivo não lido: {error}",
        "err.archive_outside": "o arquivo compactado precisa estar dentro de uma pasta monitorada",
        "err.folder_outside": "a pasta de modelos precisa estar dentro de uma pasta monitorada",
        "warn.step_open": "STEP: sem pré-visualização nem layout; abra-o no Orca, que o converte sozinho",
        "warn.3mf_error": "Não foi possível ler o 3MF: {error}",
        "warn.3mf_empty": "O 3MF não contém nenhum modelo",
        "btn.open_in_orca": "Abrir no Orca",
        "msg.step_sent": "{name} enviado ao Orca: ele o abre em «Preparar».",
        "warn.unsafe_name": "nome com letras não latinas: esta versão do Orca não consegue abri-lo para os plugins (o OrcaSlicer 2.5.0 consegue); arraste-o para o PlatePilot",
        "err.unsafe_path": "{path}: o Orca não deixa os plugins abrirem caminhos com uma pasta chamada config, cert ou secret, e as versões anteriores ao OrcaSlicer 2.5.0 nenhum com letras não latinas. Arraste os arquivos para o PlatePilot ou renomeie a pasta.",
        "st.no_answer": "O plugin não respondeu. Clique em «Atualizar» para tentar de novo.",
        "st.scan_interrupted": "A última verificação das pastas não terminou (o Orca foi fechado), por isso não foi repetida automaticamente. Clique em «Atualizar» para verificar as pastas.",
        "warn.too_big": "arquivo maior que {mb} MB",
        "err.total_archive": "os arquivos STL do arquivo compactado passam de {mb} MB no total",
        "err.total_folder": "os arquivos STL da pasta passam de {mb} MB no total",
        "warn.empty_model": "modelo vazio",
        "warn.stl_error": "Erro de STL: {error}",
        "err.stl_short": "o arquivo STL é curto demais",
        "err.stl_coords": "o STL tem coordenadas inválidas",
        "err.stl_vertices": "não foi possível ler os vértices do STL",
        "err.stl_facets_limit": "o STL passa de 2 milhões de facetas",
        "err.stl_geometry": "o STL não tem geometria válida",
        "err.stl_no_triangles": "o STL não tem triângulos",
        "err.gap_too_big": "o espaço é maior que a mesa",
        "err.plan_first": "calcule primeiro o layout",
        "err.reread": "não foi possível ler o STL de novo: {name}",
        "err.facets_limit": "um projeto é limitado a {n} facetas; escolha menos peças ou use a organização do Orca",
        "msg.already_watched": "Esta pasta já é monitorada: {path}",
        "msg.inside_watched": "A pasta já está dentro da pasta monitorada {path} (a busca em subpastas está ligada).",
        "msg.replaced_nested": "Pasta adicionada; estas pastas internas não são mais necessárias separadamente: {paths}",
        "err.scan_stopped": "Busca interrompida: {error}",
        "err.source_gone": "a fonte não está mais dentro de uma pasta monitorada",
        "err.select_source": "escolha primeiro um ZIP ou uma pasta com arquivos STL",
        "err.archive_moved": "o arquivo compactado não está mais dentro de uma pasta monitorada",
        "err.stl_left_archive": "o STL não está mais no arquivo escolhido",
        "err.stl_left_folder": "o STL não está mais na pasta escolhida",
        "err.stl_limit": "o STL passa do limite de {mb} MB",
        "err.arrange_running": "a organização do Orca já está em andamento",
        "err.select_stl": "escolha pelo menos uma peça STL",
        "err.too_many_parts": "uma execução pode organizar no máximo {n} arquivos STL",
        "err.no_bed": "não foi possível ler a área de impressão da impressora ativa",
        "err.cli_failed": "A linha de comando do Orca não criou um projeto. {detail}",
        "err.cli_exit": "Código de saída: {code}",
        "msg.arranged": "O Orca organizou {parts} em {plates} e está abrindo o projeto. Se «Preparar» já tem modelos, escolha «Abrir como projeto» para manter as placas.",
        "msg.arranged_partly": "O Orca não conseguiu colocar {n} de {parts} nas placas, nem girando: elas ficam ao lado das placas. Compare o tamanho delas com a mesa ou use o layout do PlatePilot para ver quais são.",
        "err.arrange_failed": "A organização não terminou: {error}",
        "msg.picker_cancelled": "Escolha de pasta cancelada.",
        "msg.source_hidden": "A fonte foi ocultada da lista; o arquivo no disco é mantido.",
        "msg.deleted": "Excluído do PlatePilot.",
        "msg.imported": "Adicionado ao PlatePilot: {files}.",
        "import.files": "{n} arquivo|{n} arquivos",
        "import.group": "Arquivos {time}",
        "err.import_type": "{name}: só é possível adicionar arquivos ZIP, STL, STEP e 3MF",
        "err.import_size": "{name} é maior que {mb} MB",
        "err.import_state": "a transferência dos arquivos foi interrompida; adicione-os de novo",
        "err.import_path": "o nome de {name} é longo demais para a pasta do PlatePilot",
        "err.no_storage": "a pasta de dados do PlatePilot não está disponível: {error}",
        "err.not_added": "só é possível excluir fontes adicionadas ao PlatePilot",
        "err.paste_path": "{path} não é uma pasta nem um arquivo ZIP, STL, STEP ou 3MF",
        "err.part_missing": "peça não encontrada",
        "err.no_preview": "esta peça não tem dados de pré-visualização",
        "err.stl_outside": "o STL está fora da pasta monitorada",
        "err.no_printable_area": "Não foi possível ler printable_area do perfil ativo. Recarregue o perfil da impressora e tente de novo.",
        "err.plan_source": "calcule primeiro o layout da fonte escolhida",
        "err.plan_not_exportable": "corrija primeiro as peças que não cabem na mesa ou são mais altas que a impressora",
        "err.printer_changed": "a impressora ativa mudou depois do cálculo; calcule o layout de novo",
        "err.stl_changed": "um STL mudou depois do cálculo; calcule o layout de novo"
    },
    "it": {
        "btn.support": "Sostieni",
        "btn.support_tip": "Sostieni PlatePilot con le stelle di Telegram: apre t.me/GIMINOT nel browser",
        "err.support_open": "Il browser non si è aperto. Telegram dell’autore: {url}",
        "err.3mf_transform": "la posizione di un oggetto nel file è danneggiata",
        "err.3mf_nesting": "gli oggetti sono annidati troppo in profondità",
        "app.tagline": "Libreria di modelli e pianificatore di piastre",
        "app.loading_profile": "Caricamento del profilo stampante…",
        "btn.refresh": "Aggiorna",
        "btn.folders": "Impostazioni",
        "btn.add_files": "Aggiungi file",
        "btn.add_files_tip": "Aggiungi file ZIP, STL, STEP o 3MF. Vengono copiati in PlatePilot, quindi Orca non chiede il permesso.",
        "lib.title": "Libreria",
        "lib.search": "Cerca per nome",
        "lib.all": "Tutto",
        "lib.folders": "Cartelle",
        "parts.title": "Parti",
        "parts.all": "Tutte",
        "parts.none": "Nessuna",
        "view.reset": "Ripristina vista",
        "view.hint": "Trascina per ruotare · rotella per lo zoom · doppio clic per ripristinare",
        "view.empty": "Seleziona una parte STL per vederla sul piatto della stampante",
        "mode.own": "Disposizione PlatePilot",
        "mode.orca": "Disposizione di Orca",
        "mode.gap": "Distanza",
        "mode.orient": "Ruota le parti (orientamento di Orca)",
        "mode.orient_tip": "Prima di disporle, Orca ruota ogni parte come farebbe il suo «Orientamento automatico»: meno sporgenze, una base stabile. Disattivato: le parti restano come nei loro file, come le hanno pensate i progettisti.",
        "orient.checking": "Controllo di come sono appoggiate le parti… {done}/{total}",
        "orient.done": "{n} parte potrebbe stare meglio: vedi «Ruota» nell’elenco|{n} parti potrebbero stare meglio: vedi «Ruota» nell’elenco",
        "orient.advice": "meglio ruotata: {pct}% di sporgenza in meno",
        "orient.turned": "ruotata: {pct}% di sporgenza in meno",
        "btn.turn": "Ruota",
        "btn.unturn": "Annulla rotazione",
        "btn.turn_all": "Ruota tutte le suggerite ({n})",
        "btn.turn_all_tip": "Ruota ogni parte che PlatePilot ha trovato appoggiata peggio del possibile: capovolta, su un bordo stretto, con sporgenze evitabili",
        "mode.plates": "Piastre",
        "mode.plates_auto": "auto",
        "mode.plates_tip": "Su quante piastre distribuire le parti. Vuoto: il minimo in cui entrano. Oltre, le distribuisce in modo uniforme, anche se entrerebbero in una.",
        "unit.mm": "mm",
        "btn.plan": "Calcola",
        "btn.plan_smart": "Disposizione intelligente",
        "btn.plan_smart_tip": "Beta: le parti di ogni piastra sono raggruppate al centro, le piccole più vicine al centro, lontano dai bordi del piatto e a distanza uniforme; le piastre vengono riempite in modo uniforme",
        "btn.send": "Apri in Prepara",
        "btn.arrange": "Disponi in Orca",
        "mode.own_note": "PlatePilot sistema le parti in base all’ingombro sul piatto della stampante attiva, mostra le piastre e apre il progetto in «Prepara».",
        "mode.orca_note": "Le parti passano alla disposizione di Orca: usa i contorni delle parti e i profili attivi di stampante e processo, aggiunge piastre se serve e apre il progetto.",
        "drawer.title": "Impostazioni",
        "drawer.close": "Chiudi",
        "drawer.folders": "Cartelle monitorate",
        "drawer.folders_note": "PlatePilot trova archivi ZIP e cartelle con file di modelli dal loro nome. Un file viene letto solo quando lo apri.",
        "drawer.pick": "Scegli cartella…",
        "drawer.paste": "Oppure incolla il percorso di una cartella (vanno bene le virgolette di «Copia come percorso»):",
        "drawer.paste_only": "Incolla il percorso completo di una cartella con modelli:",
        "drawer.path_ph": "Percorso completo della cartella",
        "drawer.add": "Aggiungi",
        "drawer.formats": "Formati",
        "drawer.formats_note": "Le fonti senza i formati scelti sono nascoste. L’anteprima e la disposizione funzionano con STL.",
        "drawer.scan": "Ricerca",
        "drawer.recursive": "Cerca nelle sottocartelle",
        "drawer.check_on_open": "Controlla le cartelle all’apertura di PlatePilot",
        "drawer.audit_note": "Orca chiede una volta prima che PlatePilot guardi in una cartella monitorata, e una volta per ogni archivio o cartella di modelli che apri. I file trascinati in PlatePilot non richiedono permessi.",
        "drawer.preview": "Anteprima 3D",
        "drawer.gpu_preview": "Disegna con la scheda grafica (WebGL)",
        "drawer.gpu_note": "Mostra per intero le parti fino a 250 000 facce e le ruota con fluidità. Se l’anteprima sembra sbagliata, disattivala: la modalità semplice disegna fino a 30 000 facce.",
        "drawer.theme": "Tema",
        "theme.orca": "Come in Orca",
        "theme.parchment": "Pergamena",
        "theme.terracotta": "Terracotta",
        "theme.sage": "Salvia",
        "theme.blueprint": "Cianografia",
        "theme.dusk": "Crepuscolo",
        "theme.workshop": "Officina (scuro)",
        "theme.night": "Turno di notte (scuro)",
        "drawer.language": "Lingua",
        "drawer.lang_auto": "Come in Orca",
        "modal.confirm": "Conferma",
        "modal.cancel": "Annulla",
        "modal.continue": "Continua",
        "st.choose_folder": "Trascina qui file ZIP o STL, oppure apri Impostazioni e scegli una cartella con modelli.",
        "st.press_refresh": "Premi «Aggiorna» per controllare le cartelle.",
        "st.scanning": "Controllo delle cartelle…",
        "st.scanned": "Cartelle controllate · {sources}",
        "st.scanned_new": "Cartelle controllate · {sources} · {n} nuove",
        "st.importing": "Aggiunta di {name} · {i} di {n} · {pct}%",
        "st.import_none": "Niente da aggiungere: trascina file ZIP, STL, STEP o 3MF o una cartella che li contiene.",
        "st.import_skipped": " Altri file saltati: {n}.",
        "st.opening": "Apertura…",
        "st.opening_picker": "Apertura della scelta cartella…",
        "st.opened_source": "Aperto «{name}».",
        "st.plan_ready": "La disposizione è pronta. Aprila in «Prepara».",
        "st.plan_problems": "Alcune parti non entrano nel piatto. Cambia la selezione.",
        "st.building": "Creazione del progetto per Orca…",
        "st.project_opened": "Il progetto si sta aprendo in Orca. Se «Prepara» contiene già modelli, Orca chiede cosa fare: «Apri come progetto» mantiene le piastre, «Importa solo la geometria» aggiunge le parti al progetto attuale.",
        "st.project_saved": "3MF salvato: {path}. Aprilo con File > Apri progetto. {error}",
        "st.arranging": "Orca sta disponendo {n} parti e aggiunge piastre se serve…",
        "st.arrange_done": "Disposizione completata.",
        "st.plugin_error": "Errore del plugin.",
        "st.removed_source": "La fonte è stata eliminata dal disco o nascosta.",
        "printer.unreadable": "Profilo non letto",
        "printer.zones": "zone",
        "printer.zones_tip": "Il profilo stampante ha aree escluse sul piatto. Controlla la disposizione in Orca.",
        "lib.nothing_found": "Nulla corrisponde al filtro.",
        "lib.press_refresh": "Premi «Aggiorna» per trovare modelli nelle cartelle.",
        "lib.add_folder": "Trascina qui file ZIP o STL, oppure aggiungi una cartella con modelli in Impostazioni.",
        "lib.new": "Nuove",
        "lib.added": "Aggiunte",
        "lib.all_folders": "Tutte le cartelle",
        "lib.folder_tip": "Mostra solo le fonti di una cartella",
        "lib.other_show": "Altri {n} archivi il cui nome non sembra di modelli",
        "lib.other_hide": "Nascondi gli archivi il cui nome non sembra di modelli",
        "lib.other_note": "PlatePilot giudica dal nome, perché guardare dentro ogni archivio farebbe chiedere a Orca per ciascuno. Apri un archivio per controllarlo: con modelli sale, senza sparisce. Anche la ricerca li considera.",
        "lib.mark_seen": "Segna tutto come visto",
        "lib.no_new": "Nessuna nuova fonte dall’ultimo controllo.",
        "drop.title": "Rilascia per aggiungere a PlatePilot",
        "drop.note": "ZIP, STL, STEP, 3MF o una cartella che li contiene. I file vengono copiati nella cartella di PlatePilot, quindi Orca non chiede il permesso. Per monitorare invece una cartella, aggiungila in Impostazioni o incolla il percorso con Ctrl+V.",
        "src.choose": "Scegli una fonte",
        "src.choose_note": "Scegli un archivio o una cartella nella libreria. PlatePilot mostra le parti e un’anteprima sul piatto.",
        "src.summary": "{n} file di modelli · {stl} STL · {path}",
        "src.hide": "Nascondi",
        "src.hide_tip": "Nascondi dall’elenco. Il file resta sul disco.",
        "src.new": "nuova",
        "src.added": "aggiunta",
        "src.unopened": "non ancora aperta",
        "src.delete": "Elimina",
        "src.delete_tip": "Elimina la copia aggiunta a PlatePilot",
        "delete.title": "Eliminare da PlatePilot?",
        "delete.body": "La copia di «{name}» nella cartella di PlatePilot viene eliminata. I file da cui l’hai trascinata o incollata non vengono toccati.",
        "parts.meta": "{dims} mm · {facets} facce",
        "parts.catalog_only": "{type} · solo in catalogo",
        "parts.no_files": "La fonte non ha file dei formati scelti.",
        "parts.placeholder": "Qui compaiono le parti.",
        "view.meta": "{dims} mm · {facets} facce",
        "view.shown": " · {n} mostrate",
        "plan.parts": "{n} parte|{n} parti",
        "plan.plates": "{n} piastra|{n} piastre",
        "plan.not_fit": "non entra: {name}",
        "plan.at_least": "servono almeno {plates}",
        "plan.at_most": "al massimo una piastra per parte",
        "plan.too_tall": "più alta di Z: {name}",
        "plan.zones": "controlla le aree escluse in Orca",
        "plan.smart": "disposizione intelligente β",
        "plan.plate": "Piastra {n}",
        "plan.view3d": "3D",
        "plan.view3d_all": "Piastre in 3D",
        "plan.view3d_all_tip": "Vedi le piastre in 3D nell’anteprima, ogni parte come sarà nel progetto",
        "view.plate_button": "Piastra {n} di {total} in 3D",
        "view.plate_prev": "Piastra precedente",
        "view.plate_next": "Piastra successiva",
        "plan.view3d_tip": "Vedi questa piastra in 3D nell’anteprima",
        "view.plate_meta": "{parts} · piatto {bed} · semplificato",
        "st.plate_shown": "{name} in 3D: trascina per ruotarla.",
        "st.plate_loading": "{name} in 3D: parte {i} di {n}…",
        "basket.add": "Nel cestino",
        "basket.add_tip": "Metti le parti selezionate nel cestino, dove parti di più fonti vengono disposte insieme",
        "basket.show": "Cestino · {n}",
        "basket.back": "Torna alla fonte",
        "basket.empty": "Il cestino è vuoto. Seleziona parti in qualsiasi fonte e premi «Nel cestino»; qui le parti di più fonti vengono disposte insieme.",
        "basket.from": "da {name}",
        "basket.copies": "Copie di questa parte",
        "basket.remove": "Togli dal cestino",
        "basket.clear": "Svuota il cestino",
        "basket.added": "Nel cestino: {n}",
        "basket.title": "Cestino",
        "basket.and_more": "{name} e altri {n}",
        "err.basket_empty": "il cestino è vuoto",
        "basket.turned": "ruotata",
        "err.basket_gone": "«{name}» del cestino non è più dove era (spostato, modificato o eliminato); toglilo e aggiungilo di nuovo",
        "plan.plate_meta": "{parts} · {fill}%",
        "plan.rotated": " · ruotata di {n}°",
        "plan.zoom_hint": "Fai clic su una piastra per vederla da vicino",
        "plan.zoom_title": "Piastra {n} di {total}",
        "plan.prev": "Piastra precedente",
        "plan.next": "Piastra successiva",
        "plan.close": "Chiudi",
        "folders.none": "Ancora nessuna cartella.",
        "folders.remove": "Smetti di monitorare",
        "folders.sources": "{n} fonte|{n} fonti",
        "folders.stats": "{sources} · controllata {time}",
        "folders.new": "{n} nuove",
        "folders.other": "{n} archivi che non sembrano di modelli",
        "folders.empty": "{n} senza modelli nascoste",
        "folders.never": "Non ancora controllata",
        "folders.missing": "Cartella non trovata",
        "folders.skipped": "{n} sottocartelle saltate: Orca non può aprirle per i plugin",
        "folders.unreadable": "Orca non può aprire questa cartella per i plugin: il percorso ha lettere non latine o una cartella chiamata config, cert o secret",
        "folders.truncated": "sono mostrate solo le prime {n}",
        "folders.added": "Aggiunte a PlatePilot",
        "folders.added_note": "I file trascinati o incollati vengono copiati qui.",
        "err.path_empty": "inserisci il percorso di una cartella",
        "err.path_relative": "inserisci il percorso completo della cartella, per esempio {example}",
        "err.folder_missing": "cartella non trovata: {path}",
        "err.not_filesystem": "l’elemento scelto non è una cartella sul disco (per esempio «Raccolte»); scegli una cartella normale",
        "err.picker_windows": "La scelta cartella è disponibile su Windows; incolla invece il percorso.",
        "picker.title": "Cartella con modelli per PlatePilot",
        "picker.ok": "Monitora cartella",
        "err.no_main_window": "la finestra principale di Orca non è stata trovata",
        "err.open_unsupported": "aprire il progetto in questa finestra di Orca è possibile solo su Windows",
        "err.no_executable": "non è stato trovato l’eseguibile di OrcaSlicer in esecuzione",
        "err.no_settings": "impossibile leggere le impostazioni del profilo Orca attivo",
        "err.no_printer_preset": "in Orca non è scelto alcun profilo stampante",
        "err.no_process_preset": "in Orca non è scelto alcun profilo di processo",
        "warn.zip_empty": "Nessun STL, STEP o 3MF nell’archivio",
        "warn.zip_unreadable": "Archivio non letto: {error}",
        "err.archive_outside": "l’archivio deve stare in una cartella monitorata",
        "err.folder_outside": "la cartella di modelli deve stare in una cartella monitorata",
        "warn.step_open": "STEP: niente anteprima né disposizione; aprilo in Orca, che lo converte da sé",
        "warn.3mf_error": "Impossibile leggere il 3MF: {error}",
        "warn.3mf_empty": "Il 3MF non contiene alcun modello",
        "btn.open_in_orca": "Apri in Orca",
        "msg.step_sent": "{name} inviato a Orca: lo apre in «Prepara».",
        "warn.unsafe_name": "nome con lettere non latine: questa versione di Orca non può aprirlo per i plugin (OrcaSlicer 2.5.0 sì); trascinalo in PlatePilot",
        "err.unsafe_path": "{path}: Orca non permette ai plugin di aprire percorsi con una cartella chiamata config, cert o secret, e le versioni precedenti a OrcaSlicer 2.5.0 nessuno con lettere non latine. Trascina i file in PlatePilot o rinomina la cartella.",
        "st.no_answer": "Il plugin non ha risposto. Premi «Aggiorna» per riprovare.",
        "st.scan_interrupted": "L’ultimo controllo delle cartelle non è terminato (Orca è stato chiuso), quindi non è stato ripetuto automaticamente. Premi «Aggiorna» per controllare le cartelle.",
        "warn.too_big": "file più grande di {mb} MB",
        "err.total_archive": "i file STL dell’archivio superano in totale {mb} MB",
        "err.total_folder": "i file STL della cartella superano in totale {mb} MB",
        "warn.empty_model": "modello vuoto",
        "warn.stl_error": "Errore STL: {error}",
        "err.stl_short": "il file STL è troppo corto",
        "err.stl_coords": "lo STL ha coordinate non valide",
        "err.stl_vertices": "impossibile leggere i vertici dello STL",
        "err.stl_facets_limit": "lo STL supera 2 milioni di facce",
        "err.stl_geometry": "lo STL non ha una geometria valida",
        "err.stl_no_triangles": "lo STL non ha triangoli",
        "err.gap_too_big": "la distanza è più grande del piatto",
        "err.plan_first": "calcola prima la disposizione",
        "err.reread": "impossibile rileggere lo STL: {name}",
        "err.facets_limit": "un progetto è limitato a {n} facce; scegli meno parti o usa la disposizione di Orca",
        "msg.already_watched": "Questa cartella è già monitorata: {path}",
        "msg.inside_watched": "La cartella è già dentro la cartella monitorata {path} (la ricerca nelle sottocartelle è attiva).",
        "msg.replaced_nested": "Cartella aggiunta; queste cartelle interne non servono più separatamente: {paths}",
        "err.scan_stopped": "Ricerca interrotta: {error}",
        "err.source_gone": "la fonte non è più in una cartella monitorata",
        "err.select_source": "scegli prima uno ZIP o una cartella con file STL",
        "err.archive_moved": "l’archivio non è più in una cartella monitorata",
        "err.stl_left_archive": "lo STL non è più nell’archivio scelto",
        "err.stl_left_folder": "lo STL non è più nella cartella scelta",
        "err.stl_limit": "lo STL supera il limite di {mb} MB",
        "err.arrange_running": "la disposizione di Orca è già in corso",
        "err.select_stl": "scegli almeno una parte STL",
        "err.too_many_parts": "un passaggio può disporre al massimo {n} file STL",
        "err.no_bed": "impossibile leggere l’area di stampa della stampante attiva",
        "err.cli_failed": "La riga di comando di Orca non ha creato un progetto. {detail}",
        "err.cli_exit": "Codice di uscita: {code}",
        "msg.arranged": "Orca ha disposto {parts} su {plates} e sta aprendo il progetto. Se «Prepara» contiene già modelli, scegli «Apri come progetto» per mantenere le piastre.",
        "msg.arranged_partly": "Orca non è riuscito a mettere {n} di {parts} sulle piastre, neanche ruotandole: restano accanto alle piastre. Confronta le loro dimensioni con il piatto o usa la disposizione PlatePilot per vedere quali sono.",
        "err.arrange_failed": "La disposizione non è terminata: {error}",
        "msg.picker_cancelled": "Scelta della cartella annullata.",
        "msg.source_hidden": "La fonte è nascosta dall’elenco; il file sul disco resta.",
        "msg.deleted": "Eliminato da PlatePilot.",
        "msg.imported": "Aggiunto a PlatePilot: {files}.",
        "import.files": "{n} file|{n} file",
        "import.group": "File {time}",
        "err.import_type": "{name}: si possono aggiungere solo file ZIP, STL, STEP e 3MF",
        "err.import_size": "{name} supera {mb} MB",
        "err.import_state": "il trasferimento dei file si è interrotto; aggiungili di nuovo",
        "err.import_path": "il nome di {name} è troppo lungo per la cartella di PlatePilot",
        "err.no_storage": "la cartella dati di PlatePilot non è disponibile: {error}",
        "err.not_added": "si possono eliminare solo le fonti aggiunte a PlatePilot",
        "err.paste_path": "{path} non è una cartella né un file ZIP, STL, STEP o 3MF",
        "err.part_missing": "parte non trovata",
        "err.no_preview": "questa parte non ha dati di anteprima",
        "err.stl_outside": "lo STL è fuori dalla cartella monitorata",
        "err.no_printable_area": "Impossibile leggere printable_area del profilo attivo. Ricarica il profilo stampante e riprova.",
        "err.plan_source": "calcola prima la disposizione della fonte scelta",
        "err.plan_not_exportable": "correggi prima le parti che non entrano nel piatto o sono più alte della stampante",
        "err.printer_changed": "la stampante attiva è cambiata dopo il calcolo; calcola di nuovo la disposizione",
        "err.stl_changed": "uno STL è cambiato dopo il calcolo; calcola di nuovo la disposizione"
    },
    "tr": {
        "btn.support": "Destekle",
        "btn.support_tip": "PlatePilot'u Telegram Yıldızları ile destekle: tarayıcıda t.me/GIMINOT açılır",
        "err.support_open": "Tarayıcı açılmadı. Yazarın Telegram'ı: {url}",
        "err.3mf_transform": "dosyadaki bir nesnenin konumu bozuk",
        "err.3mf_nesting": "nesneler çok derin iç içe",
        "app.tagline": "Model kütüphanesi ve plaka planlayıcı",
        "app.loading_profile": "Yazıcı profili yükleniyor…",
        "btn.refresh": "Yenile",
        "btn.folders": "Ayarlar",
        "btn.add_files": "Dosya ekle",
        "btn.add_files_tip": "ZIP, STL, STEP veya 3MF dosyaları ekleyin. PlatePilot'a kopyalanırlar, bu yüzden Orca izin istemez.",
        "lib.title": "Kütüphane",
        "lib.search": "Ada göre ara",
        "lib.all": "Tümü",
        "lib.folders": "Klasörler",
        "parts.title": "Parçalar",
        "parts.all": "Tümü",
        "parts.none": "Hiçbiri",
        "view.reset": "Görünümü sıfırla",
        "view.hint": "Döndürmek için sürükleyin · yakınlaştırmak için tekerlek · sıfırlamak için çift tıklayın",
        "view.empty": "Yazıcı yatağında görmek için bir STL parçası seçin",
        "mode.own": "PlatePilot yerleşimi",
        "mode.orca": "Orca yerleşimi",
        "mode.gap": "Boşluk",
        "mode.orient": "Parçaları döndür (Orca yönlendirmesi)",
        "mode.orient_tip": "Orca yerleştirmeden önce her parçayı \"Otomatik Yönlendir\" düğmesinin yapacağı gibi döndürür: daha az çıkıntı, sağlam bir taban. Kapalı: parçalar dosyalarındaki gibi, tasarımcılarının istediği gibi durur.",
        "orient.checking": "Parçaların duruşu kontrol ediliyor… {done}/{total}",
        "orient.done": "{n} parça daha iyi durabilir: listede \"Döndür\"e bakın",
        "orient.advice": "döndürülürse daha iyi: %{pct} daha az çıkıntı",
        "orient.turned": "döndürüldü: %{pct} daha az çıkıntı",
        "btn.turn": "Döndür",
        "btn.unturn": "Döndürmeyi geri al",
        "btn.turn_all": "Önerilenlerin hepsini döndür ({n})",
        "btn.turn_all_tip": "PlatePilot'un olabileceğinden kötü duruyor bulduğu her parçayı döndürün: baş aşağı, dar bir kenar üzerinde, önlenebilir çıkıntılarla",
        "mode.plates": "Plakalar",
        "mode.plates_auto": "otomatik",
        "mode.plates_tip": "Parçaların kaç plakaya dağıtılacağı. Boş: sığabilecekleri en az sayı. Daha fazlası, tek plakaya sığsalar bile onları eşit dağıtır.",
        "unit.mm": "mm",
        "btn.plan": "Hesapla",
        "btn.plan_smart": "Akıllı yerleşim",
        "btn.plan_smart_tip": "Beta: her plakanın parçaları ortada toplanır, küçükler merkeze en yakın, yatak kenarlarından uzak ve eşit aralıklı; plakalar eşit doldurulur",
        "btn.send": "Hazırlık'ta aç",
        "btn.arrange": "Orca'da hizala",
        "mode.own_note": "PlatePilot parçaları etkin yazıcının yatağında taban alanlarına göre yerleştirir, plakaları gösterir ve projeyi \"Hazırlık\" sekmesinde açar.",
        "mode.orca_note": "Parçalar Orca'nın kendi hizalamasına gider: parça hatlarını ve etkin yazıcı ve işlem profillerini kullanır, gerekirse plaka ekler ve projeyi açar.",
        "drawer.title": "Ayarlar",
        "drawer.close": "Kapat",
        "drawer.folders": "İzlenen klasörler",
        "drawer.folders_note": "PlatePilot, ZIP arşivlerini ve model dosyası içeren klasörleri adlarından bulur. Bir dosya yalnızca siz açtığınızda okunur.",
        "drawer.pick": "Klasör seç…",
        "drawer.paste": "Ya da bir klasör yolu yapıştırın (\"Yol olarak kopyala\"daki tırnak işaretleri sorun değil):",
        "drawer.paste_only": "Modellerin bulunduğu klasörün tam yolunu yapıştırın:",
        "drawer.path_ph": "Tam klasör yolu",
        "drawer.add": "Ekle",
        "drawer.formats": "Biçimler",
        "drawer.formats_note": "Seçili biçimleri içermeyen kaynaklar gizlenir. Ön izleme ve yerleşim STL ile çalışır.",
        "drawer.scan": "Tarama",
        "drawer.recursive": "Alt klasörlerde ara",
        "drawer.check_on_open": "PlatePilot açılırken klasörleri kontrol et",
        "drawer.audit_note": "Orca, PlatePilot izlenen bir klasöre bakmadan önce bir kez ve açtığınız her arşiv ya da model klasörü için bir kez sorar. PlatePilot'a sürüklenen dosyalar izin gerektirmez.",
        "drawer.preview": "3B ön izleme",
        "drawer.gpu_preview": "Ekran kartıyla çiz (WebGL)",
        "drawer.gpu_note": "250 000 yüzeye kadar parçaları tam gösterir ve akıcı döndürür. Ön izleme yanlış görünürse kapatın: basit mod 30 000 yüzeye kadar çizer.",
        "drawer.theme": "Tema",
        "theme.orca": "Orca'daki gibi",
        "theme.parchment": "Parşömen",
        "theme.terracotta": "Terakota",
        "theme.sage": "Adaçayı",
        "theme.blueprint": "Mavi kopya",
        "theme.dusk": "Alacakaranlık",
        "theme.workshop": "Atölye (koyu)",
        "theme.night": "Gece vardiyası (koyu)",
        "drawer.language": "Dil",
        "drawer.lang_auto": "Orca'daki gibi",
        "modal.confirm": "Onayla",
        "modal.cancel": "İptal",
        "modal.continue": "Devam et",
        "st.choose_folder": "ZIP veya STL dosyalarını buraya bırakın ya da Ayarlar'ı açıp modellerin bulunduğu bir klasör seçin.",
        "st.press_refresh": "Klasörleri taramak için \"Yenile\"ye basın.",
        "st.scanning": "Klasörler taranıyor…",
        "st.scanned": "Klasörler kontrol edildi · {sources}",
        "st.scanned_new": "Klasörler kontrol edildi · {sources} · {n} yeni",
        "st.importing": "{name} ekleniyor · {i}/{n} · %{pct}",
        "st.import_none": "Eklenecek bir şey yok: ZIP, STL, STEP veya 3MF dosyalarını ya da bunları içeren bir klasörü bırakın.",
        "st.import_skipped": " Atlanan diğer dosyalar: {n}.",
        "st.opening": "Açılıyor…",
        "st.opening_picker": "Klasör seçici açılıyor…",
        "st.opened_source": "\"{name}\" açıldı.",
        "st.plan_ready": "Yerleşim hazır. \"Hazırlık\" sekmesinde açın.",
        "st.plan_problems": "Bazı parçalar yatağa sığmıyor. Seçimi değiştirin.",
        "st.building": "Orca için proje oluşturuluyor…",
        "st.project_opened": "Proje Orca'da açılıyor. \"Hazırlık\"ta zaten modeller varsa Orca ne yapılacağını sorar: \"Proje olarak aç\" plakaları korur, \"Yalnızca geometriyi içe aktar\" parçaları mevcut projeye ekler.",
        "st.project_saved": "3MF kaydedildi: {path}. Dosya > Projeyi aç ile açın. {error}",
        "st.arranging": "Orca {n} parçayı hizalıyor ve gerekirse plaka ekliyor…",
        "st.arrange_done": "Hizalama tamamlandı.",
        "st.plugin_error": "Eklenti hatası.",
        "st.removed_source": "Kaynak diskten silindi veya gizlendi.",
        "printer.unreadable": "Profil okunamadı",
        "printer.zones": "bölgeler",
        "printer.zones_tip": "Yazıcı profilinde yatağın hariç tutulan alanları var. Yerleşimi Orca'da kontrol edin.",
        "lib.nothing_found": "Filtreye uyan bir şey yok.",
        "lib.press_refresh": "Klasörlerdeki modelleri bulmak için \"Yenile\"ye basın.",
        "lib.add_folder": "ZIP veya STL dosyalarını buraya bırakın ya da Ayarlar'da modellerin bulunduğu bir klasör ekleyin.",
        "lib.new": "Yeni",
        "lib.added": "Eklenen",
        "lib.all_folders": "Tüm klasörler",
        "lib.folder_tip": "Yalnızca bir klasörün kaynaklarını göster",
        "lib.other_show": "Adı model gibi görünmeyen {n} arşiv daha",
        "lib.other_hide": "Adı model gibi görünmeyen arşivleri gizle",
        "lib.other_note": "PlatePilot ada göre karar verir, çünkü her arşivin içine bakmak Orca'nın her biri için sormasına yol açardı. Kontrol etmek için bir arşivi açın: model varsa yukarı çıkar, yoksa kaybolur. Arama bunlarda da arar.",
        "lib.mark_seen": "Tümünü görüldü olarak işaretle",
        "lib.no_new": "Son kontrolden bu yana yeni kaynak yok.",
        "drop.title": "PlatePilot'a eklemek için bırakın",
        "drop.note": "ZIP, STL, STEP, 3MF veya bunları içeren bir klasör. Dosyalar PlatePilot'un kendi klasörüne kopyalanır, bu yüzden Orca izin istemez. Bunun yerine bir klasörü izlemek için onu Ayarlar'da ekleyin veya yolunu Ctrl+V ile yapıştırın.",
        "src.choose": "Bir kaynak seçin",
        "src.choose_note": "Kütüphanede bir arşiv veya klasör seçin. PlatePilot parçalarını ve yatakta bir ön izlemesini gösterir.",
        "src.summary": "{n} model dosyası · {stl} STL · {path}",
        "src.hide": "Gizle",
        "src.hide_tip": "Listeden gizle. Dosya diskte kalır.",
        "src.new": "yeni",
        "src.added": "eklendi",
        "src.unopened": "henüz açılmadı",
        "src.delete": "Sil",
        "src.delete_tip": "PlatePilot'a eklenen kopyayı sil",
        "delete.title": "PlatePilot'tan silinsin mi?",
        "delete.body": "\"{name}\" dosyasının PlatePilot klasöründeki kopyası silinir. Sürüklediğiniz veya yapıştırdığınız dosyalara dokunulmaz.",
        "parts.meta": "{dims} mm · {facets} yüzey",
        "parts.catalog_only": "{type} · yalnızca katalog",
        "parts.no_files": "Kaynakta seçili biçimlerde dosya yok.",
        "parts.placeholder": "Parçalar burada görünür.",
        "view.meta": "{dims} mm · {facets} yüzey",
        "view.shown": " · {n} gösteriliyor",
        "plan.parts": "{n} parça",
        "plan.plates": "{n} plaka",
        "plan.not_fit": "sığmıyor: {name}",
        "plan.at_least": "en az {plates} gerekiyor",
        "plan.at_most": "parça başına en fazla bir plaka",
        "plan.too_tall": "Z'den yüksek: {name}",
        "plan.zones": "Orca'da hariç tutulan alanları kontrol edin",
        "plan.smart": "akıllı yerleşim β",
        "plan.plate": "Plaka {n}",
        "plan.view3d": "3B",
        "plan.view3d_all": "Plakalar 3B",
        "plan.view3d_all_tip": "Plakaları ön izlemede 3B olarak görün, her parça projede olacağı gibi",
        "view.plate_button": "Plaka {n}/{total} 3B",
        "view.plate_prev": "Önceki plaka",
        "view.plate_next": "Sonraki plaka",
        "plan.view3d_tip": "Bu plakayı ön izlemede 3B olarak görün",
        "view.plate_meta": "{parts} · yatak {bed} · sadeleştirilmiş",
        "st.plate_shown": "{name} 3B: döndürmek için sürükleyin.",
        "st.plate_loading": "{name} 3B: parça {i}/{n}…",
        "basket.add": "Sepete",
        "basket.add_tip": "İşaretli parçaları, birden çok kaynaktan parçaların birlikte yerleştirildiği sepete koyun",
        "basket.show": "Sepet · {n}",
        "basket.back": "Kaynağa dön",
        "basket.empty": "Sepet boş. Herhangi bir kaynakta parçaları işaretleyip \"Sepete\"ye basın; burada birden çok kaynaktan parçalar birlikte yerleştirilir.",
        "basket.from": "{name} kaynağından",
        "basket.copies": "Bu parçanın kopyaları",
        "basket.remove": "Sepetten çıkar",
        "basket.clear": "Sepeti boşalt",
        "basket.added": "Sepette: {n}",
        "basket.title": "Sepet",
        "basket.and_more": "{name} ve {n} tane daha",
        "err.basket_empty": "sepet boş",
        "basket.turned": "döndürüldü",
        "err.basket_gone": "sepetteki \"{name}\" artık eski yerinde değil (taşındı, değişti veya silindi); çıkarıp yeniden ekleyin",
        "plan.plate_meta": "{parts} · %{fill}",
        "plan.rotated": " · {n}° döndürüldü",
        "plan.zoom_hint": "Yakından görmek için bir plakaya tıklayın",
        "plan.zoom_title": "Plaka {n}/{total}",
        "plan.prev": "Önceki plaka",
        "plan.next": "Sonraki plaka",
        "plan.close": "Kapat",
        "folders.none": "Henüz klasör yok.",
        "folders.remove": "İzlemeyi bırak",
        "folders.sources": "{n} kaynak",
        "folders.stats": "{sources} · {time} kontrol edildi",
        "folders.new": "{n} yeni",
        "folders.other": "model gibi görünmeyen {n} arşiv",
        "folders.empty": "modelsiz {n} kaynak gizlendi",
        "folders.never": "Henüz kontrol edilmedi",
        "folders.missing": "Klasör bulunamadı",
        "folders.skipped": "{n} alt klasör atlandı: Orca bunları eklentiler için açamıyor",
        "folders.unreadable": "Orca bu klasörü eklentiler için açamıyor: yolunda Latin olmayan harfler ya da config, cert veya secret adlı bir klasör var",
        "folders.truncated": "yalnızca ilk {n} tanesi gösteriliyor",
        "folders.added": "PlatePilot'a eklenenler",
        "folders.added_note": "Sürüklediğiniz veya yapıştırdığınız dosyalar buraya kopyalanır.",
        "err.path_empty": "bir klasör yolu girin",
        "err.path_relative": "tam klasör yolunu girin, örneğin {example}",
        "err.folder_missing": "klasör bulunamadı: {path}",
        "err.not_filesystem": "seçilen öğe diskte bir klasör değil (örneğin \"Kitaplıklar\"); normal bir klasör seçin",
        "err.picker_windows": "Klasör seçici Windows'ta kullanılabilir; bunun yerine yolu yapıştırın.",
        "picker.title": "PlatePilot için modellerin bulunduğu klasör",
        "picker.ok": "Klasörü izle",
        "err.no_main_window": "Orca ana penceresi bulunamadı",
        "err.open_unsupported": "projeyi bu Orca penceresinde açmak yalnızca Windows'ta mümkün",
        "err.no_executable": "çalışan OrcaSlicer programı bulunamadı",
        "err.no_settings": "etkin Orca profilinin ayarları okunamadı",
        "err.no_printer_preset": "Orca'da yazıcı profili seçili değil",
        "err.no_process_preset": "Orca'da işlem profili seçili değil",
        "warn.zip_empty": "Arşivde STL, STEP veya 3MF yok",
        "warn.zip_unreadable": "Arşiv okunamadı: {error}",
        "err.archive_outside": "arşiv izlenen bir klasörün içinde olmalı",
        "err.folder_outside": "model klasörü izlenen bir klasörün içinde olmalı",
        "warn.step_open": "STEP: ön izleme veya yerleşim yok; onu kendisi dönüştüren Orca'da açın",
        "warn.3mf_error": "3MF okunamadı: {error}",
        "warn.3mf_empty": "3MF model içermiyor",
        "btn.open_in_orca": "Orca'da aç",
        "msg.step_sent": "{name} Orca'ya gönderildi: \"Hazırlık\" sekmesinde açıyor.",
        "warn.unsafe_name": "Latin olmayan harfler içeren ad: bu Orca sürümü onu eklentiler için açamıyor (OrcaSlicer 2.5.0 açabiliyor); PlatePilot'a sürükleyin",
        "err.unsafe_path": "{path}: Orca, eklentilerin config, cert veya secret adlı klasör içeren yolları açmasına izin vermez; OrcaSlicer 2.5.0 öncesi sürümler ise Latin olmayan harfli hiçbir yolu açmaz. Dosyaları PlatePilot'a sürükleyin veya klasörü yeniden adlandırın.",
        "st.no_answer": "Eklenti yanıt vermedi. Yeniden denemek için \"Yenile\"ye basın.",
        "st.scan_interrupted": "Son klasör kontrolü bitmedi (Orca kapatıldı), bu yüzden otomatik olarak tekrarlanmadı. Klasörleri kontrol etmek için \"Yenile\"ye basın.",
        "warn.too_big": "{mb} MB'den büyük dosya",
        "err.total_archive": "arşivdeki STL dosyaları toplamda {mb} MB'yi aşıyor",
        "err.total_folder": "klasördeki STL dosyaları toplamda {mb} MB'yi aşıyor",
        "warn.empty_model": "boş model",
        "warn.stl_error": "STL hatası: {error}",
        "err.stl_short": "STL dosyası çok kısa",
        "err.stl_coords": "STL'de geçersiz koordinatlar var",
        "err.stl_vertices": "STL köşe noktaları okunamadı",
        "err.stl_facets_limit": "STL 2 milyon yüzeyi aşıyor",
        "err.stl_geometry": "STL'de geçerli geometri yok",
        "err.stl_no_triangles": "STL'de üçgen yok",
        "err.gap_too_big": "boşluk yataktan büyük",
        "err.plan_first": "önce yerleşimi hesaplayın",
        "err.reread": "STL yeniden okunamadı: {name}",
        "err.facets_limit": "bir proje {n} yüzeyle sınırlıdır; daha az parça seçin veya Orca yerleşimini kullanın",
        "msg.already_watched": "Bu klasör zaten izleniyor: {path}",
        "msg.inside_watched": "Klasör zaten izlenen {path} klasörünün içinde (alt klasör araması açık).",
        "msg.replaced_nested": "Klasör eklendi; şu iç içe klasörlere artık ayrıca gerek yok: {paths}",
        "err.scan_stopped": "Tarama durdu: {error}",
        "err.source_gone": "kaynak artık izlenen bir klasörün içinde değil",
        "err.select_source": "önce bir ZIP veya STL dosyaları içeren bir klasör seçin",
        "err.archive_moved": "arşiv artık izlenen bir klasörün içinde değil",
        "err.stl_left_archive": "STL artık seçili arşivde değil",
        "err.stl_left_folder": "STL artık seçili klasörde değil",
        "err.stl_limit": "STL {mb} MB sınırını aşıyor",
        "err.arrange_running": "Orca yerleşimi zaten çalışıyor",
        "err.select_stl": "en az bir STL parçası seçin",
        "err.too_many_parts": "bir çalıştırmada en fazla {n} STL dosyası hizalanabilir",
        "err.no_bed": "etkin yazıcının yazdırılabilir alanı okunamadı",
        "err.cli_failed": "Orca komut satırı proje oluşturmadı. {detail}",
        "err.cli_exit": "Çıkış kodu: {code}",
        "msg.arranged": "Orca yerleştirdi: {parts}, {plates}; proje açılıyor. \"Hazırlık\"ta zaten modeller varsa plakaları korumak için \"Proje olarak aç\"ı seçin.",
        "msg.arranged_partly": "Orca {parts} içinden {n} tanesini döndürse bile plakalara yerleştiremedi: plakaların yanında kaldılar. Boyutlarını yatakla karşılaştırın veya hangileri olduğunu görmek için PlatePilot yerleşimini kullanın.",
        "err.arrange_failed": "Hizalama tamamlanmadı: {error}",
        "msg.picker_cancelled": "Klasör seçimi iptal edildi.",
        "msg.source_hidden": "Kaynak listeden gizlendi; diskteki dosya korunuyor.",
        "msg.deleted": "PlatePilot'tan silindi.",
        "msg.imported": "PlatePilot'a eklendi: {files}.",
        "import.files": "{n} dosya",
        "import.group": "Dosyalar {time}",
        "err.import_type": "{name}: yalnızca ZIP, STL, STEP ve 3MF dosyaları eklenebilir",
        "err.import_size": "{name} {mb} MB'den büyük",
        "err.import_state": "dosya aktarımı kesildi; dosyaları yeniden ekleyin",
        "err.import_path": "{name} adı PlatePilot klasörü için çok uzun",
        "err.no_storage": "PlatePilot veri klasörü kullanılamıyor: {error}",
        "err.not_added": "yalnızca PlatePilot'a eklenen kaynaklar silinebilir",
        "err.paste_path": "{path} bir klasör ya da ZIP, STL, STEP veya 3MF dosyası değil",
        "err.part_missing": "parça bulunamadı",
        "err.no_preview": "bu parçanın ön izleme verisi yok",
        "err.stl_outside": "STL izlenen klasörün dışında",
        "err.no_printable_area": "Etkin profilin printable_area değeri okunamadı. Yazıcı profilini yeniden yükleyip tekrar deneyin.",
        "err.plan_source": "önce seçili kaynağın yerleşimini hesaplayın",
        "err.plan_not_exportable": "önce yatağa sığmayan veya yazıcıdan yüksek olan parçaları düzeltin",
        "err.printer_changed": "hesaplamadan sonra etkin yazıcı değişti; yerleşimi yeniden hesaplayın",
        "err.stl_changed": "hesaplamadan sonra bir STL değişti; yerleşimi yeniden hesaplayın"
    },
    "ja": {
        "btn.support": "応援する",
        "btn.support_tip": "Telegram スターで PlatePilot を応援：ブラウザーで t.me/GIMINOT を開きます",
        "err.support_open": "ブラウザーが開きませんでした。作者の Telegram：{url}",
        "err.3mf_transform": "ファイル内のオブジェクトの位置情報が壊れています",
        "err.3mf_nesting": "オブジェクトの入れ子が深すぎます",
        "app.tagline": "モデルライブラリとプレート計画",
        "app.loading_profile": "プリンタープロファイルを読み込み中…",
        "btn.refresh": "更新",
        "btn.folders": "設定",
        "btn.add_files": "ファイルを追加",
        "btn.add_files_tip": "ZIP、STL、STEP、3MF ファイルを追加します。PlatePilot にコピーされるので、Orca は許可を求めません。",
        "lib.title": "ライブラリ",
        "lib.search": "名前で検索",
        "lib.all": "すべて",
        "lib.folders": "フォルダー",
        "parts.title": "パーツ",
        "parts.all": "すべて",
        "parts.none": "なし",
        "view.reset": "表示をリセット",
        "view.hint": "ドラッグで回転 · ホイールで拡大縮小 · ダブルクリックでリセット",
        "view.empty": "STL パーツを選ぶとプリンターのベッド上に表示されます",
        "mode.own": "PlatePilot の配置",
        "mode.orca": "Orca のレイアウト",
        "mode.gap": "間隔",
        "mode.orient": "パーツを回転（Orca の自動向き調整）",
        "mode.orient_tip": "配置の前に、Orca が「自動向き調整」と同じように各パーツを回転します：オーバーハングが減り、底面が安定します。オフ：パーツはファイルのまま、設計者の意図どおりに置かれます。",
        "orient.checking": "パーツの置き方を確認中… {done}/{total}",
        "orient.done": "{n} 個のパーツはもっと良い向きにできます：リストの「回転」を参照",
        "orient.advice": "回転するとより良い：オーバーハング {pct}% 減",
        "orient.turned": "回転済み：オーバーハング {pct}% 減",
        "btn.turn": "回転",
        "btn.unturn": "回転を戻す",
        "btn.turn_all": "提案をすべて回転（{n}）",
        "btn.turn_all_tip": "PlatePilot が本来より悪い向きと判断したパーツをすべて回転します：上下逆、細い縁で立っている、避けられるオーバーハングがある",
        "mode.plates": "プレート",
        "mode.plates_auto": "自動",
        "mode.plates_tip": "パーツを何枚のプレートに分けるか。空欄：入る最少枚数。それより多いと、1 枚に入る場合でも均等に分けます。",
        "unit.mm": "mm",
        "btn.plan": "計算",
        "btn.plan_smart": "スマート配置",
        "btn.plan_smart_tip": "ベータ：各プレートのパーツを中央に集め、小さいものほど中心寄り、ベッドの端から離して均等な間隔にします。プレートは均等に埋めます",
        "btn.send": "準備で開く",
        "btn.arrange": "Orca でレイアウト",
        "mode.own_note": "PlatePilot はアクティブなプリンターのベッド上にパーツを底面の大きさで並べ、プレートを表示し、プロジェクトを「準備」で開きます。",
        "mode.orca_note": "パーツは Orca 自身のレイアウトに渡されます：パーツの輪郭とアクティブなプリンター・プロセスのプロファイルを使い、必要に応じてプレートを追加してプロジェクトを開きます。",
        "drawer.title": "設定",
        "drawer.close": "閉じる",
        "drawer.folders": "監視フォルダー",
        "drawer.folders_note": "PlatePilot は ZIP アーカイブとモデルファイルのあるフォルダーを名前から見つけます。ファイルは開いたときにだけ読み込まれます。",
        "drawer.pick": "フォルダーを選択…",
        "drawer.paste": "またはフォルダーのパスを貼り付け（「パスのコピー」の引用符もそのままで可）：",
        "drawer.paste_only": "モデルのあるフォルダーのフルパスを貼り付け：",
        "drawer.path_ph": "フォルダーのフルパス",
        "drawer.add": "追加",
        "drawer.formats": "形式",
        "drawer.formats_note": "選んだ形式を含まないソースは非表示になります。プレビューと配置は STL で動作します。",
        "drawer.scan": "スキャン",
        "drawer.recursive": "サブフォルダーも検索",
        "drawer.check_on_open": "PlatePilot を開いたときにフォルダーを確認",
        "drawer.audit_note": "PlatePilot が監視フォルダーを見る前に Orca が 1 回、開くアーカイブやモデルフォルダーごとに 1 回確認します。PlatePilot にドロップしたファイルは許可不要です。",
        "drawer.preview": "3D プレビュー",
        "drawer.gpu_preview": "グラフィックカードで描画（WebGL）",
        "drawer.gpu_note": "250 000 面までのパーツを完全に表示し、滑らかに回転します。表示がおかしい場合はオフにしてください：簡易モードは 30 000 面まで描画します。",
        "drawer.theme": "テーマ",
        "theme.orca": "Orca と同じ",
        "theme.parchment": "羊皮紙",
        "theme.terracotta": "テラコッタ",
        "theme.sage": "セージ",
        "theme.blueprint": "青図",
        "theme.dusk": "夕暮れ",
        "theme.workshop": "工房（ダーク）",
        "theme.night": "夜勤（ダーク）",
        "drawer.language": "言語",
        "drawer.lang_auto": "Orca と同じ",
        "modal.confirm": "確認",
        "modal.cancel": "取消し",
        "modal.continue": "続行",
        "st.choose_folder": "ZIP や STL をここにドロップするか、設定を開いてモデルのあるフォルダーを選んでください。",
        "st.press_refresh": "「更新」を押してフォルダーをスキャンします。",
        "st.scanning": "フォルダーをスキャン中…",
        "st.scanned": "フォルダー確認済み · {sources}",
        "st.scanned_new": "フォルダー確認済み · {sources} · 新規 {n}",
        "st.importing": "{name} を追加中 · {i}/{n} · {pct}%",
        "st.import_none": "追加するものがありません：ZIP、STL、STEP、3MF ファイルまたはそれらを含むフォルダーをドロップしてください。",
        "st.import_skipped": " その他のファイルをスキップ：{n}。",
        "st.opening": "開いています…",
        "st.opening_picker": "フォルダー選択を開いています…",
        "st.opened_source": "「{name}」を開きました。",
        "st.plan_ready": "配置ができました。「準備」で開いてください。",
        "st.plan_problems": "ベッドに入らないパーツがあります。選択を変えてください。",
        "st.building": "Orca 用のプロジェクトを作成中…",
        "st.project_opened": "プロジェクトを Orca で開いています。「準備」に既にモデルがある場合、Orca が処理を尋ねます：「プロジェクトとして開く」はプレートを保持し、「ジオメトリのみをインポート」は現在のプロジェクトにパーツを追加します。",
        "st.project_saved": "3MF を保存しました：{path}。ファイル > プロジェクトを開く で開いてください。{error}",
        "st.arranging": "Orca が {n} 個のパーツをレイアウトし、必要に応じてプレートを追加しています…",
        "st.arrange_done": "レイアウトが完了しました。",
        "st.plugin_error": "プラグインのエラー。",
        "st.removed_source": "ソースはディスクから削除されたか、非表示になりました。",
        "printer.unreadable": "プロファイルを読めません",
        "printer.zones": "除外領域",
        "printer.zones_tip": "プリンタープロファイルにベッドの除外領域があります。Orca で配置を確認してください。",
        "lib.nothing_found": "フィルターに一致するものがありません。",
        "lib.press_refresh": "「更新」を押してフォルダー内のモデルを探します。",
        "lib.add_folder": "ZIP や STL をここにドロップするか、設定でモデルのあるフォルダーを追加してください。",
        "lib.new": "新規",
        "lib.added": "追加済み",
        "lib.all_folders": "すべてのフォルダー",
        "lib.folder_tip": "1 つのフォルダーのソースだけを表示",
        "lib.other_show": "名前がモデルらしくないアーカイブがあと {n} 個",
        "lib.other_hide": "名前がモデルらしくないアーカイブを隠す",
        "lib.other_note": "PlatePilot は名前で判断します。すべてのアーカイブの中を見ると、Orca が 1 つずつ確認を求めるためです。アーカイブを開いて確認してください：モデルがあれば上に移り、なければ消えます。検索はこれらも対象にします。",
        "lib.mark_seen": "すべて既読にする",
        "lib.no_new": "前回の確認以降、新しいソースはありません。",
        "drop.title": "ドロップして PlatePilot に追加",
        "drop.note": "ZIP、STL、STEP、3MF またはそれらを含むフォルダー。ファイルは PlatePilot 専用のフォルダーにコピーされるので、Orca は許可を求めません。代わりにフォルダーを監視するには、設定で追加するか Ctrl+V でパスを貼り付けてください。",
        "src.choose": "ソースを選択",
        "src.choose_note": "ライブラリでアーカイブかフォルダーを選んでください。PlatePilot がパーツとベッド上のプレビューを表示します。",
        "src.summary": "モデルファイル {n} · STL {stl} · {path}",
        "src.hide": "隠す",
        "src.hide_tip": "リストから隠します。ファイルはディスクに残ります。",
        "src.new": "新規",
        "src.added": "追加済み",
        "src.unopened": "未開封",
        "src.delete": "削除",
        "src.delete_tip": "PlatePilot に追加したコピーを削除",
        "delete.title": "PlatePilot から削除しますか？",
        "delete.body": "PlatePilot のフォルダーにある「{name}」のコピーを削除します。ドロップや貼り付けの元のファイルには触れません。",
        "parts.meta": "{dims} mm · {facets} 面",
        "parts.catalog_only": "{type} · カタログのみ",
        "parts.no_files": "このソースには選んだ形式のファイルがありません。",
        "parts.placeholder": "ここにパーツが表示されます。",
        "view.meta": "{dims} mm · {facets} 面",
        "view.shown": " · {n} 面を表示",
        "plan.parts": "{n} 個のパーツ",
        "plan.plates": "{n} 枚のプレート",
        "plan.not_fit": "入りません：{name}",
        "plan.at_least": "少なくとも {plates} が必要",
        "plan.at_most": "パーツ 1 個につきプレートは最大 1 枚",
        "plan.too_tall": "Z より高い：{name}",
        "plan.zones": "Orca で除外領域を確認してください",
        "plan.smart": "スマート配置 β",
        "plan.plate": "プレート {n}",
        "plan.view3d": "3D",
        "plan.view3d_all": "プレートを 3D で",
        "plan.view3d_all_tip": "プレビューでプレートを 3D 表示します。各パーツはプロジェクトと同じ配置です",
        "view.plate_button": "プレート {n}/{total} を 3D で",
        "view.plate_prev": "前のプレート",
        "view.plate_next": "次のプレート",
        "plan.view3d_tip": "このプレートをプレビューで 3D 表示",
        "view.plate_meta": "{parts} · ベッド {bed} · 簡略表示",
        "st.plate_shown": "{name} を 3D 表示：ドラッグで回転できます。",
        "st.plate_loading": "{name} を 3D 表示：パーツ {i}/{n}…",
        "basket.add": "かごに入れる",
        "basket.add_tip": "チェックしたパーツをかごに入れます。かごでは複数のソースのパーツをまとめて配置できます",
        "basket.show": "かご · {n}",
        "basket.back": "ソースに戻る",
        "basket.empty": "かごは空です。任意のソースでパーツにチェックを入れ「かごに入れる」を押してください。ここでは複数のソースのパーツをまとめて配置できます。",
        "basket.from": "{name} から",
        "basket.copies": "このパーツの個数",
        "basket.remove": "かごから出す",
        "basket.clear": "かごを空にする",
        "basket.added": "かごの中：{n}",
        "basket.title": "かご",
        "basket.and_more": "{name} ほか {n} 件",
        "err.basket_empty": "かごが空です",
        "basket.turned": "回転済み",
        "err.basket_gone": "かごの「{name}」が元の場所にありません（移動、変更、または削除されました）。かごから出して、もう一度追加してください",
        "plan.plate_meta": "{parts} · {fill}%",
        "plan.rotated": " · {n}° 回転",
        "plan.zoom_hint": "プレートをクリックすると拡大表示します",
        "plan.zoom_title": "プレート {n}/{total}",
        "plan.prev": "前のプレート",
        "plan.next": "次のプレート",
        "plan.close": "閉じる",
        "folders.none": "フォルダーはまだありません。",
        "folders.remove": "監視をやめる",
        "folders.sources": "{n} 件のソース",
        "folders.stats": "{sources} · {time} に確認",
        "folders.new": "新規 {n}",
        "folders.other": "モデルらしくないアーカイブ {n} 個",
        "folders.empty": "モデルのない {n} 件を非表示",
        "folders.never": "未確認",
        "folders.missing": "フォルダーが見つかりません",
        "folders.skipped": "サブフォルダー {n} 個をスキップ：Orca がプラグイン用に開けません",
        "folders.unreadable": "Orca はこのフォルダーをプラグイン用に開けません：パスにラテン文字以外の文字、または config、cert、secret という名前のフォルダーが含まれています",
        "folders.truncated": "最初の {n} 件のみ表示",
        "folders.added": "PlatePilot に追加",
        "folders.added_note": "ドロップや貼り付けたファイルはここにコピーされます。",
        "err.path_empty": "フォルダーのパスを入力してください",
        "err.path_relative": "フォルダーのフルパスを入力してください。例：{example}",
        "err.folder_missing": "フォルダーが見つかりません：{path}",
        "err.not_filesystem": "選んだ項目はディスク上のフォルダーではありません（例：「ライブラリ」）。通常のフォルダーを選んでください",
        "err.picker_windows": "フォルダー選択は Windows で使えます。代わりにパスを貼り付けてください。",
        "picker.title": "PlatePilot 用のモデルフォルダー",
        "picker.ok": "フォルダーを監視",
        "err.no_main_window": "Orca のメインウィンドウが見つかりません",
        "err.open_unsupported": "この Orca ウィンドウでプロジェクトを開けるのは Windows のみです",
        "err.no_executable": "実行中の OrcaSlicer の実行ファイルが見つかりません",
        "err.no_settings": "アクティブな Orca プロファイルの設定を読めませんでした",
        "err.no_printer_preset": "Orca でプリンタープロファイルが選ばれていません",
        "err.no_process_preset": "Orca でプロセスプロファイルが選ばれていません",
        "warn.zip_empty": "アーカイブに STL、STEP、3MF がありません",
        "warn.zip_unreadable": "アーカイブを読めません：{error}",
        "err.archive_outside": "アーカイブは監視フォルダー内にある必要があります",
        "err.folder_outside": "モデルフォルダーは監視フォルダー内にある必要があります",
        "warn.step_open": "STEP：プレビューと配置は不可。Orca で開くと Orca 自身が変換します",
        "warn.3mf_error": "3MF を読めませんでした：{error}",
        "warn.3mf_empty": "3MF にモデルがありません",
        "btn.open_in_orca": "Orca で開く",
        "msg.step_sent": "{name} を Orca に送りました：「準備」で開きます。",
        "warn.unsafe_name": "ラテン文字以外を含む名前：この Orca ではプラグイン用に開けません（OrcaSlicer 2.5.0 は可能）。PlatePilot にドラッグしてください",
        "err.unsafe_path": "{path}：Orca はプラグインが config、cert、secret という名前のフォルダーを含むパスを開くことを許可しません。また OrcaSlicer 2.5.0 より前のバージョンはラテン文字以外を含むパスを開けません。ファイルを PlatePilot にドラッグするか、フォルダー名を変更してください。",
        "st.no_answer": "プラグインが応答しません。「更新」を押してもう一度試してください。",
        "st.scan_interrupted": "前回のフォルダー確認が完了しなかったため（Orca が閉じられた）、自動では繰り返していません。「更新」を押してフォルダーを確認してください。",
        "warn.too_big": "{mb} MB を超えるファイル",
        "err.total_archive": "アーカイブ内の STL ファイルの合計が {mb} MB を超えています",
        "err.total_folder": "フォルダー内の STL ファイルの合計が {mb} MB を超えています",
        "warn.empty_model": "空のモデル",
        "warn.stl_error": "STL エラー：{error}",
        "err.stl_short": "STL ファイルが短すぎます",
        "err.stl_coords": "STL の座標が無効です",
        "err.stl_vertices": "STL の頂点を読めませんでした",
        "err.stl_facets_limit": "STL が 200 万面を超えています",
        "err.stl_geometry": "STL に有効な形状がありません",
        "err.stl_no_triangles": "STL に三角形がありません",
        "err.gap_too_big": "間隔がベッドより大きくなっています",
        "err.plan_first": "まず配置を計算してください",
        "err.reread": "STL を再読み込みできませんでした：{name}",
        "err.facets_limit": "1 つのプロジェクトは {n} 面までです。パーツを減らすか、Orca のレイアウトを使ってください",
        "msg.already_watched": "このフォルダーは既に監視されています：{path}",
        "msg.inside_watched": "このフォルダーは既に監視フォルダー {path} の中にあります（サブフォルダー検索がオン）。",
        "msg.replaced_nested": "フォルダーを追加しました。次の入れ子のフォルダーは個別に必要なくなりました：{paths}",
        "err.scan_stopped": "スキャンが止まりました：{error}",
        "err.source_gone": "ソースが監視フォルダー内にありません",
        "err.select_source": "まず ZIP または STL ファイルのあるフォルダーを選んでください",
        "err.archive_moved": "アーカイブが監視フォルダー内にありません",
        "err.stl_left_archive": "STL が選んだアーカイブ内にありません",
        "err.stl_left_folder": "STL が選んだフォルダー内にありません",
        "err.stl_limit": "STL が {mb} MB の上限を超えています",
        "err.arrange_running": "Orca のレイアウトは既に実行中です",
        "err.select_stl": "STL パーツを 1 つ以上選んでください",
        "err.too_many_parts": "1 回でレイアウトできる STL ファイルは最大 {n} 個です",
        "err.no_bed": "アクティブなプリンターの造形可能領域を読めませんでした",
        "err.cli_failed": "Orca のコマンドラインがプロジェクトを作成しませんでした。{detail}",
        "err.cli_exit": "終了コード：{code}",
        "msg.arranged": "Orca が {parts} を {plates} にレイアウトし、プロジェクトを開いています。「準備」に既にモデルがある場合は、プレートを保持するため「プロジェクトとして開く」を選んでください。",
        "msg.arranged_partly": "Orca は {parts} のうち {n} 個を、回転してもプレートに置けませんでした：プレートの横に残っています。ベッドとの大きさを確認するか、PlatePilot の配置でどれかを確認してください。",
        "err.arrange_failed": "レイアウトが完了しませんでした：{error}",
        "msg.picker_cancelled": "フォルダーの選択を取り消しました。",
        "msg.source_hidden": "ソースをリストから隠しました。ディスク上のファイルは残ります。",
        "msg.deleted": "PlatePilot から削除しました。",
        "msg.imported": "PlatePilot に追加しました：{files}。",
        "import.files": "{n} 個のファイル",
        "import.group": "ファイル {time}",
        "err.import_type": "{name}：追加できるのは ZIP、STL、STEP、3MF ファイルだけです",
        "err.import_size": "{name} は {mb} MB を超えています",
        "err.import_state": "ファイルの転送が中断されました。もう一度追加してください",
        "err.import_path": "{name} の名前は PlatePilot のフォルダーには長すぎます",
        "err.no_storage": "PlatePilot のデータフォルダーを使えません：{error}",
        "err.not_added": "削除できるのは PlatePilot に追加したソースだけです",
        "err.paste_path": "{path} はフォルダーでも ZIP、STL、STEP、3MF ファイルでもありません",
        "err.part_missing": "パーツが見つかりません",
        "err.no_preview": "このパーツにはプレビューのデータがありません",
        "err.stl_outside": "STL が監視フォルダーの外にあります",
        "err.no_printable_area": "アクティブなプロファイルの printable_area を読めませんでした。プリンタープロファイルを再読み込みして、もう一度試してください。",
        "err.plan_source": "まず選んだソースの配置を計算してください",
        "err.plan_not_exportable": "まずベッドに入らない、またはプリンターより高いパーツを直してください",
        "err.printer_changed": "計算後にアクティブなプリンターが変わりました。配置を計算し直してください",
        "err.stl_changed": "計算後に STL が変わりました。配置を計算し直してください"
    },
    "zh_CN": {
        "btn.support": "支持作者",
        "btn.support_tip": "用 Telegram 星星支持 PlatePilot：在浏览器中打开 t.me/GIMINOT",
        "err.support_open": "浏览器没有打开。作者的 Telegram：{url}",
        "err.3mf_transform": "文件中某个对象的位置数据已损坏",
        "err.3mf_nesting": "对象嵌套层级过深",
        "app.tagline": "模型库与多盘排布",
        "app.loading_profile": "正在加载打印机配置…",
        "btn.refresh": "刷新",
        "btn.folders": "设置",
        "btn.add_files": "添加文件",
        "btn.add_files_tip": "添加 ZIP、STL、STEP 或 3MF 文件。它们会复制到 PlatePilot 中，因此 Orca 不会请求权限。",
        "lib.title": "模型库",
        "lib.search": "按名称搜索",
        "lib.all": "全部",
        "lib.folders": "文件夹",
        "parts.title": "零件",
        "parts.all": "全选",
        "parts.none": "全不选",
        "view.reset": "重置视图",
        "view.hint": "拖动旋转 · 滚轮缩放 · 双击重置",
        "view.empty": "选择一个 STL 零件，在打印机热床上查看",
        "mode.own": "PlatePilot 排布",
        "mode.orca": "Orca 自动摆放",
        "mode.gap": "间距",
        "mode.orient": "旋转零件（Orca 自动朝向）",
        "mode.orient_tip": "摆放前，Orca 会像“自动朝向”按钮那样旋转每个零件：悬垂更少，底面更稳。关闭：零件保持文件中的摆放方式，即设计者的本意。",
        "orient.checking": "正在检查零件的摆放… {done}/{total}",
        "orient.done": "{n} 个零件可以摆得更好：请看列表中的“旋转”",
        "orient.advice": "旋转后更好：悬垂减少 {pct}%",
        "orient.turned": "已旋转：悬垂减少 {pct}%",
        "btn.turn": "旋转",
        "btn.unturn": "撤销旋转",
        "btn.turn_all": "旋转全部建议（{n}）",
        "btn.turn_all_tip": "旋转 PlatePilot 发现摆放不佳的每个零件：倒置、立在窄边上、有可避免的悬垂",
        "mode.plates": "盘数",
        "mode.plates_auto": "自动",
        "mode.plates_tip": "把零件分到几个盘上。留空：能放下的最少盘数。更多时会平均分配，即使一个盘就能放下。",
        "unit.mm": "mm",
        "btn.plan": "计算",
        "btn.plan_smart": "智能排布",
        "btn.plan_smart_tip": "测试版：每个盘的零件集中在中间，小零件最靠近中心，远离热床边缘且间距均匀；各盘装填均匀",
        "btn.send": "在“准备”中打开",
        "btn.arrange": "在 Orca 中摆放",
        "mode.own_note": "PlatePilot 按占地面积把零件排在当前打印机的热床上，显示各个盘，并在“准备”中打开项目。",
        "mode.orca_note": "零件交给 Orca 自己的自动摆放：它使用零件轮廓以及当前的打印机和工艺配置，按需添加盘并打开项目。",
        "drawer.title": "设置",
        "drawer.close": "关闭",
        "drawer.folders": "监视的文件夹",
        "drawer.folders_note": "PlatePilot 根据名称查找 ZIP 压缩包和含有模型文件的文件夹。只有在你打开文件时才会读取它。",
        "drawer.pick": "选择文件夹…",
        "drawer.paste": "或粘贴文件夹路径（可以带有“复制为路径”产生的引号）：",
        "drawer.paste_only": "粘贴模型文件夹的完整路径：",
        "drawer.path_ph": "文件夹完整路径",
        "drawer.add": "添加",
        "drawer.formats": "格式",
        "drawer.formats_note": "不含所选格式的来源会被隐藏。预览和排布适用于 STL。",
        "drawer.scan": "扫描",
        "drawer.recursive": "搜索子文件夹",
        "drawer.check_on_open": "打开 PlatePilot 时检查文件夹",
        "drawer.audit_note": "PlatePilot 查看监视文件夹之前，Orca 会询问一次；你每打开一个压缩包或模型文件夹，也会询问一次。拖入 PlatePilot 的文件无需权限。",
        "drawer.preview": "3D 预览",
        "drawer.gpu_preview": "使用显卡绘制（WebGL）",
        "drawer.gpu_note": "完整显示不超过 250 000 个面的零件，并能流畅旋转。如果预览显示异常，请关闭：简易模式最多绘制 30 000 个面。",
        "drawer.theme": "主题",
        "theme.orca": "与 Orca 相同",
        "theme.parchment": "羊皮纸",
        "theme.terracotta": "陶土",
        "theme.sage": "鼠尾草",
        "theme.blueprint": "蓝图",
        "theme.dusk": "黄昏",
        "theme.workshop": "工坊（深色）",
        "theme.night": "夜班（深色）",
        "drawer.language": "语言",
        "drawer.lang_auto": "与 Orca 相同",
        "modal.confirm": "确定",
        "modal.cancel": "取消",
        "modal.continue": "继续",
        "st.choose_folder": "把 ZIP 或 STL 文件拖到这里，或打开设置并选择一个模型文件夹。",
        "st.press_refresh": "按“刷新”扫描文件夹。",
        "st.scanning": "正在扫描文件夹…",
        "st.scanned": "文件夹已检查 · {sources}",
        "st.scanned_new": "文件夹已检查 · {sources} · 新增 {n}",
        "st.importing": "正在添加 {name} · {i}/{n} · {pct}%",
        "st.import_none": "没有可添加的内容：请拖入 ZIP、STL、STEP 或 3MF 文件或包含它们的文件夹。",
        "st.import_skipped": " 已跳过其他文件：{n}。",
        "st.opening": "正在打开…",
        "st.opening_picker": "正在打开文件夹选择器…",
        "st.opened_source": "已打开“{name}”。",
        "st.plan_ready": "排布已完成。请在“准备”中打开。",
        "st.plan_problems": "有些零件放不进热床。请修改选择。",
        "st.building": "正在为 Orca 生成项目…",
        "st.project_opened": "项目正在 Orca 中打开。如果“准备”中已有模型，Orca 会询问如何处理：“按项目打开”保留各个盘，“仅导入模型数据”把零件加入当前项目。",
        "st.project_saved": "3MF 已保存：{path}。请用 文件 > 打开项目 打开。{error}",
        "st.arranging": "Orca 正在摆放 {n} 个零件，并按需添加盘…",
        "st.arrange_done": "自动摆放完成。",
        "st.plugin_error": "插件错误。",
        "st.removed_source": "该来源已从磁盘删除或被隐藏。",
        "printer.unreadable": "未读取到配置",
        "printer.zones": "禁区",
        "printer.zones_tip": "打印机配置中有热床禁用区域。请在 Orca 中检查排布。",
        "lib.nothing_found": "没有符合筛选条件的内容。",
        "lib.press_refresh": "按“刷新”在文件夹中查找模型。",
        "lib.add_folder": "把 ZIP 或 STL 文件拖到这里，或在设置中添加模型文件夹。",
        "lib.new": "新增",
        "lib.added": "已添加",
        "lib.all_folders": "全部文件夹",
        "lib.folder_tip": "只显示一个文件夹的来源",
        "lib.other_show": "另有 {n} 个名称不像模型的压缩包",
        "lib.other_hide": "隐藏名称不像模型的压缩包",
        "lib.other_note": "PlatePilot 按名称判断，因为查看每个压缩包内部会让 Orca 对每一个都询问权限。打开压缩包即可确认：有模型的会排到前面，没有的会消失。搜索也会包括它们。",
        "lib.mark_seen": "全部标记为已查看",
        "lib.no_new": "自上次检查以来没有新来源。",
        "drop.title": "拖放以添加到 PlatePilot",
        "drop.note": "ZIP、STL、STEP、3MF 或包含它们的文件夹。文件会复制到 PlatePilot 自己的文件夹中，因此 Orca 不会请求权限。如果想改为监视某个文件夹，请在设置中添加，或用 Ctrl+V 粘贴其路径。",
        "src.choose": "选择来源",
        "src.choose_note": "在模型库中选择一个压缩包或文件夹。PlatePilot 会显示其中的零件和热床上的预览。",
        "src.summary": "{n} 个模型文件 · {stl} 个 STL · {path}",
        "src.hide": "隐藏",
        "src.hide_tip": "从列表中隐藏。文件仍保留在磁盘上。",
        "src.new": "新",
        "src.added": "已添加",
        "src.unopened": "尚未打开",
        "src.delete": "删除",
        "src.delete_tip": "删除添加到 PlatePilot 的副本",
        "delete.title": "从 PlatePilot 中删除？",
        "delete.body": "将删除 PlatePilot 文件夹中“{name}”的副本。你拖入或粘贴时的原始文件不受影响。",
        "parts.meta": "{dims} mm · {facets} 个面",
        "parts.catalog_only": "{type} · 仅目录",
        "parts.no_files": "该来源没有所选格式的文件。",
        "parts.placeholder": "零件会显示在这里。",
        "view.meta": "{dims} mm · {facets} 个面",
        "view.shown": " · 显示 {n} 个",
        "plan.parts": "{n} 个零件",
        "plan.plates": "{n} 个盘",
        "plan.not_fit": "放不下：{name}",
        "plan.at_least": "至少需要 {plates}",
        "plan.at_most": "每个零件最多一个盘",
        "plan.too_tall": "高于 Z：{name}",
        "plan.zones": "请在 Orca 中检查禁用区域",
        "plan.smart": "智能排布 β",
        "plan.plate": "盘 {n}",
        "plan.view3d": "3D",
        "plan.view3d_all": "3D 查看各盘",
        "plan.view3d_all_tip": "在预览中以 3D 查看各盘，每个零件都与项目中一致",
        "view.plate_button": "盘 {n}/{total} 3D",
        "view.plate_prev": "上一个盘",
        "view.plate_next": "下一个盘",
        "plan.view3d_tip": "在预览中以 3D 查看此盘",
        "view.plate_meta": "{parts} · 热床 {bed} · 简化显示",
        "st.plate_shown": "{name} 3D：拖动即可旋转。",
        "st.plate_loading": "{name} 3D：零件 {i}/{n}…",
        "basket.add": "加入篮子",
        "basket.add_tip": "把勾选的零件放入篮子，篮子中来自多个来源的零件会一起排布",
        "basket.show": "篮子 · {n}",
        "basket.back": "返回来源",
        "basket.empty": "篮子是空的。在任意来源中勾选零件并按“加入篮子”；这里会把多个来源的零件一起排布。",
        "basket.from": "来自 {name}",
        "basket.copies": "此零件的份数",
        "basket.remove": "移出篮子",
        "basket.clear": "清空篮子",
        "basket.added": "篮子中：{n}",
        "basket.title": "篮子",
        "basket.and_more": "{name} 等 {n} 个",
        "err.basket_empty": "篮子是空的",
        "basket.turned": "已旋转",
        "err.basket_gone": "篮子中的“{name}”已不在原处（已移动、修改或删除）；请移出后重新添加",
        "plan.plate_meta": "{parts} · {fill}%",
        "plan.rotated": " · 旋转 {n}°",
        "plan.zoom_hint": "点击盘可放大查看",
        "plan.zoom_title": "盘 {n}/{total}",
        "plan.prev": "上一个盘",
        "plan.next": "下一个盘",
        "plan.close": "关闭",
        "folders.none": "还没有文件夹。",
        "folders.remove": "停止监视",
        "folders.sources": "{n} 个来源",
        "folders.stats": "{sources} · 检查于 {time}",
        "folders.new": "新增 {n}",
        "folders.other": "{n} 个不像模型的压缩包",
        "folders.empty": "已隐藏 {n} 个无模型的来源",
        "folders.never": "尚未检查",
        "folders.missing": "未找到文件夹",
        "folders.skipped": "已跳过 {n} 个子文件夹：Orca 无法为插件打开它们",
        "folders.unreadable": "Orca 无法为插件打开此文件夹：其路径包含非拉丁字母，或包含名为 config、cert 或 secret 的文件夹",
        "folders.truncated": "仅显示前 {n} 个",
        "folders.added": "已添加到 PlatePilot",
        "folders.added_note": "拖入或粘贴的文件会复制到这里。",
        "err.path_empty": "请输入文件夹路径",
        "err.path_relative": "请输入完整的文件夹路径，例如 {example}",
        "err.folder_missing": "未找到文件夹：{path}",
        "err.not_filesystem": "所选项目不是磁盘上的文件夹（例如“库”）；请选择普通文件夹",
        "err.picker_windows": "文件夹选择器仅在 Windows 上可用；请改为粘贴路径。",
        "picker.title": "PlatePilot 的模型文件夹",
        "picker.ok": "监视文件夹",
        "err.no_main_window": "未找到 Orca 主窗口",
        "err.open_unsupported": "仅在 Windows 上支持在此 Orca 窗口中打开项目",
        "err.no_executable": "未找到正在运行的 OrcaSlicer 程序",
        "err.no_settings": "无法读取当前 Orca 配置的设置",
        "err.no_printer_preset": "Orca 中未选择打印机配置",
        "err.no_process_preset": "Orca 中未选择工艺配置",
        "warn.zip_empty": "压缩包中没有 STL、STEP 或 3MF",
        "warn.zip_unreadable": "无法读取压缩包：{error}",
        "err.archive_outside": "压缩包必须位于监视的文件夹内",
        "err.folder_outside": "模型文件夹必须位于监视的文件夹内",
        "warn.step_open": "STEP：不支持预览和排布；请在 Orca 中打开，由 Orca 自行转换",
        "warn.3mf_error": "无法读取 3MF：{error}",
        "warn.3mf_empty": "3MF 中没有模型",
        "btn.open_in_orca": "在 Orca 中打开",
        "msg.step_sent": "已将 {name} 发送到 Orca：它会在“准备”中打开。",
        "warn.unsafe_name": "名称含非拉丁字母：此 Orca 版本无法为插件打开它（OrcaSlicer 2.5.0 可以）；请把它拖入 PlatePilot",
        "err.unsafe_path": "{path}：Orca 不允许插件打开包含名为 config、cert 或 secret 的文件夹的路径，而 OrcaSlicer 2.5.0 之前的版本也无法打开含非拉丁字母的路径。请把文件拖入 PlatePilot 或重命名文件夹。",
        "st.no_answer": "插件没有响应。请按“刷新”重试。",
        "st.scan_interrupted": "上次的文件夹检查没有完成（Orca 被关闭），因此没有自动重复。请按“刷新”检查文件夹。",
        "warn.too_big": "文件大于 {mb} MB",
        "err.total_archive": "压缩包中的 STL 文件总计超过 {mb} MB",
        "err.total_folder": "文件夹中的 STL 文件总计超过 {mb} MB",
        "warn.empty_model": "空模型",
        "warn.stl_error": "STL 错误：{error}",
        "err.stl_short": "STL 文件太短",
        "err.stl_coords": "STL 坐标无效",
        "err.stl_vertices": "无法读取 STL 顶点",
        "err.stl_facets_limit": "STL 超过 200 万个面",
        "err.stl_geometry": "STL 没有有效的几何形状",
        "err.stl_no_triangles": "STL 中没有三角形",
        "err.gap_too_big": "间距大于热床",
        "err.plan_first": "请先计算排布",
        "err.reread": "无法重新读取 STL：{name}",
        "err.facets_limit": "一个项目最多 {n} 个面；请少选一些零件，或使用 Orca 自动摆放",
        "msg.already_watched": "此文件夹已在监视中：{path}",
        "msg.inside_watched": "此文件夹已位于监视的文件夹 {path} 内（已开启子文件夹搜索）。",
        "msg.replaced_nested": "已添加文件夹；以下嵌套文件夹不再需要单独监视：{paths}",
        "err.scan_stopped": "扫描已停止：{error}",
        "err.source_gone": "该来源已不在监视的文件夹内",
        "err.select_source": "请先选择一个 ZIP 或含有 STL 文件的文件夹",
        "err.archive_moved": "压缩包已不在监视的文件夹内",
        "err.stl_left_archive": "STL 已不在所选压缩包中",
        "err.stl_left_folder": "STL 已不在所选文件夹中",
        "err.stl_limit": "STL 超过 {mb} MB 的限制",
        "err.arrange_running": "Orca 自动摆放已在运行",
        "err.select_stl": "请至少选择一个 STL 零件",
        "err.too_many_parts": "一次最多可摆放 {n} 个 STL 文件",
        "err.no_bed": "无法读取当前打印机的可打印区域",
        "err.cli_failed": "Orca 命令行没有生成项目。{detail}",
        "err.cli_exit": "退出代码：{code}",
        "msg.arranged": "Orca 已将 {parts} 摆放到 {plates} 上，正在打开项目。如果“准备”中已有模型，请选择“按项目打开”以保留各个盘。",
        "msg.arranged_partly": "{parts} 中有 {n} 个即使旋转也无法放到盘上：它们留在盘旁边。请对比它们与热床的尺寸，或用 PlatePilot 排布查看是哪些。",
        "err.arrange_failed": "自动摆放未完成：{error}",
        "msg.picker_cancelled": "已取消选择文件夹。",
        "msg.source_hidden": "该来源已从列表中隐藏；磁盘上的文件保留。",
        "msg.deleted": "已从 PlatePilot 中删除。",
        "msg.imported": "已添加到 PlatePilot：{files}。",
        "import.files": "{n} 个文件",
        "import.group": "文件 {time}",
        "err.import_type": "{name}：只能添加 ZIP、STL、STEP 和 3MF 文件",
        "err.import_size": "{name} 大于 {mb} MB",
        "err.import_state": "文件传输中断；请重新添加文件",
        "err.import_path": "{name} 的名称对 PlatePilot 文件夹来说太长",
        "err.no_storage": "PlatePilot 的数据文件夹不可用：{error}",
        "err.not_added": "只能删除添加到 PlatePilot 的来源",
        "err.paste_path": "{path} 不是文件夹，也不是 ZIP、STL、STEP 或 3MF 文件",
        "err.part_missing": "未找到零件",
        "err.no_preview": "此零件没有预览数据",
        "err.stl_outside": "STL 位于监视的文件夹之外",
        "err.no_printable_area": "无法读取当前配置的 printable_area。请重新加载打印机配置后再试。",
        "err.plan_source": "请先计算所选来源的排布",
        "err.plan_not_exportable": "请先修正放不进热床或高于打印机的零件",
        "err.printer_changed": "计算后当前打印机已更改；请重新计算排布",
        "err.stl_changed": "计算后 STL 已更改；请重新计算排布"
    }
}


def _set_language(choice):
    """Select the UI language: "en", "ru" or "auto" (Orca's own language)."""
    global _LANG
    if choice not in LANGUAGES:
        try:
            choice = str(orca.host.app_info().get("language", ""))
        except Exception:
            choice = ""
        choice = _orca_language(choice)
    _LANG = choice
    return _LANG


def _orca_language(code):
    """PlatePilot's language for Orca's language code ("de_DE", "pt_BR", "zh_CN"…); English otherwise."""
    code = str(code or "").replace("-", "_").lower()
    for lang in LANGUAGES:
        if code == lang.lower() or code.split("_")[0] == lang.split("_")[0].lower():
            return lang
    return "en"


def _text(key, lang=None):
    lang = lang or _LANG
    if lang in TABLE_LANGUAGES:
        return STRINGS[key][TABLE_LANGUAGES.index(lang)]
    return EXTRA_STRINGS.get(lang, {}).get(key) or STRINGS[key][0]


def _t(key, **values):
    """Translated text for key in the active language, with {placeholders} filled in."""
    text = _text(key)
    return text.format(**values) if values else text


def _plural(key, n):
    """Pick the plural form of a "one|few|many" (ru), "one|other" or single-form (ja, zh, tr) string."""
    forms = _t(key).split("|")
    if _LANG == "ru" and len(forms) == 3:
        index = 0 if n % 10 == 1 and n % 100 != 11 else 1 if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 2
    elif _LANG == "fr":
        index = 0 if n in (0, 1) else len(forms) - 1
    else:
        index = 0 if n == 1 else len(forms) - 1
    return forms[index].format(n=n)


def _ui_strings():
    return {key: _text(key) for key in STRINGS}


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
:is([data-orca-theme=dark]:not([data-pp-mode=light]),[data-pp-mode=dark]){--raised:color-mix(in srgb,#fff 5%,var(--bg));--surface:color-mix(in srgb,#fff 3%,var(--bg));--surface-2:color-mix(in srgb,#fff 7%,var(--bg));--warn:#f2ab3c;--danger:#ff7a7a;--shadow:0 1px 2px rgb(0 0 0/.35),0 14px 34px -18px rgb(0 0 0/.75)}
*{box-sizing:border-box}[hidden]{display:none!important}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);display:grid;grid-template-rows:auto 3px minmax(0,1fr) auto;overflow:hidden}
svg.ic-support{width:20px;height:20px;flex:none;margin:-2px 0}
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
.glyph.k-zip{--k:#d97706}.glyph.k-folder{--k:#2563eb}.glyph.k-added{--k:var(--accent)}:is([data-orca-theme=dark]:not([data-pp-mode=light]),[data-pp-mode=dark]) .glyph.k-zip{--k:#f5a524}:is([data-orca-theme=dark]:not([data-pp-mode=light]),[data-pp-mode=dark]) .glyph.k-folder{--k:#60a5fa}
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
html[data-pp-theme=parchment]{--bg:#ece4d3;--fg:#3a2f25;--muted:#7a6a57;--line:#dccfb4;--accent:#9a5a2a;--accent-fg:#fff8ee;--raised:#f7f1e3;--surface:color-mix(in srgb,#3a2f25 3%,#f7f1e3);--surface-2:color-mix(in srgb,#3a2f25 7%,#f7f1e3);--accent-soft:color-mix(in srgb,#9a5a2a 15%,transparent)}
html[data-pp-theme=terracotta]{--bg:#efe2d9;--fg:#3a2a24;--muted:#836a5f;--line:#e2cdbf;--accent:#b4532f;--accent-fg:#fff7f2;--raised:#fbf5f1;--surface:color-mix(in srgb,#3a2a24 3%,#fbf5f1);--surface-2:color-mix(in srgb,#3a2a24 7%,#fbf5f1);--accent-soft:color-mix(in srgb,#b4532f 15%,transparent)}
html[data-pp-theme=sage]{--bg:#e6ece4;--fg:#24302a;--muted:#63716a;--line:#d0dacd;--accent:#477a58;--accent-fg:#f6fbf7;--raised:#f5f8f2;--surface:color-mix(in srgb,#24302a 3%,#f5f8f2);--surface-2:color-mix(in srgb,#24302a 7%,#f5f8f2);--accent-soft:color-mix(in srgb,#477a58 15%,transparent)}
html[data-pp-theme=blueprint]{--bg:#e7edf3;--fg:#16202b;--muted:#5b6878;--line:#cfd9e4;--accent:#2b5fb8;--accent-fg:#f5f8ff;--raised:#f7f9fc;--surface:color-mix(in srgb,#16202b 3%,#f7f9fc);--surface-2:color-mix(in srgb,#16202b 7%,#f7f9fc);--accent-soft:color-mix(in srgb,#2b5fb8 15%,transparent)}
html[data-pp-theme=dusk]{--bg:#ebe7f1;--fg:#2a2533;--muted:#6c6580;--line:#dad3e5;--accent:#6650ad;--accent-fg:#faf8ff;--raised:#f8f6fb;--surface:color-mix(in srgb,#2a2533 3%,#f8f6fb);--surface-2:color-mix(in srgb,#2a2533 7%,#f8f6fb);--accent-soft:color-mix(in srgb,#6650ad 15%,transparent)}
html[data-pp-theme=workshop]{--bg:#26231f;--fg:#ece5d8;--muted:#a59c8d;--line:#433e37;--accent:#e0a548;--accent-fg:#241c10;--raised:#2f2b26;--surface:color-mix(in srgb,#ece5d8 3%,#2f2b26);--surface-2:color-mix(in srgb,#ece5d8 7%,#2f2b26);--accent-soft:color-mix(in srgb,#e0a548 15%,transparent)}
html[data-pp-theme=night]{--bg:#141a24;--fg:#dce4ef;--muted:#8a96aa;--line:#2a3445;--accent:#3fb6a8;--accent-fg:#071a18;--raised:#1b2330;--surface:color-mix(in srgb,#dce4ef 3%,#1b2330);--surface-2:color-mix(in srgb,#dce4ef 7%,#1b2330);--accent-soft:color-mix(in srgb,#3fb6a8 15%,transparent)}
.basket-item .copies{display:flex;align-items:center;gap:3px;font-size:11.5px;color:var(--muted)}.basket-item .copies input{width:48px}
.basket-foot{display:flex;justify-content:flex-end;padding:6px 4px 2px}
.plate-head .view3d{margin-left:auto;height:24px;padding:0 9px;font-size:11.5px;gap:4px;flex:none}.plate-head .view3d .ic{width:14px;height:14px}
.plate-nav{display:flex;align-items:center;gap:4px;pointer-events:auto}.plate-nav .ic{width:15px;height:15px}.plate-nav button.small{gap:5px}
.summary .view3d-all{height:24px;padding:0 10px;font-size:12px;gap:5px}.summary .view3d-all .ic{width:14px;height:14px}
.zoom-head .view3d-zoom{gap:5px}.zoom-head .view3d-zoom .ic{width:15px;height:15px}
.orient-bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:0 12px 8px;font-size:12px;color:var(--muted)}.orient-bar span{flex:1 1 200px}
.part .tip{color:var(--accent)}
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
 <symbol id="i-support" viewBox="0 0 24 24"><defs><clipPath id="support-heart"><path d="M12 22s-8-4.8-8-10.3A4.3 4.3 0 0 1 12 9.3a4.3 4.3 0 0 1 8 2.4C20 17.2 12 22 12 22z"/></clipPath></defs><path d="M9.6 1.5h4.8v2.3l-1.2 1.8h-2.4L9.6 3.8z" fill="currentColor"/><path d="M11.4 5.6h1.2l-.6 1.4z" fill="currentColor"/><g clip-path="url(#support-heart)"><rect x="3" y="8" width="18" height="15" fill="#C8A266"/><path d="M3 11.2h18M3 13.6h18M3 16h18M3 18.4h18M3 20.8h18" stroke="#8E6A31" stroke-width=".7" opacity=".75"/><rect x="3" y="8" width="18" height="3.2" fill="#F2EBDC"/></g><path d="M12 22s-8-4.8-8-10.3A4.3 4.3 0 0 1 12 9.3a4.3 4.3 0 0 1 8 2.4C20 17.2 12 22 12 22z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></symbol>
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
 <div class="appbar-actions"><button id="scan" class="primary"><svg class="ic"><use href="#i-refresh"/></svg><span data-i18n="btn.refresh">Refresh</span></button><button id="addFiles" data-i18n-title="btn.add_files_tip" title="Add ZIP, STL, STEP or 3MF files"><svg class="ic"><use href="#i-file-plus"/></svg><span data-i18n="btn.add_files">Add files</span></button><input id="filePick" type="file" multiple accept=".zip,.stl,.step,.stp,.3mf" hidden><button id="support" data-i18n-title="btn.support_tip" title="Support PlatePilot with Telegram Stars"><svg class="ic-support" viewBox="0 0 24 24"><use href="#i-support"/></svg><span data-i18n="btn.support">Support</span></button><button id="openSettings"><svg class="ic"><use href="#i-sliders"/></svg><span data-i18n="btn.folders">Settings</span></button></div>
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
    <div class="card-head"><h2 id="partsTitle" data-i18n="parts.title">Parts</h2><span id="partCount" class="count">0</span><span class="spacer"></span><button id="toBasket" class="small" data-i18n="basket.add" data-i18n-title="basket.add_tip" title="Put the ticked parts in the basket">To basket</button><button id="basketTab" class="ghost" hidden></button><button id="all" class="ghost" data-i18n="parts.all">All</button><button id="none" class="ghost" data-i18n="parts.none">None</button></div>
    <div id="orientBar" class="orient-bar" hidden><span id="orientNote"></span><button id="turnAll" class="small" data-i18n-title="btn.turn_all_tip" title="Turn every suggested part"></button></div>
    <div id="parts" class="part-list"></div>
    <div id="basketList" class="part-list" hidden></div>
   </div>
   <div class="card viewer">
    <canvas id="view"></canvas><canvas id="viewGl" aria-hidden="true"></canvas>
    <div class="hud"><div class="t"><b id="viewTitle"></b><span id="viewMeta" class="muted"></span></div><span id="plateNav" class="plate-nav" hidden><button id="platePrev" class="icon" data-i18n-title="view.plate_prev" title="Previous plate">&#8249;</button><button id="plateShow" class="primary small"><svg class="ic"><use href="#i-cube"/></svg><span id="plateShowText"></span></button><button id="plateNext" class="icon" data-i18n-title="view.plate_next" title="Next plate">&#8250;</button></span><button id="resetView" class="icon" data-i18n-title="view.reset" title="Reset view" hidden><svg class="ic"><use href="#i-target"/></svg></button></div>
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
    <label class="field" id="orientField" data-i18n-title="mode.orient_tip" title="Orca auto-orient" hidden><input id="orcaOrient" type="checkbox"> <span data-i18n="mode.orient">Turn parts (Orca auto-orient)</span></label>
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
 <div class="drawer-head"><svg class="ic"><use href="#i-sliders"/></svg><h2 data-i18n="drawer.title">Settings</h2><button id="closeSettings" class="icon ghost" data-i18n-title="drawer.close" title="Close"><svg class="ic"><use href="#i-x"/></svg></button></div>
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
  <div class="section"><h3 data-i18n="drawer.theme">Theme</h3>
   <select id="theme"><option value="orca" data-i18n="theme.orca">Same as Orca</option><option value="parchment" data-i18n="theme.parchment">Parchment</option><option value="terracotta" data-i18n="theme.terracotta">Terracotta</option><option value="sage" data-i18n="theme.sage">Sage</option><option value="blueprint" data-i18n="theme.blueprint">Blueprint</option><option value="dusk" data-i18n="theme.dusk">Dusk</option><option value="workshop" data-i18n="theme.workshop">Workshop (dark)</option><option value="night" data-i18n="theme.night">Night shift (dark)</option></select>
  </div>
  <div class="section"><h3 data-i18n="drawer.language">Language</h3>
   <select id="language"><option value="auto" data-i18n="drawer.lang_auto">Same as Orca</option><option value="en">English</option><option value="ru">Русский</option><option value="de">Deutsch</option><option value="fr">Français</option><option value="es">Español</option><option value="pt_BR">Português (Brasil)</option><option value="it">Italiano</option><option value="tr">Türkçe</option><option value="ja">日本語</option><option value="zh_CN">简体中文</option></select>
  </div>
 </div>
</aside>

<div id="drop" class="drop" hidden><div class="drop-card"><svg class="ic"><use href="#i-plus"/></svg><h2 data-i18n="drop.title">Drop to add to PlatePilot</h2><p data-i18n="drop.note">ZIP, STL, STEP, 3MF or a folder with them.</p></div></div>
<div id="zoom" class="modal" hidden><div class="zoom" role="dialog" aria-modal="true"><div class="zoom-head"><h2 id="zoomTitle"></h2><span id="zoomMeta"></span><button id="zoom3d" class="primary small view3d-zoom" data-i18n-title="plan.view3d_tip" title="See this plate in 3D"><svg class="ic"><use href="#i-cube"/></svg><span data-i18n="plan.view3d">3D</span></button><button id="zoomPrev" class="icon" data-i18n-title="plan.prev" title="Previous plate">&#8249;</button><button id="zoomNext" class="icon" data-i18n-title="plan.next" title="Next plate">&#8250;</button><button id="zoomClose" data-i18n="plan.close">Close</button></div><div class="zoom-body"><div id="zoomPlate"></div><ol id="zoomList" class="zoom-list"></ol></div></div></div>
<div id="modal" class="modal" hidden><div class="dialog"><h2 id="modalTitle" data-i18n="modal.confirm">Confirm</h2><div id="modalText" class="body"></div><div class="actions"><button id="modalNo" data-i18n="modal.cancel">Cancel</button><button id="modalYes" class="primary" data-i18n="modal.continue">Continue</button></div></div></div>

<script>
const $=id=>document.getElementById(id);
const S={view:'source',basket:{items:[],total:0},previewMember:null,plateView:null,archives:[],folders:[],selected:null,selectedKind:null,selectedName:'',selectedAdded:false,parts:[],profile:null,config:{},mesh:null,drag:null,busy:false,scanning:false,importing:false,importWait:null,replyTimer:null,lastScan:0,showOther:false,pending:null,nativeTick:null,mode:'own',kind:'all',plan:null,previewIndex:-1,strings:{},lang:'en',platform:'windows'};
const IMPORT_RE=/\.(zip|stl|step|stp|3mf)$/i,CHUNK=1536*1024;
const PALETTE=['#14b8a6','#6366f1','#f59e0b','#ec4899','#0ea5e9','#84cc16','#a855f7','#f97316'];
const icon=n=>`<svg class="ic"><use href="#i-${n}"/></svg>`;
function t(key,values={}){return String(S.strings[key]??key).replace(/\{(\w+)\}/g,(m,k)=>values[k]??m)}
function plural(key,n){const forms=t(key).split('|');let i=(S.lang==='fr'?n<=1:n===1)?0:forms.length-1;if(S.lang==='ru'&&forms.length===3)i=n%10===1&&n%100!==11?0:n%10>=2&&n%10<=4&&(n%100<12||n%100>14)?1:2;return forms[i].replace('{n}',n)}
function applyStrings(){document.documentElement.lang=locale();document.querySelectorAll('[data-i18n]').forEach(el=>el.textContent=t(el.dataset.i18n));document.querySelectorAll('[data-i18n-ph]').forEach(el=>el.placeholder=t(el.dataset.i18nPh));document.querySelectorAll('[data-i18n-title]').forEach(el=>el.title=t(el.dataset.i18nTitle))}
function send(type,extra={}){orca.postMessage({type,...extra})}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function locale(){return {ru:'ru-RU',de:'de-DE',fr:'fr-FR',es:'es-ES',pt_BR:'pt-BR',it:'it-IT',tr:'tr-TR',ja:'ja-JP',zh_CN:'zh-CN'}[S.lang]||'en-US'}
function fmt(v,d=1){return Number(v||0).toLocaleString(locale(),{maximumFractionDigits:d,minimumFractionDigits:0})}
function status(text,level='info'){$('status').textContent=text;$('statusbar').className='statusbar '+({ok:'ok',warn:'warn',error:'err'}[level]||'')}
function setBusy(b){S.busy=b;document.body.classList.toggle('busy',b);$('scan').disabled=b;clearTimeout(S.replyTimer);if(b)S.replyTimer=setTimeout(noAnswer,30000);updateActions()}
/* a plugin call that fails inside Orca never answers; the page must not stay busy forever */
function noAnswer(){if(!S.busy||S.importing||S.nativeTick)return;S.scanning=false;setBusy(false);status(t('st.no_answer'),'error')}
function meshPart(p){return (p.filetype==='stl'||p.filetype==='3mf')&&p.member&&!p.blocked}
function selectedStl(){return S.parts.filter(p=>p.included&&meshPart(p))}
function inBasket(){return S.view==='basket'}
function renderBasket(){const b=S.basket,on=inBasket();$('partsTitle').textContent=on?t('basket.title'):t('parts.title');$('basketTab').hidden=!on&&!b.items.length;$('basketTab').textContent=on?t('basket.back'):t('basket.show',{n:b.total});$('basketTab').classList.toggle('on',on);
 $('parts').hidden=on;$('basketList').hidden=!on;$('all').hidden=on;$('none').hidden=on;$('toBasket').hidden=on;if(on)$('orientBar').hidden=true;else renderOrientBar();
 if(on){$('basketList').innerHTML=b.items.length?b.items.map(e=>`<div class="part basket-item ${S.previewMember==='basket:'+e.id+':1'?'on':''}" data-bid="${e.id}"><div class="t"><div class="n">${esc(e.name)}</div><div class="m">${esc(t('basket.from',{name:e.source_name}))} · ${esc(e.dims.map(v=>fmt(v)).join(' × '))} ${esc(t('unit.mm'))}${e.turned?` · <span class="tip">${esc(t('basket.turned'))}</span>`:''}</div></div><label class="copies" title="${esc(t('basket.copies'))}">×<input type="number" min="1" max="50" step="1" value="${e.copies}" data-copies="${e.id}"></label><button class="small" data-unbasket="${e.id}" title="${esc(t('basket.remove'))}">✕</button></div>`).join('')+`<div class="basket-foot"><button id="basketClear" class="ghost">${esc(t('basket.clear'))}</button></div>`:`<div class="empty">${icon('cube')}<div>${esc(t('basket.empty'))}</div></div>`;
  document.querySelectorAll('[data-copies]').forEach(el=>{el.onclick=e=>e.stopPropagation();el.onchange=()=>send('basket_set',{id:Number(el.dataset.copies),copies:Math.max(1,Math.min(50,Number(el.value)||1))})});
  document.querySelectorAll('[data-unbasket]').forEach(el=>el.onclick=e=>{e.stopPropagation();send('basket_remove',{id:Number(el.dataset.unbasket)})});
  document.querySelectorAll('.basket-item').forEach(el=>el.onclick=()=>{S.previewMember='basket:'+el.dataset.bid+':1';S.previewIndex=-1;S.plateView=null;renderBasket();send('preview_part',{member:S.previewMember,max_facets:previewBudget()})});
  if($('basketClear'))$('basketClear').onclick=()=>send('basket_clear')}
 updateActions()}
// A plate in 3D arrives part by part; it is drawn as it fills. A new request (another plate, a part,
// a new plan) leaves the parts still on their way unasked.
function showPlate3d(pi,quiet){S.plateShown=pi;renderPlateNav();S.previewIndex=-1;S.previewMember=null;S.plateView={plate:pi,token:(S.plateView?.token||0)+1,chunks:[],facets:0,quiet:!!quiet};if(!quiet)status(t('st.plate_loading',{name:t('plan.plate',{n:pi+1}),i:1,n:S.plan?.plates?.[pi]?.items?.length||1}));send('plate_view',{plate:pi,part:0})}
function plateChunk(msg){const v=S.plateView;if(!v||v.plate!==msg.plate||msg.part!==v.chunks.length)return;const bin=atob(msg.vertices),arr=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)arr[i]=bin.charCodeAt(i);v.chunks.push(arr);v.facets+=msg.facets;
 const total=v.chunks.reduce((s,c)=>s+c.length,0),all=new Uint8Array(total);let o=0;for(const c of v.chunks){all.set(c,o);o+=c.length}
 const keep=S.mesh&&S.mesh.plateToken===v.token?{yaw:S.mesh.yaw,pitch:S.mesh.pitch,zoom:S.mesh.zoom}:null;
 drawMesh({name:msg.name,facets:v.facets,dims:[msg.bed[0],msg.bed[1],msg.top],floats:new Float32Array(all.buffer,0,all.length>>2),plate:true,bed:msg.bed});
 if(S.mesh){S.mesh.plateToken=v.token;if(keep){Object.assign(S.mesh,keep);renderMesh()}}
 $('viewMeta').textContent=t('view.plate_meta',{parts:plural('plan.parts',msg.count),bed:`${fmt(msg.bed[0])} × ${fmt(msg.bed[1])} ${t('unit.mm')}`});
 if(msg.part+1<msg.count){if(!v.quiet)status(t('st.plate_loading',{name:msg.name,i:msg.part+2,n:msg.count}));const token=v.token;setTimeout(()=>{if(S.plateView&&S.plateView.token===token)send('plate_view',{plate:msg.plate,part:msg.part+1})},20)}
 else if(!v.quiet)status(t('st.plate_shown',{name:msg.name}))}
function renderPlateNav(){const n=S.plan&&S.plan.plates?S.plan.plates.length:0;$('plateNav').hidden=!n;if(!n)return;const i=Math.min(S.plateShown||0,n-1);$('plateShowText').textContent=t('view.plate_button',{n:i+1,total:n});$('platePrev').hidden=$('plateNext').hidden=n<2}
function stepPlate(d){const n=S.plan&&S.plan.plates?S.plan.plates.length:0;if(n)showPlate3d(((S.plateShown||0)+d+n)%n)}
function setView(view){if(S.view===view)return;S.view=view;invalidatePlan();renderBasket()}
function updateActions(){const n=inBasket()?S.basket.total:selectedStl().length;$('plan').disabled=S.busy||!n;$('planSmart').disabled=S.busy||!n;$('sendPrepare').disabled=S.busy||!(S.plan&&S.plan.exportable);$('nativeArrange').disabled=S.busy||!n;$('toBasket').disabled=S.busy||!selectedStl().length;$('partCount').textContent=inBasket()?String(n):S.parts.length?`${n} / ${S.parts.length}`:'0'}
function scheduleNativeTick(){if(S.nativeTick)return;S.nativeTick=setTimeout(()=>{S.nativeTick=null;send('native_arrange_tick')},700)}
function stopNativeTicks(){if(S.nativeTick)clearTimeout(S.nativeTick);S.nativeTick=null}
function requestScan(){if(S.busy)return;S.scanning=true;S.lastScan=Date.now();setBusy(true);status(t('st.scanning'));send('scan')}
function ask(title,body,kind,payload={},yes){S.pending={kind,payload};$('modalTitle').textContent=title;$('modalText').textContent=body;$('modalYes').textContent=yes||t('modal.continue');$('modal').hidden=false;$('modalYes').focus()}
function finishAsk(yes){const p=S.pending;S.pending=null;$('modal').hidden=true;if(p&&yes&&p.kind==='delete')send('delete_source',{path:p.payload.path})}
function openDrawer(open){$('drawer').classList.toggle('open',open);$('scrim').classList.toggle('open',open)}

/* settings */
// A theme sets its own colours; a dark one also switches the dark variants of icons and shadows,
// whatever Orca's theme is. "orca" leaves both to Orca.
const THEME_MODES={parchment:'light',terracotta:'light',sage:'light',blueprint:'light',dusk:'light',workshop:'dark',night:'dark'};
function applyTheme(name){const h=document.documentElement,mode=THEME_MODES[name];if(mode){h.dataset.ppTheme=name;h.dataset.ppMode=mode}else{delete h.dataset.ppTheme;delete h.dataset.ppMode}
 if(S.mesh)renderMesh();if(S.plan)renderPlan(S.plan)}
function isDarkUi(){const h=document.documentElement;return h.dataset.ppMode?h.dataset.ppMode==='dark':h.dataset.orcaTheme==='dark'}
function applyConfig(c){S.config=c||{};$('orcaOrient').checked=!!S.config.orca_orient;$('checkOnOpen').checked=S.config.check_on_open!==false;$('recursive').checked=S.config.recursive!==false;$('gpuPreview').checked=S.config.gpu_preview!==false;const types=S.config.model_types||['stl','step','3mf'];for(const [k,id] of [['stl','modelStl'],['step','modelStep'],['3mf','model3mf']]){$(id).checked=types.includes(k);$(id).parentElement.classList.toggle('on',types.includes(k))}if(S.config.margin_mm!=null)$('margin').value=S.config.margin_mm;$('language').value=S.config.language||'auto';$('theme').value=THEME_MODES[S.config.theme]?S.config.theme:'orca';applyTheme($('theme').value);setMode(S.config.arrange_mode||'own',false);renderFolders()}
function when(sec){const d=new Date(sec*1000),hm=d.toLocaleTimeString(locale(),{hour:'2-digit',minute:'2-digit'});return d.toDateString()===new Date().toDateString()?hm:d.toLocaleDateString(locale(),{day:'numeric',month:'short'})+' '+hm}
function folderMeta(f){if(!f||f.checked==null)return esc(t('folders.never'));if(f.missing)return `<span class="warn">${esc(t('folders.missing'))}</span>`;if(f.unreadable)return `<span class="warn">${esc(t('folders.unreadable'))}</span>`;const out=[esc(t('folders.stats',{sources:plural('folders.sources',f.sources||0),time:when(f.checked)}))];if(f.new)out.push(`<b>${esc(t('folders.new',{n:f.new}))}</b>`);if(f.other)out.push(esc(t('folders.other',{n:f.other})));if(f.empty)out.push(esc(t('folders.empty',{n:f.empty})));if(f.skipped)out.push(`<span class="warn">${esc(t('folders.skipped',{n:f.skipped}))}</span>`);if(f.truncated)out.push(`<span class="warn">${esc(t('folders.truncated',{n:f.truncated}))}</span>`);return out.join(' · ')}
function renderFolders(){const folders=S.config.folders||[],info=p=>S.folders.find(f=>f.path===p&&f.origin==='watched'),added=S.folders.find(f=>f.origin==='added');let html=folders.length?folders.map(p=>`<div class="folder">${icon('folder')}<div class="p">${esc(p)}<div class="fm">${folderMeta(info(p))}</div></div><button class="icon ghost" title="${esc(t('folders.remove'))}" data-remove="${esc(p)}">${icon('x')}</button></div>`).join(''):`<div class="muted">${esc(t('folders.none'))}</div>`;if(added)html+=`<div class="folder added">${icon('plus')}<div class="p">${esc(t('folders.added'))}<div class="fm">${folderMeta(added)} · ${esc(t('folders.added_note'))}</div></div></div>`;$('folderList').innerHTML=html;document.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>{if(S.busy)return;S.scanning=true;setBusy(true);send('remove_folder',{path:b.dataset.remove})})}
function saveUiSettings(){S.config.orca_orient=$('orcaOrient').checked;S.config.check_on_open=$('checkOnOpen').checked;S.config.recursive=$('recursive').checked;S.config.gpu_preview=$('gpuPreview').checked;S.config.model_types=['stl','step','3mf'].filter(k=>$(k==='stl'?'modelStl':k==='step'?'modelStep':'model3mf').checked);S.config.margin_mm=Math.max(0,Math.min(20,Number($('margin').value)||0));S.config.arrange_mode=S.mode;S.config.language=$('language').value;S.config.theme=$('theme').value;send('save_settings',{config:S.config})}

/* printer */
function setProfile(p){S.profile=p||null;if(!p||p.error){$('printer').innerHTML=`<span class="pill warn">${esc(t('printer.unreadable'))}</span><span class="name muted">${esc(p&&p.error||'')}</span>`;return}const s=p.size||{};$('printer').innerHTML=`<span class="name" title="${esc(p.name)}">${esc(p.name||'—')}</span><span class="spec"><span class="pill accent">${fmt(s.x)} × ${fmt(s.y)} ${esc(t('unit.mm'))}</span><span class="pill">Z ${fmt(p.height)}</span>${p.exclusions?`<span class="pill warn" title="${esc(t('printer.zones_tip'))}">${esc(t('printer.zones'))}</span>`:''}</span>`;if(S.mesh)renderMesh()}

/* library */
function kindClass(kind,added){return added?'k-added':kind==='zip'?'k-zip':'k-folder'}
function sourceRow(a){const badges=a.type_counts?Object.entries(a.type_counts).map(([k,v])=>`<span class="pill">${esc(k)} ${v}</span>`).join(''):`<span class="pill">${esc(t('src.unopened'))}</span>`;return `<div class="source ${S.selected===a.path?'on':''} ${a.likely===false?'other':''}" data-path="${esc(a.path)}" data-kind="${esc(a.kind)}"><div class="glyph ${kindClass(a.kind,a.added)}">${icon(a.kind==='zip'?'zip':'models')}</div><div class="t"><div class="n">${esc(a.name)}</div><div class="m">${a.new?`<span class="pill accent">${esc(t('src.new'))}</span>`:''}${a.added?`<span class="pill">${esc(t('src.added'))}</span>`:''}${badges}<span class="pill">${esc(a.size)}</span>${a.warning?`<span class="pill warn">${esc(a.warning)}</span>`:''}</div></div></div>`}
// The folder switch: every watched folder and PlatePilot's own folder of added files, when there are two or more.
function renderRoots(){const roots=(S.folders||[]).filter(f=>f.path),sel=$('rootFilter');if(S.root&&!roots.some(f=>f.path===S.root))S.root='';
 $('rootBar').hidden=roots.length<2;if(roots.length<2)return;const n=p=>S.archives.filter(x=>x.root===p&&x.likely!==false).length,base=p=>p.replace(/[\\/]+$/,'').split(/[\\/]/).pop()||p;
 sel.innerHTML=`<option value="">${esc(t('lib.all_folders'))} (${S.archives.filter(x=>x.likely!==false).length})</option>`+roots.map(f=>`<option value="${esc(f.path)}" title="${esc(f.path)}"${f.path===S.root?' selected':''}>${esc(f.origin==='added'?t('folders.added'):base(f.path))} (${n(f.path)})</option>`).join('');sel.value=S.root||''}
function renderArchives(){const q=$('filter').value.trim().toLowerCase();renderRoots();const match=x=>(!S.root||x.root===S.root)&&(S.kind==='all'||(S.kind==='new'?x.new:S.kind==='added'?x.added:x.kind===S.kind||(S.kind==='folder'&&x.kind==='file')));const found=S.archives.filter(x=>x.name.toLowerCase().includes(q)&&match(x));
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
function renderSourceHead(){if(!S.selected){$('sourceHead').innerHTML=`<div class="glyph">${icon('cube')}</div><div class="grow"><h2>${esc(t('src.choose'))}</h2><div class="sub">${esc(t('src.choose_note'))}</div></div>`;return}const stl=S.parts.filter(p=>p.filetype==='stl').length;const action=S.selectedAdded?`<button id="deleteSelected" class="ghost" title="${esc(t('src.delete_tip'))}">${icon('x')}${esc(t('src.delete'))}</button>`:`<button id="hideSelected" class="ghost" title="${esc(t('src.hide_tip'))}">${icon('eye-off')}${esc(t('src.hide'))}</button>`;$('sourceHead').innerHTML=`<div class="glyph ${kindClass(S.selectedKind,S.selectedAdded)}">${icon(S.selectedKind==='zip'?'zip':'models')}</div><div class="grow"><h2>${esc(S.selectedName)}</h2><div class="sub">${esc(t('src.summary',{n:S.parts.length,stl,path:S.selected}))}</div></div>${action}`;if(S.selectedAdded)$('deleteSelected').onclick=()=>ask(t('delete.title'),t('delete.body',{name:S.selectedName}),'delete',{path:S.selected},t('src.delete'));else $('hideSelected').onclick=()=>send('hide_source',{path:S.selected})}
function partMeta(p){if(Array.isArray(p.dims)&&p.dims.length===3&&p.filetype==='stl')return esc(t('parts.meta',{dims:p.dims.map(v=>fmt(v)).join(' × '),facets:Number(p.facets||0).toLocaleString(locale())}));return esc(t('parts.catalog_only',{type:(p.filetype||'model').toUpperCase()}))}
function renderParts(note){if(!S.parts.length){$('parts').innerHTML=`<div class="empty">${icon('cube')}<div>${esc(note||(S.selected?t('parts.no_files'):t('parts.placeholder')))}</div></div>`;updateActions();return}$('parts').innerHTML=S.parts.map((p,i)=>{const usable=meshPart(p);return `<div class="part ${usable?'':p.open_in_orca?'ext':'off'} ${S.previewIndex===i?'on':''}" data-index="${i}"><input class="check" type="checkbox" data-check="${i}" ${p.included?'checked':''} ${usable?'':'disabled'}><div class="t"><div class="n">${esc(p.name)}</div><div class="m">${partMeta(p)}${p.warning?` · <span class="warn">${esc(p.warning)}</span>`:''}${p.turned!=null?` · <span class="tip">${esc(t('orient.turned',{pct:p.turned}))}</span>`:p.advice?` · <span class="tip">${esc(t('orient.advice',{pct:p.advice.pct}))}</span>`:''}</div></div>${p.advice&&usable?`<button class="small" data-turn="${i}" data-on="${p.turned==null?1:0}">${esc(t(p.turned==null?'btn.turn':'btn.unturn'))}</button>`:''}${p.open_in_orca?`<button class="small" data-orca="${i}">${esc(t('btn.open_in_orca'))}</button>`:''}</div>`}).join('');
 document.querySelectorAll('[data-orca]').forEach(el=>el.onclick=e=>{e.stopPropagation();if(!S.busy)send('open_in_orca',{index:Number(el.dataset.orca)})});
 document.querySelectorAll('[data-turn]').forEach(el=>el.onclick=e=>{e.stopPropagation();if(!S.busy)send('turn_part',{index:Number(el.dataset.turn),on:el.dataset.on==='1'})});
 renderOrientBar();document.querySelectorAll('[data-check]').forEach(el=>{el.onclick=e=>e.stopPropagation();el.onchange=()=>{S.parts[Number(el.dataset.check)].included=el.checked;invalidatePlan();updateActions()}});document.querySelectorAll('.part:not(.off):not(.ext)').forEach(el=>el.onclick=()=>{S.previewIndex=Number(el.dataset.index);S.previewMember=null;S.plateView=null;document.querySelectorAll('.part').forEach(x=>x.classList.toggle('on',x===el));send('preview_part',{index:S.previewIndex,max_facets:previewBudget()})});updateActions()}
function invalidatePlan(){if(S.plan){S.plan=null;renderPlan(null)}}
// The orientation check runs part by part after a source opens; its advice shows in the rows.
function renderOrientBar(){const n=S.parts.filter(p=>p.advice&&p.turned==null).length,checking=S.orient&&S.orient.done<S.orient.total;
 $('orientBar').hidden=inBasket()||(!checking&&!n);$('orientNote').textContent=checking?t('orient.checking',{done:S.orient.done,total:S.orient.total}):n?plural('orient.done',n):'';
 $('turnAll').hidden=!n;$('turnAll').textContent=t('btn.turn_all',{n})}
function startOrientCheck(){S.orientToken=(S.orientToken||0)+1;S.orient={done:0,total:S.parts.filter(meshPart).length};renderOrientBar();if(S.orient.total)send('orient_check',{index:0,token:S.orientToken})}

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
function drawMesh(data){if(!data||!(data.vertices||data.floats)){S.mesh=null;drawEmptyView();return}let f=data.floats;if(!f){const bin=atob(data.vertices),arr=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)arr[i]=bin.charCodeAt(i);f=new Float32Array(arr.buffer,0,arr.length>>2)}let lo=[Infinity,Infinity,Infinity],hi=[-Infinity,-Infinity,-Infinity];for(let i=0;i<f.length;i+=3)for(let a=0;a<3;a++){lo[a]=Math.min(lo[a],f[i+a]);hi[a]=Math.max(hi[a],f[i+a])}S.mesh={f,lo,hi,yaw:.62,pitch:1.02,zoom:fitZoom(lo,hi),dims:data.dims,data};if(data.plate&&data.bed){S.mesh.center=[data.bed[0]/2,data.bed[1]/2];S.mesh.zoom=1}$('viewEmpty').hidden=true;$('viewHint').hidden=false;$('resetView').hidden=false;$('viewTitle').textContent=data.name;const shown=data.facets>f.length/9?t('view.shown',{n:Number(f.length/9).toLocaleString(locale())}):'';$('viewMeta').textContent=t('view.meta',{dims:data.dims.map(v=>fmt(v)).join(' × '),facets:Number(data.facets).toLocaleString(locale())})+shown;renderMesh()}
function renderMesh(){const m=S.mesh,c=$('view');if(!m)return;const dpr=window.devicePixelRatio||1,w=Math.max(1,c.clientWidth),h=Math.max(1,c.clientHeight);c.width=w*dpr;c.height=h*dpr;const ctx=c.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
const bx=Number(S.profile?.size?.x)||250,by=Number(S.profile?.size?.y)||250,f=m.f,cx=m.center?m.center[0]:(m.lo[0]+m.hi[0])/2,cy=m.center?m.center[1]:(m.lo[1]+m.hi[1])/2,z0=m.lo[2];
const cyw=Math.cos(m.yaw),syw=Math.sin(m.yaw),cp=Math.cos(m.pitch),sp=Math.sin(m.pitch);
const rot=(x,y,z)=>{const x1=cyw*x-syw*y,y1=syw*x+cyw*y;return [x1,cp*y1-sp*z,sp*y1+cp*z]};
const corners=[[-bx/2,-by/2,0],[bx/2,-by/2,0],[bx/2,by/2,0],[-bx/2,by/2,0]];const top=Math.max(1,m.hi[2]-m.lo[2]);const probe=corners.concat(corners.map(p=>[p[0],p[1],top])).map(p=>rot(...p));
const minX=Math.min(...probe.map(p=>p[0])),maxX=Math.max(...probe.map(p=>p[0])),minY=Math.min(...probe.map(p=>p[1])),maxY=Math.max(...probe.map(p=>p[1]));
const scale=Math.min((w-40)/(maxX-minX),(h-90)/(maxY-minY))*m.zoom,ox=w/2-(minX+maxX)/2*scale,oy=h/2+18-(minY+maxY)/2*scale;const P=p=>[ox+p[0]*scale,oy+p[1]*scale];
const dark=isDarkUi(),[ar,ag,ab]=accentRgb();
const bed=corners.map(p=>P(rot(...p)));ctx.beginPath();bed.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.closePath();ctx.fillStyle=dark?'rgba(255,255,255,.045)':'rgba(20,40,60,.045)';ctx.fill();
ctx.lineWidth=.6;for(let x=-bx/2;x<=bx/2+.01;x+=10){const major=Math.abs((x+bx/2)%50)<.01;ctx.strokeStyle=dark?`rgba(255,255,255,${major?.14:.05})`:`rgba(20,40,60,${major?.16:.06})`;const a=P(rot(x,-by/2,0)),b=P(rot(x,by/2,0));ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);ctx.stroke()}
for(let y=-by/2;y<=by/2+.01;y+=10){const major=Math.abs((y+by/2)%50)<.01;ctx.strokeStyle=dark?`rgba(255,255,255,${major?.14:.05})`:`rgba(20,40,60,${major?.16:.06})`;const a=P(rot(-bx/2,y,0)),b=P(rot(bx/2,y,0));ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);ctx.stroke()}
ctx.beginPath();bed.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.closePath();ctx.strokeStyle=`rgba(${ar},${ag},${ab},.85)`;ctx.lineWidth=1.6;ctx.stroke();
const origin=P(rot(-bx/2,-by/2,0)),ax=P(rot(-bx/2+Math.min(40,bx/5),-by/2,0)),ay=P(rot(-bx/2,-by/2+Math.min(40,by/5),0));ctx.lineWidth=2.2;ctx.strokeStyle='#e5484d';ctx.beginPath();ctx.moveTo(...origin);ctx.lineTo(...ax);ctx.stroke();ctx.strokeStyle='#30a46c';ctx.beginPath();ctx.moveTo(...origin);ctx.lineTo(...ay);ctx.stroke();
const hx=(m.hi[0]-m.lo[0])/2,hy=(m.hi[1]-m.lo[1])/2,shadow=[[-hx,-hy,0],[hx,-hy,0],[hx,hy,0],[-hx,hy,0]].map(p=>P(rot(...p)));ctx.beginPath();shadow.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.closePath();if(!m.center){ctx.fillStyle='rgba(0,0,0,.13)';ctx.filter='blur(6px)';ctx.fill();ctx.filter='none'}
if(!glDraw(m,{w,h,dpr,ox,oy,scale,cyw,syw,cp,sp,cx,cy,z0,dark,rgb:[ar,ag,ab]})){const L=[.35,-.55,.76],ln=Math.hypot(...L),lx=L[0]/ln,ly=L[1]/ln,lz=L[2]/ln,tris=[];
for(let i=0;i+8<f.length;i+=9){const a=rot(f[i]-cx,f[i+1]-cy,f[i+2]-z0),b=rot(f[i+3]-cx,f[i+4]-cy,f[i+5]-z0),d=rot(f[i+6]-cx,f[i+7]-cy,f[i+8]-z0);const ux=b[0]-a[0],uy=b[1]-a[1],uz=b[2]-a[2],vx=d[0]-a[0],vy=d[1]-a[1],vz=d[2]-a[2];let nx=uy*vz-uz*vy,ny=uz*vx-ux*vz,nz=ux*vy-uy*vx;const nl=Math.hypot(nx,ny,nz)||1;nx/=nl;ny/=nl;nz/=nl;const lit=Math.abs(nx*lx+ny*ly+nz*lz);tris.push({a:P(a),b:P(b),d:P(d),z:a[2]+b[2]+d[2],k:.32+.68*lit,rim:Math.pow(1-Math.abs(nz),3)})}
tris.sort((p,q)=>p.z-q.z);for(const tr of tris){const k=tr.k,r=Math.min(255,ar*k+40*tr.rim+(dark?14:0)),g=Math.min(255,ag*k+40*tr.rim+(dark?14:0)),b=Math.min(255,ab*k+48*tr.rim+(dark?18:0));ctx.beginPath();ctx.moveTo(...tr.a);ctx.lineTo(...tr.b);ctx.lineTo(...tr.d);ctx.closePath();ctx.fillStyle=`rgb(${r|0},${g|0},${b|0})`;ctx.fill();ctx.strokeStyle=ctx.fillStyle;ctx.lineWidth=.5;ctx.stroke()}}
ctx.fillStyle=dark?'rgba(255,255,255,.55)':'rgba(30,40,50,.55)';ctx.font='11px system-ui,sans-serif';ctx.textAlign='right';ctx.fillText(`${fmt(bx)} × ${fmt(by)} ${t('unit.mm')}`,w-12,h-12)}

/* plan */
function renderPlan(plan){const same=!!plan&&plan===S.plan;S.plan=plan;if(!same){S.plateView=null;S.plateShown=0}renderPlateNav();updateActions();if(!$('zoom').hidden)closeZoom();if(!plan){$('zoomHint').hidden=true;$('summary').innerHTML='';$('plates').innerHTML='';return}const bx=plan.bed.x,by=plan.bed.y;const pills=[`<span class="pill accent">${esc(plural('plan.parts',plan.total))}</span>`,`<span class="pill accent">${esc(plural('plan.plates',plan.plates.length))}</span>`].concat((plan.problems||[]).map(p=>`<span class="pill warn">${esc(t('plan.not_fit',{name:p}))}</span>`)).concat((plan.too_tall||[]).map(p=>`<span class="pill warn">${esc(t('plan.too_tall',{name:p}))}</span>`));if(plan.exclusions)pills.push(`<span class="pill warn">${esc(t('plan.zones'))}</span>`);if(plan.count_note==='at_least')pills.push(`<span class="pill warn">${esc(t('plan.at_least',{plates:plural('plan.plates',plan.min_plates)}))}</span>`);if(plan.count_note==='at_most')pills.push(`<span class="pill warn">${esc(t('plan.at_most'))}</span>`);if(plan.smart===true)pills.unshift(`<span class="pill accent">${esc(t('plan.smart'))}</span>`);if(plan.plates.length)pills.push(`<button id="plan3d" class="primary small view3d-all" title="${esc(t('plan.view3d_all_tip'))}"><svg class="ic"><use href="#i-cube"/></svg>${esc(t('plan.view3d_all'))}</button>`);$('summary').innerHTML=pills.join('');if($('plan3d'))$('plan3d').onclick=()=>showPlate3d(S.plateShown||0);
$('plates').innerHTML=plan.plates.map((p,pi)=>`<div class="plate" data-plate="${pi}"><div class="plate-head"><b>${esc(t('plan.plate',{n:pi+1}))}</b><span>${esc(plateMeta(p))}</span><button class="primary small view3d" data-view3d="${pi}" title="${esc(t('plan.view3d_tip'))}"><svg class="ic"><use href="#i-cube"/></svg>${esc(t('plan.view3d'))}</button></div>${plateSvg(p,pi,false)}</div>`).join('');
 document.querySelectorAll('[data-view3d]').forEach(b=>b.onclick=e=>{e.stopPropagation();showPlate3d(Number(b.dataset.view3d))});
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
function setMode(mode,save=true){S.mode=mode==='orca'?'orca':'own';document.querySelectorAll('#modeSeg button').forEach(b=>b.classList.toggle('on',b.dataset.mode===S.mode));const own=S.mode==='own';$('marginField').hidden=!own;$('platesField').hidden=!own;$('plan').hidden=!own;$('planSmart').hidden=!own;$('sendPrepare').hidden=!own;$('nativeArrange').hidden=own;$('orientField').hidden=own;$('modeNote').textContent=t(own?'mode.own_note':'mode.orca_note');if(!own)renderPlan(null);if(save)saveUiSettings()}

/* wiring */
$('openSettings').onclick=()=>openDrawer(true);$('support').onclick=()=>send('open_support');$('closeSettings').onclick=()=>openDrawer(false);$('scrim').onclick=()=>openDrawer(false);
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(!$('modal').hidden)finishAsk(false);else openDrawer(false)}});
$('pickFolder').onclick=()=>{if(S.busy)return;S.scanning=true;status(t('st.opening_picker'));send('pick_folder',{initial:$('folder').value.trim()||(S.config.folders||[])[0]||''})};
$('addFolder').onclick=()=>{const path=$('folder').value.trim();if(path&&!S.busy){S.scanning=true;setBusy(true);send('request_folder_add',{path})}};$('folder').onkeydown=e=>{if(e.key==='Enter')$('addFolder').click()};
const pickFiles=()=>{if(!S.busy){openDrawer(false);$('filePick').value='';$('filePick').click()}};$('addFiles').onclick=pickFiles;$('addFiles2').onclick=pickFiles;
$('filePick').onchange=()=>{const files=Array.from($('filePick').files||[]);if(files.length)importFiles(files.map(f=>({file:f,top:'',rel:f.name})))};
$('turnAll').onclick=()=>{if(!S.busy)send('turn_all')};$('orcaOrient').onchange=saveUiSettings;
$('scan').onclick=requestScan;$('filter').oninput=renderArchives;$('rootFilter').onchange=()=>{S.root=$('rootFilter').value;renderArchives()};$('markSeen').onclick=()=>send('mark_seen');
document.querySelectorAll('#kindChips .chip').forEach(b=>b.onclick=()=>{S.kind=b.dataset.kind;document.querySelectorAll('#kindChips .chip').forEach(x=>x.classList.toggle('on',x===b));renderArchives()});
for(const id of ['checkOnOpen','recursive'])$(id).onchange=saveUiSettings;
$('gpuPreview').onchange=()=>{saveUiSettings();glClear();if(S.mesh&&S.previewIndex>=0)send('preview_part',{index:S.previewIndex,max_facets:previewBudget()});else renderMesh()};
for(const id of ['modelStl','modelStep','model3mf'])$(id).onchange=()=>{$(id).parentElement.classList.toggle('on',$(id).checked);saveUiSettings()};
$('language').onchange=()=>{saveUiSettings();send('init')};
$('theme').onchange=()=>{applyTheme($('theme').value);saveUiSettings()};
$('margin').onchange=()=>{invalidatePlan();saveUiSettings()};
$('all').onclick=()=>{S.parts.forEach(p=>{if(p.filetype==='stl'&&p.member&&!p.blocked)p.included=true});invalidatePlan();renderParts()};$('none').onclick=()=>{S.parts.forEach(p=>p.included=false);invalidatePlan();renderParts()};
document.querySelectorAll('#modeSeg button').forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
// Empty or 0 is "auto": as few plates as the parts fit on.
function wantedPlates(){const v=Math.round(Number($('plateCount').value));return v>0?Math.min(50,v):0}
$('plateCount').onchange=()=>{const v=wantedPlates();$('plateCount').value=v||''};
$('plan').onclick=()=>{renderPlan(null);send('plan',{margin:Number($('margin').value)||0,plates:wantedPlates(),included:selectedStl().map(p=>p.member),basket:inBasket()})};
$('planSmart').onclick=()=>{renderPlan(null);send('plan',{margin:Number($('margin').value)||0,plates:wantedPlates(),included:selectedStl().map(p=>p.member),smart:true,basket:inBasket()})};
$('toBasket').onclick=()=>{const m=selectedStl().map(p=>p.member);if(m.length)send('basket_add',{members:m})};
$('basketTab').onclick=()=>setView(inBasket()?'source':'basket');
$('plateShow').onclick=()=>showPlate3d(S.plateShown||0);$('platePrev').onclick=()=>stepPlate(-1);$('plateNext').onclick=()=>stepPlate(1);
$('zoom3d').onclick=()=>{const pi=S.zoom;closeZoom();if(pi!=null)showPlate3d(pi)};
$('sendPrepare').onclick=()=>{if(!S.busy){status(t('st.building'));send('export_project')}};
$('nativeArrange').onclick=()=>{if(!S.busy)send('native_arrange',{included:selectedStl().map(p=>p.member),basket:inBasket()})};
$('modalYes').onclick=()=>finishAsk(true);$('modalNo').onclick=()=>finishAsk(false);
const view=$('view');view.onpointerdown=e=>{if(!S.mesh)return;S.drag={x:e.clientX,y:e.clientY};view.setPointerCapture(e.pointerId)};view.onpointermove=e=>{if(!S.drag||!S.mesh)return;S.mesh.yaw+=(e.clientX-S.drag.x)*.01;S.mesh.pitch=Math.max(.05,Math.min(1.55,S.mesh.pitch+(e.clientY-S.drag.y)*.01));S.drag={x:e.clientX,y:e.clientY};renderMesh()};view.onpointerup=()=>S.drag=null;
view.onwheel=e=>{if(!S.mesh)return;e.preventDefault();S.mesh.zoom=Math.max(.4,Math.min(6,S.mesh.zoom*(e.deltaY<0?1.12:1/1.12)));renderMesh()};
const resetView=()=>{if(S.mesh){S.mesh.yaw=.62;S.mesh.pitch=1.02;S.mesh.zoom=S.mesh.center?1:fitZoom(S.mesh.lo,S.mesh.hi);renderMesh()}};view.ondblclick=resetView;$('resetView').onclick=resetView;
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
 if(k==='init'){S.strings=msg.strings||{};S.lang=msg.language||'en';S.platform=msg.platform||'windows';applyStrings();const win=S.platform==='windows';$('pickFolder').hidden=!win;$('pasteNote').textContent=t(win?'drawer.paste':'drawer.paste_only');applyConfig(msg.config||{});setProfile(msg.profile);renderSourceHead();renderParts();renderArchives();renderFolders();renderBasket();if(S.plan){const shown=S.mesh&&S.mesh.center?S.plateShown||0:-1;renderPlan(S.plan);
  if(shown>=0&&S.plan.plates[shown]){const keep={yaw:S.mesh.yaw,pitch:S.mesh.pitch,zoom:S.mesh.zoom,center:S.mesh.center,plateToken:S.mesh.plateToken};S.mesh.data.name=t('plan.plate',{n:shown+1});drawMesh(S.mesh.data);Object.assign(S.mesh,keep);renderMesh();
   $('viewMeta').textContent=t('view.plate_meta',{parts:plural('plan.parts',S.plan.plates[shown].items.length),bed:`${fmt(S.plan.bed.x)} × ${fmt(S.plan.bed.y)} ${t('unit.mm')}`})}
  else if(S.mesh)drawMesh(S.mesh.data);else drawEmptyView()}
 else if(S.mesh)drawMesh(S.mesh.data);else drawEmptyView();if(msg.scan_interrupted&&!S.lastScan)status(t('st.scan_interrupted'),'warn');else if(!S.lastScan&&S.config.check_on_open!==false)requestScan();else if(!S.lastScan)status(t((msg.config?.folders||[]).length?'st.press_refresh':'st.choose_folder'))}
 else if(k==='config'){applyConfig(msg.config||{})}
 else if(k==='profile'){setProfile(msg.profile)}
 else if(k==='archives'){S.archives=msg.archives||[];S.folders=msg.folders||[];renderArchives();renderFolders();if(S.scanning){S.scanning=false;if(!S.importing)setBusy(false);const n=S.archives.filter(a=>a.new).length,sources=plural('folders.sources',S.archives.filter(a=>a.likely!==false).length);if(!S.archives.length&&!S.folders.length)status(t('st.choose_folder'));else status(n?t('st.scanned_new',{sources,n}):t('st.scanned',{sources}),'ok')}}
 else if(k==='folder_selected'){if(msg.path)$('folder').value=msg.path}
 else if(k==='folder_list'){S.config.folders=msg.folders||[];renderFolders();renderArchives()}
 else if(k==='basket'){const grew=msg.total>(S.basket.total||0);S.basket=msg;if(inBasket())invalidatePlan();renderBasket();if(grew&&!inBasket())status(t('basket.added',{n:msg.total}),'info')}
 else if(k==='archive'){S.view='source';S.previewMember=null;S.selected=msg.path;S.selectedKind=msg.kind;S.selectedName=msg.name;S.selectedAdded=!!msg.added;S.parts=msg.parts||[];S.previewIndex=-1;S.mesh=null;if(!S.importing)setBusy(false);renderSourceHead();renderArchives();renderParts();renderBasket();renderPlan(null);drawEmptyView();status(msg.error||t('st.opened_source',{name:msg.name}),msg.error?'warn':'info');startOrientCheck()}
 else if(k==='orient_advice'){if(msg.index>=0&&S.parts[msg.index]){S.parts[msg.index].advice=msg.advice||null}S.orient={done:msg.done,total:msg.total};if(msg.next>=0){const token=S.orientToken;setTimeout(()=>{if(token===S.orientToken)send('orient_check',{index:msg.next})},20)}renderParts()}
 else if(k==='parts_turned'){for(const c of msg.parts||[]){const p=S.parts[c.index];if(!p)continue;p.dims=c.dims;p.turned=c.turned?p.advice.pct:null}invalidatePlan();renderParts();if(S.previewIndex>=0&&(msg.parts||[]).some(c=>c.index===S.previewIndex))send('preview_part',{index:S.previewIndex,max_facets:previewBudget()})}
 else if(k==='import_next'){if(S.importWait){const w=S.importWait;S.importWait=null;w.res(msg)}}
 else if(k==='preview'){S.plateView=null;drawMesh(msg)}
 else if(k==='plate_part'){plateChunk(msg)}
 else if(k==='plan'){setProfile(msg.profile);renderPlan(msg);status(t(msg.exportable?'st.plan_ready':'st.plan_problems'),msg.exportable?'ok':'warn');
  // The first plate in 3D at once when it comes whole (no part needs simplifying, about 0.1 s a part).
  const first=msg.plates&&msg.plates[0];if(first&&first.facets&&first.facets<=(msg.view_budget||0))setTimeout(()=>{if(S.plan===msg)showPlate3d(0,true)},30)}
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


def _scan_folder(folder, recursive=True, split_root=False):
    """Rows for the ZIP archives and model folders under folder, found by their names only.

    Nothing is opened here. Orca asks before a plugin reads a file, so a scan that opened every
    ZIP asked about archives that turned out to hold no models. Listing the tree takes a single
    os.walk(), which Orca grants once per watched folder and which covers every subfolder while
    it runs, so the whole walk has to happen within one call. Subfolders whose path Orca cannot
    check (see _orca_can_read) are not entered. With split_root, every model file lying in folder
    itself is a source of its own (kind "file"): a downloads folder collects unrelated models
    there. Returns (rows, truncated, skipped folders).
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
        if split_root and current == folder:
            for name in model_files:
                path = os.path.join(current, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                row = {"kind": "file", "name": name, "path": os.path.realpath(path), "root": folder,
                       "fingerprint": f"{stat.st_size}:{getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1e9))}",
                       "size": _fmt_size(stat.st_size), "count": 1, "type_counts": _type_counts([name]), "files": [name],
                       "time": _freshness(stat)}
                if not _orca_can_read(path):
                    row["warning"] = _t("warn.unsafe_name")
                rows.append(row)
                if len(rows) >= MAX_ARCHIVES:
                    return rows, True, skipped
            model_files = []
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


def _snapshot_folder(source, names, cache_root, keep=(), label=""):
    """Copy the STL files of a watched model folder into the plugin's own folder and return the copy.

    Reading the files one by one makes Orca ask about every file. shutil.copytree() is a single
    operation that Orca asks about once per folder, and the copy inside the plugin's data folder
    can then be read freely. The copy is kept while its files match the folder's.
    """
    key = _folder_key(source) + ("|" + label if label else "")
    target = os.path.join(cache_root, hashlib.sha1(key.encode("utf-8")).hexdigest()[:12])
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
    keep = {os.path.normcase(os.path.realpath(k)) for k in keep}
    for old in copies[FOLDER_CACHE_KEEP:]:
        if os.path.normcase(os.path.realpath(old)) not in keep:   # the basket still reads from it
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
        raise ValueError(_t("err.3mf_transform"))
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
                raise ValueError(_t("err.3mf_nesting"))
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


ORIENT_SAMPLE = 15000   # facets looked at per part; a uniform sample of bigger meshes is enough to compare
ORIENT_OVERHANG = math.cos(math.radians(45))   # a face whose normal points further down than this needs support
ORIENT_BED = 0.3   # mm above the lowest point that still counts as lying on the bed


def _sample_triangles(raw, limit=ORIENT_SAMPLE):
    """(triangles as 9-tuples, weight): every facet of a small mesh, or every n-th of a big one
    with weight n, so sums of facet areas stay in mm²."""
    if len(raw) >= 84:
        count = struct.unpack_from("<I", raw, 80)[0]
        if 0 < count <= 2_000_000 and 84 + count * 50 == len(raw):
            step = max(1, count // limit)
            return [struct.unpack_from("<9f", raw, 84 + i * 50 + 12) for i in range(0, count, step)], step
    triangles = list(_iter_stl_triangles(raw))
    step = max(1, len(triangles) // limit)
    return triangles[::step], step


def _rotation_to_down(d):
    """The 3x3 rotation (rows) that turns direction d to (0, 0, -1): that side then lies on the bed."""
    x, y, z = d
    c = -z                      # d . (0, 0, -1)
    if c > 1 - 1e-9:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    if c < -1 + 1e-9:           # upside down: half a turn about X
        return ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
    vx, vy, vz = -y, x, 0.0     # d x (0, 0, -1)
    k = 1.0 / (1.0 + c)
    return ((1 - k * (vy * vy + vz * vz), -vz + k * vx * vy, vy + k * vx * vz),
            (vz + k * vx * vy, 1 - k * (vx * vx + vz * vz), -vx + k * vy * vz),
            (-vy + k * vx * vz, vx + k * vy * vz, 1 - k * (vx * vx + vy * vy)))


def _orientation_advice(raw):
    """Whether a part would print better lying another way, or None.

    Like Orca's Auto orient, in short: the sides a part could lie on are the six axis sides and
    its largest flat faces; for each, the area of faces that would hang over air steeper than
    45° from level, and the area lying on the bed. A part is only flagged when it lies clearly
    worse than it could (upside down, on a narrow edge): at least 40% and 2% of its surface less
    overhang, so a part its designer laid out on purpose is left alone.
    """
    triangles, weight = _sample_triangles(raw)
    faces = []
    total = 0.0
    for t in triangles:
        ux, uy, uz = t[3] - t[0], t[4] - t[1], t[5] - t[2]
        vx, vy, vz = t[6] - t[0], t[7] - t[1], t[8] - t[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length <= 1e-12:
            continue
        area = 0.5 * length * weight
        total += area
        faces.append((nx / length, ny / length, nz / length, area, t))
    if not faces or total <= 0:
        return None
    candidates = [(0.0, 0.0, -1.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, -1.0, 0.0)]
    for nx, ny, nz, _area, _t in sorted(faces, key=lambda f: -f[3])[:40]:
        if all(nx * a + ny * b + nz * c < 0.985 for a, b, c in candidates):
            candidates.append((nx, ny, nz))
        if len(candidates) >= 12:
            break

    def judge(d):
        dx, dy, dz = d
        low = min(-(dx * v[0] + dy * v[1] + dz * v[2]) for _, _, _, _, t in faces for v in (t[0:3], t[3:6], t[6:9]))
        overhang = contact = 0.0
        high = low
        for nx, ny, nz, area, t in faces:
            up = -(nx * dx + ny * dy + nz * dz)           # the face normal's height component once d is down
            heights = (-(dx * t[0] + dy * t[1] + dz * t[2]), -(dx * t[3] + dy * t[4] + dz * t[5]), -(dx * t[6] + dy * t[7] + dz * t[8]))
            top = max(heights)
            high = max(high, top)
            if top - low < ORIENT_BED:
                if up < -0.95:
                    contact += area
            elif up < -ORIENT_OVERHANG:
                overhang += area
        # Too little to stand on is as bad as an overhang of the missing area.
        return overhang + max(0.0, 0.03 * total - contact), overhang, high - low

    scores = [judge(d) for d in candidates]
    now = scores[0]
    best = min(range(len(candidates)), key=lambda i: (scores[i][0], scores[i][2]))
    cost, overhang, height = scores[best]
    if best == 0 or now[0] <= 0 or cost > 0.6 * now[0] or now[1] - overhang < max(30.0, 0.02 * total):
        return None
    return {"down": [round(v, 6) for v in candidates[best]],
            "pct": int(round(100 * (1 - overhang / now[1]))) if now[1] > 0 else 0,
            "overhang_now": round(now[1], 1), "overhang_new": round(overhang, 1),
            "height_now": round(now[2], 2), "height_new": round(height, 2)}


def _turned_stl(raw, rows):
    """A binary STL rotated by the 3x3 rotation `rows` (facet normals left for Orca to work out)."""
    out = bytearray(84)
    out[0:80] = b"PlatePilot turned".ljust(80)
    count = 0
    (a, b, c), (d, e, f), (g, h, i) = rows
    pack = STL_RECORD.pack
    pieces = []
    for tri in _iter_stl_triangles(raw):
        pieces.append(pack(0.0, 0.0, 0.0,
                           a * tri[0] + b * tri[1] + c * tri[2], d * tri[0] + e * tri[1] + f * tri[2], g * tri[0] + h * tri[1] + i * tri[2],
                           a * tri[3] + b * tri[4] + c * tri[5], d * tri[3] + e * tri[4] + f * tri[5], g * tri[3] + h * tri[4] + i * tri[5],
                           a * tri[6] + b * tri[7] + c * tri[8], d * tri[6] + e * tri[7] + f * tri[8], g * tri[6] + h * tri[7] + i * tri[8], 0))
        count += 1
    struct.pack_into("<I", out, 80, count)
    return bytes(out) + b"".join(pieces)


BASKET_MAX_COPIES = 50
MAX_PLATE_VIEW_FACETS = MAX_PREVIEW_FACETS   # a whole plate in the 3D view, as much as one part's preview


def _plate_shares(counts, budget=None):
    """How many facets each part of a plate keeps in the 3D view: parts smaller than an equal
    share keep all of theirs, and what they leave goes to the bigger ones. A plate within the
    budget is shown whole, with nothing simplified."""
    budget = budget or MAX_PLATE_VIEW_FACETS
    shares = [0] * len(counts)
    left = budget
    for done, i in enumerate(sorted(range(len(counts)), key=lambda k: counts[k] or budget)):
        fair = max(1500, left // (len(counts) - done))
        shares[i] = min(counts[i], fair) if counts[i] else fair
        left = max(0, left - shares[i])
    return shares


def _placed_mesh(item, raw, share, simplified=None):
    """One planned part as float32 triangles in bed millimetres: simplified to `share` facets,
    turned and placed the way _write_orca_project() places it. `simplified`, if given, is a
    cache of simplified meshes (simplifying a big part takes about a second)."""
    import array
    key = (len(raw), hashlib.blake2b(raw, digest_size=16).digest(), share)
    vertices = simplified.get(key) if simplified is not None else None
    if vertices is None:
        vertices = _binary_stl_info(raw, include_preview=True, max_facets=share)["vertices"] or ""
        if simplified is not None:
            simplified[key] = vertices
            while len(simplified) > 64:
                simplified.pop(next(iter(simplified)))
    flat = array.array("f")
    flat.frombytes(base64.b64decode(vertices))
    if sys.byteorder != "little":
        flat.byteswap()
    angle = math.radians(float(item.get("angle", 0)) + (90 if item.get("rotated") else 0))
    c, s = math.cos(angle), math.sin(angle)
    if abs(s) < 1e-12 or abs(c) < 1e-12:
        c, s = round(c), round(s)
    xs = [flat[i] * c - flat[i + 1] * s for i in range(0, len(flat), 3)]
    ys = [flat[i] * s + flat[i + 1] * c for i in range(0, len(flat), 3)]
    zs = flat[2::3]
    if not xs:
        return b"", 0
    # Centred in the cell the plan gave it, standing on the bed, like the project.
    dx = float(item["x"]) + float(item["w"]) / 2 - (min(xs) + max(xs)) / 2
    dy = float(item["y"]) + float(item["h"]) / 2 - (min(ys) + max(ys)) / 2
    dz = -min(zs)
    out = array.array("f")
    for x, y, z in zip(xs, ys, zs):
        out.extend((x + dx, y + dy, z + dz))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes(), len(xs) // 3


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


def _project_base(source_name):
    """A project's name from its source: the archive without ".zip", or the folder's name; with
    only what Windows allows in a file name, and transliterated where this Orca build cannot
    take non-Latin letters (see _orca_can_read)."""
    name = re.split(r"[\\/]", str(source_name or "").rstrip("\\/"))[-1]   # either separator, on any system
    if name.lower().endswith(".zip"):
        name = name[:-4]
    if _orca_handles_non_latin():
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    else:
        name = _safe_name(name, "")
    return name[:80] or "PlatePilot"


def _write_orca_project(path, plates, model_blobs, profile, app_version, project_settings, title="PlatePilot layout"):
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
                 '<metadata name="Title">' + escape(title) + '</metadata><resources>')
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
        config["orca_orient"] = bool(config.get("orca_orient", False))
        if config.get("language") not in ("auto",) + LANGUAGES:
            config["language"] = "auto"
        if config.get("theme") not in THEMES:
            config["theme"] = "orca"
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

    def _project_path(self, source_name):
        """Where to write the project of the open source: "<source> - PlatePilot.3mf" in the
        plugin's projects folder. The same name is reused while the file there is the one
        PlatePilot last wrote (Orca's recent list then keeps one entry per source); once it was
        changed, saved over in Orca for one, the next number is taken instead of replacing it."""
        folder = self._storage("projects")
        base = _project_base(source_name) + " - PlatePilot"
        written = self._library().setdefault("projects", {})
        for n in range(1, 100):
            path = os.path.join(folder, base + ("" if n == 1 else f" ({n})") + ".3mf")
            if not os.path.exists(path):
                return path
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if written.get(path) == [stat.st_size, stat.st_mtime_ns]:
                return path
        return os.path.join(folder, base + time.strftime(" %Y%m%d-%H%M%S") + ".3mf")

    def _remember_project(self, path):
        stat = os.stat(path)
        written = self._library().setdefault("projects", {})
        written[path] = [stat.st_size, stat.st_mtime_ns]
        for gone in [p for p in written if not os.path.exists(p)]:
            del written[gone]
        self._save_library()

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
                found, truncated, skipped = _scan_folder(folder, config.get("recursive", True) or origin == "added",
                                                         split_root=origin != "added")
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
            # A file source is its folder limited to that one file.
            folder = os.path.dirname(path) if kind == "file" else path
            if not _safe_archive_path(folder, root) or (kind == "file" and not _model_type_for_name(path)):
                raise ValueError(_t("err.folder_outside"))
            names = ([os.path.basename(path)] if kind == "file" else
                     row["files"] if row else sorted(n for n in os.listdir(path) if _model_type_for_name(n)))
            unsafe = [n for n in names if not _orca_can_read(os.path.join(folder, n))]
            readable = [n for n in names if n not in unsafe and _model_type_for_name(n) in ("stl", "3mf")
                        and os.path.getsize(os.path.join(folder, n)) <= MAX_STL_BYTES]
            catalog = tuple((n, _t("warn.unsafe_name") if n in unsafe else "") for n in names if n not in readable)
            copy = _snapshot_folder(folder, readable, self._storage("folders"),
                                    keep=[e["source"][0] for e in self._basket_entries() if e["source"][1] == "folder"],
                                    label=names[0] if kind == "file" else "")
            source = (copy, "folder", copy, catalog)
            parts = _read_loose_folder(copy, copy, catalog)
            # STEP files are not copied: their part points at the original, which Orca opens.
            steps = {n for n in names if _model_type_for_name(n) == "step"}
            for part in parts:
                if part["filetype"] == "step" and part["name"] in steps:
                    part["member"] = os.path.join(folder, part["name"])
                    part["open_in_orca"] = part["name"] not in unsafe
        self._selected_source = source
        self._selected_name = row["name"] if row is not None else os.path.basename(path)
        self._last_plan = None
        self._open_parts = parts
        self._advice, self._turns, self._turned = {}, {}, {}
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
        parts = _read_archive(path, root) if kind == "zip" else _read_loose_folder(path, root, catalog)
        # A turned part has the size it has lying the new way.
        for part in parts:
            turned = getattr(self, "_turned", {}).get(part.get("member"))
            if turned and part.get("member") in getattr(self, "_turns", {}):
                part["dims"], part["turned"] = list(turned[1]), self._advice[part["member"]]["pct"]
        return parts

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

    def _read_stl_blobs(self, source, parts, members, turned=True):
        """The meshes of the selected parts as binary STL, read again from the current source with
        the safety checks; a 3MF object comes out of its file (read and converted once), and a
        turned part lies the way it was turned (unless turned=False)."""
        blobs = self._read_raw_blobs(source, parts, members)
        if turned:
            for member in members:
                if member in getattr(self, "_turns", {}):
                    blobs[member] = self._turned_blob(member, blobs[member])
        return blobs

    def _turned_blob(self, member, raw):
        cached = self._turned.get(member)
        key = (len(raw), hashlib.blake2b(raw, digest_size=16).digest(), self._turns[member])
        if not cached or cached[0] != key:
            stl = _turned_stl(raw, self._turns[member])
            cached = self._turned[member] = (key, _binary_stl_info(stl)["dims"], stl)
        return cached[2]

    def _read_raw_blobs(self, source, parts, members):
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

    # The basket: parts from several sources, laid out together. Each entry remembers where its
    # part came from and how it was turned; it is read from there again, with the usual checks,
    # whenever it is laid out or exported. The basket lasts while OrcaSlicer runs.
    def _basket_entries(self):
        if getattr(self, "_basket", None) is None:
            self._basket, self._basket_version, self._basket_next = [], 0, 1
        return self._basket

    def _basket_post(self):
        entries = self._basket_entries()
        self.post_message({"type": "basket", "version": self._basket_version, "total": sum(e["copies"] for e in entries),
                           "items": [{"id": e["id"], "name": e["name"], "source_name": e["source_name"], "dims": e["dims"],
                                      "copies": e["copies"], "turned": bool(e["turn"])} for e in entries]})

    def _basket_changed(self):
        self._basket_version += 1
        if getattr(self, "_last_plan", None) and self._last_plan.get("basket"):
            self._last_plan = None
        self._basket_post()

    def _basket_add(self, config, members):
        source = getattr(self, "_selected_source", None)
        if not source:
            raise ValueError(_t("err.select_source"))
        entries = self._basket_entries()
        members = set(str(m) for m in members)
        raw_dims = {part.get("member"): part.get("dims") for part in getattr(self, "_open_parts", [])}
        name = getattr(self, "_selected_name", "") or os.path.basename(source[0])
        for part in self._current_parts(config):
            member = part.get("member")
            if member not in members or part.get("filetype") not in ("stl", "3mf") or part.get("blocked"):
                continue
            turn = getattr(self, "_turns", {}).get(member)
            if any(e["source"][0] == source[0] and e["member"] == member and e["turn"] == turn for e in entries):
                continue   # already there: the copies field asks for more
            entries.append({"id": self._basket_next, "source": source, "source_name": name, "member": member,
                            "name": part["name"], "filetype": part["filetype"], "dims": list(part["dims"]),
                            "raw_dims": list(raw_dims.get(member) or part["dims"]), "facets": part.get("facets", 0),
                            "turn": turn, "copies": 1})
            self._basket_next += 1
        self._basket_changed()

    def _basket_set(self, entry_id, copies=None, remove=False):
        entries = self._basket_entries()
        for entry in list(entries):
            if entry["id"] == entry_id:
                if remove:
                    entries.remove(entry)
                else:
                    entry["copies"] = max(1, min(BASKET_MAX_COPIES, int(copies)))
        self._basket_changed()

    def _basket_name(self):
        names = []
        for entry in self._basket_entries():
            if entry["source_name"] not in names:
                names.append(entry["source_name"])
        if len(names) <= 2:
            return " + ".join(_project_base(n) for n in names) or _t("basket.title")
        return _t("basket.and_more", name=_project_base(names[0]), n=len(names) - 1)

    def _read_basket_blobs(self, members):
        """The basket's parts as binary STL, each read again from its own source with the same
        checks as an open source; a part changed on disk since it went in is refused."""
        entries = {e["id"]: e for e in self._basket_entries()}
        listings, by_entry, blobs = {}, {}, {}
        for member in members:
            try:
                _, entry_id, _copy = str(member).split(":")
                entry = entries[int(entry_id)]
            except (ValueError, KeyError):
                raise ValueError(_t("err.part_missing"))
            if entry["id"] not in by_entry:
                source = entry["source"]
                if source[0] not in listings:
                    path, kind, root, catalog = source
                    try:
                        listings[source[0]] = _read_archive(path, root) if kind == "zip" else _read_loose_folder(path, root, catalog)
                    except (OSError, zipfile.BadZipFile):
                        listings[source[0]] = []
                parts = listings[source[0]]
                current = next((part for part in parts if part.get("member") == entry["member"]), None)
                if current is None or any(abs(float(a) - float(b)) > 0.001 for a, b in zip(current.get("dims", []), entry["raw_dims"])):
                    raise ValueError(_t("err.basket_gone", name=entry["name"]))
                raw = self._read_raw_blobs(source, parts, [entry["member"]])[entry["member"]]
                by_entry[entry["id"]] = _turned_stl(raw, entry["turn"]) if entry["turn"] else raw
            blobs[member] = by_entry[entry["id"]]
        return blobs

    def _workset(self, config, from_basket):
        """The parts to lay out: the open source's, or the basket's (one part per copy)."""
        if from_basket:
            entries = self._basket_entries()
            if not entries:
                raise ValueError(_t("err.basket_empty"))
            parts = []
            for entry in entries:
                for copy in range(1, entry["copies"] + 1):
                    parts.append({"name": entry["name"] if entry["copies"] == 1 else f'{entry["name"]} ({copy})',
                                  "member": f'basket:{entry["id"]}:{copy}', "filetype": entry["filetype"],
                                  "dims": list(entry["dims"]), "facets": entry["facets"], "included": True})
            return {"key": ("basket", self._basket_version), "parts": parts, "read": self._read_basket_blobs,
                    "name": self._basket_name(), "basket": True}
        source = getattr(self, "_selected_source", None)
        if not source:
            raise ValueError(_t("err.select_source"))
        parts = self._current_parts(config)
        return {"key": source, "parts": parts, "read": lambda members: self._read_stl_blobs(source, parts, members),
                "name": getattr(self, "_selected_name", "") or os.path.basename(source[0]), "basket": False}

    def _plate_view(self, config, index, part=0):
        """One part of a planned plate for the 3D view, simplified, turned and placed as the
        project will have it, in bed millimetres. The page asks part by part and draws the plate
        as it fills, so Orca stays responsive while big parts are simplified."""
        plan = getattr(self, "_last_plan", None)
        if not plan:
            raise ValueError(_t("err.plan_source"))
        ws = self._workset(config, plan.get("basket"))
        if plan.get("source") != ws["key"] or index < 0 or index >= len(plan["plates"]):
            raise ValueError(_t("err.plan_source"))
        plate = plan["plates"][index]
        items = plate["items"]
        if part < 0 or part >= len(items):
            raise ValueError(_t("err.part_missing"))
        item = items[part]
        raw = ws["read"]([item["member"]])[item["member"]]
        if getattr(self, "_simplified", None) is None:
            self._simplified = {}
        counts = {p.get("member"): int(p.get("facets") or 0) for p in ws["parts"]}
        share = _plate_shares([counts.get(it["member"], 0) for it in items])[part]
        vertices, facets = _placed_mesh(item, raw, share, self._simplified)
        size = plan["profile"]["size"]
        self.post_message({"type": "plate_part", "plate": index, "part": part, "count": len(items),
                           "name": _t("plan.plate", n=index + 1), "facets": facets,
                           "top": max([float(it.get("z", 0)) for it in items] + [1.0]),
                           "bed": [float(size.get("x", 0)), float(size.get("y", 0))],
                           "vertices": base64.b64encode(vertices).decode("ascii")})

    def _orient_check(self, index):
        """Check one part of the open source and say which comes next: the page asks part by part,
        so Orca stays responsive while a big source is checked."""
        parts = getattr(self, "_open_parts", [])
        source = getattr(self, "_selected_source", None)
        usable = [i for i, part in enumerate(parts) if part.get("filetype") in ("stl", "3mf") and part.get("member") and not part.get("blocked")]
        later = [i for i in usable if i >= index]
        if not later or not source:
            self.post_message({"type": "orient_advice", "index": -1, "next": -1, "done": len(usable), "total": len(usable),
                               "advised": len(self._advice)})
            return
        i = later[0]
        member = parts[i]["member"]
        try:
            raw = self._read_stl_blobs(source, parts, [member], turned=False)[member]
            advice = _orientation_advice(raw)
        except Exception:
            advice = None
        if advice:
            self._advice[member] = advice
        following = [j for j in usable if j > i]
        self.post_message({"type": "orient_advice", "index": i, "advice": advice, "next": following[0] if following else -1,
                           "done": usable.index(i) + 1, "total": len(usable), "advised": len(self._advice)})

    def _turn_parts(self, indexes, on):
        """Turn the parts the check found lying worse (or put them back); their new sizes go to the page."""
        parts = getattr(self, "_open_parts", [])
        source = getattr(self, "_selected_source", None)
        changed = []
        for i in indexes:
            if i < 0 or i >= len(parts) or parts[i].get("member") not in self._advice:
                continue
            member = parts[i]["member"]
            if on:
                self._turns[member] = _rotation_to_down(self._advice[member]["down"])
                self._read_stl_blobs(source, parts, [member])    # turns it now, so the size is known
                changed.append({"index": i, "turned": True, "dims": list(self._turned[member][1])})
            else:
                self._turns.pop(member, None)
                changed.append({"index": i, "turned": False, "dims": parts[i]["dims"]})
        self._last_plan = None
        self.post_message({"type": "parts_turned", "parts": changed})

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

    def _start_native_arrange(self, config, included, from_basket=False):
        if getattr(self, "_native_arrange_job", None):
            raise ValueError(_t("err.arrange_running"))
        ws = self._workset(config, from_basket)
        included = set(str(value) for value in included)
        all_parts = ws["parts"]
        parts = [part for part in all_parts if part.get("filetype") in ("stl", "3mf") and (ws["basket"] or part.get("member") in included)]
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
            blobs = ws["read"]([part["member"] for part in parts])
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
            if config.get("orca_orient"):
                command += ["--orient", "1"]   # Orca's own Auto orient, before it arranges
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
                "names": names, "source_name": ws["name"],
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
            final = self._project_path(job.get("source_name", ""))
            os.replace(job["output"], final)
            job["output"] = final
            self._remember_project(final)
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
                if self._basket_entries():
                    self._basket_post()   # the page was reloaded: the basket is still here
            elif kind == "save_settings":
                incoming = data.get("config", {})
                for key in ("check_on_open", "recursive", "gpu_preview", "orca_orient"):
                    config[key] = bool(incoming.get(key, config.get(key)))
                config["margin_mm"] = max(0.0, min(20.0, float(incoming.get("margin_mm", config.get("margin_mm", DEFAULT_MARGIN_MM)))))
                config["arrange_mode"] = "orca" if incoming.get("arrange_mode") == "orca" else "own"
                language = incoming.get("language", config.get("language", "auto"))
                config["language"] = language if language in ("auto",) + LANGUAGES else "auto"
                theme = incoming.get("theme", config.get("theme", "orca"))
                config["theme"] = theme if theme in THEMES else "orca"
                incoming_types = incoming.get("model_types", config.get("model_types", ["stl", "step", "3mf"]))
                if not isinstance(incoming_types, (list, tuple, set)):
                    incoming_types = ["stl", "step", "3mf"]
                config["model_types"] = [t for t in ("stl", "step", "3mf") if t in incoming_types]
                self._save(config)
                if getattr(self, "_rows", None) is not None:
                    self._post_rows(config)
            elif kind == "open_support":
                try:
                    opened = webbrowser.open_new_tab(SUPPORT_URL)
                except Exception:
                    opened = False
                if not opened:
                    self.post_message({"type": "error", "message": _t("err.support_open", url=SUPPORT_URL)})
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
                self._open_source(config, str(data.get("path", "")), data.get("kind") if data.get("kind") in ("zip", "file") else "folder")
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
            elif kind == "orient_check":
                self._orient_check(int(data.get("index", 0)))
            elif kind == "turn_part":
                self._turn_parts([int(data.get("index", -1))], bool(data.get("on", True)))
            elif kind == "turn_all":
                parts = getattr(self, "_open_parts", [])
                self._turn_parts([i for i, part in enumerate(parts) if part.get("member") in self._advice
                                  and part.get("member") not in self._turns], True)
            elif kind == "open_in_orca":
                self._open_step_in_orca(config, int(data.get("index", -1)))
            elif kind == "preview_part":
                if str(data.get("member", "")).startswith("basket:"):
                    ws = self._workset(config, True)
                    member = str(data["member"])
                    found = [part for part in ws["parts"] if part["member"] == member]
                    if not found:
                        raise ValueError(_t("err.part_missing"))
                    parts, index = found, 0
                    raw = ws["read"]([member])[member]
                else:
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
                ws = self._workset(config, bool(data.get("basket")))
                all_parts = ws["parts"]
                if ws["basket"]:
                    selected = list(all_parts)
                else:
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
                    blobs = ws["read"]([p["member"] for p in too_wide])
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
                facet_counts = {p.get("member"): int(p.get("facets") or 0) for p in all_parts}
                for plate in result["plates"]:
                    plate["facets"] = sum(facet_counts.get(item.get("member"), 0) for item in plate["items"])
                self.post_message({"type": "plan", "plates": result["plates"], "bed": profile["size"], "profile": profile,
                                   "total": result["total"], "problems": result["problems"], "too_tall": too_tall,
                                   "exclusions": profile.get("exclusions"), "exportable": exportable, "smart": result.get("smart", False),
                                   "min_plates": result.get("min_plates"), "count_note": result.get("count_note"),
                                   "view_budget": MAX_PLATE_VIEW_FACETS})
                self._last_plan = {"source": ws["key"], "plates": result["plates"], "profile": profile, "exportable": exportable,
                                   "basket": ws["basket"]}
            elif kind == "export_project":
                plan = getattr(self, "_last_plan", None)
                try:
                    ws = self._workset(config, plan.get("basket")) if plan else None
                except ValueError:
                    ws = None
                if not plan or not ws or plan.get("source") != ws["key"]:
                    raise ValueError(_t("err.plan_source"))
                if not plan.get("exportable"):
                    raise ValueError(_t("err.plan_not_exportable"))
                active_profile = _get_profile()
                planned_profile = plan["profile"]
                if (not active_profile or active_profile.get("name") != planned_profile.get("name") or
                    any(abs(float(active_profile.get("size", {}).get(axis, 0)) - float(planned_profile.get("size", {}).get(axis, 0))) > 0.001
                        for axis in ("x", "y", "origin_x", "origin_y"))):
                    raise ValueError(_t("err.printer_changed"))
                parts = ws["parts"]
                by_member = {part.get("member"): part for part in parts if part.get("member")}
                for plate in plan["plates"]:
                    for item in plate["items"]:
                        current = by_member.get(item.get("member"))
                        if current is None or any(abs(float(a) - float(b)) > 0.001 for a, b in zip(current.get("dims", []), item.get("source_dims", []))):
                            raise ValueError(_t("err.stl_changed"))
                members = {item.get("member") for plate in plan["plates"] for item in plate["items"]}
                blobs = ws["read"](members)
                name = ws["name"]
                output = self._project_path(name)
                _write_orca_project(output, plan["plates"], blobs, plan["profile"], _app_version(), _project_settings(),
                                    title=_project_base(name))
                self._remember_project(output)
                try:
                    _open_project(output)
                    self.post_message({"type": "project_ready", "path": output, "opened": True})
                except Exception as exc:
                    self.post_message({"type": "project_ready", "path": output, "opened": False, "error": str(exc)[:180]})
            elif kind == "native_arrange":
                self._start_native_arrange(config, data.get("included", []), bool(data.get("basket")))
            elif kind == "plate_view":
                self._plate_view(config, int(data.get("plate", -1)), int(data.get("part", 0)))
            elif kind == "basket_add":
                self._basket_add(config, data.get("members", []))
            elif kind == "basket_set":
                self._basket_set(int(data.get("id", -1)), copies=data.get("copies", 1))
            elif kind == "basket_remove":
                self._basket_set(int(data.get("id", -1)), remove=True)
            elif kind == "basket_clear":
                self._basket_entries().clear()
                self._basket_changed()
            elif kind == "basket_list":
                self._basket_post()
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
