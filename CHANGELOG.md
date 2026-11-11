# Changelog

## 0.8.0

- **Parts lying the wrong way are pointed out.** When a source opens, PlatePilot checks how each STL and 3MF part lies: for the sides it could lie on (the six axis sides and its largest flat faces) it compares the area that would hang over air steeper than 45° and the area that would touch the bed. A part that lies clearly worse than it could, upside down or on a narrow edge, gets a note in the list ("lies better turned: 85% less overhang") and a **Turn** button. **Turn all suggested** turns them in one go, **Undo turn** puts a part back. Nothing is turned by itself, and a part is flagged only when the gain is large, so a part its designer laid out on purpose is left alone. A turned part is turned everywhere: in the preview, in both PlatePilot layouts (with its new footprint), in the project and in Orca auto-arrange. The check runs part by part and does not hold up the page; big meshes are sampled.
- **Orca auto-arrange: "Turn parts (Orca auto-orient)".** A checkbox next to **Arrange in Orca**, off by default. With it Orca turns every part the way its Auto orient button would before it arranges them (`--orient 1`).
- **Fixed:** 3MF parts were left out of Orca auto-arrange.

## 0.7.1

- **Projects are named after their source:** "Dragon set - PlatePilot.3mf" instead of "PlatePilot-20261103-153137.3mf", in both arrange modes, so Orca's title bar and recent projects show what was laid out. The same source keeps one file: a later layout of it replaces the file PlatePilot wrote last, so Orca's recent list does not fill up with copies. Once the project was changed and saved in Orca, PlatePilot leaves it alone and writes "... (2)". Projects are kept in a projects folder in PlatePilot's data folder.

## 0.7.0

- **3MF files work like STL:** preview, layout and Open in Prepare. Each object of a 3MF becomes a part of its own, placed and turned as the 3MF has it, in millimetres whatever unit the file uses; objects made of components, also from the separate model files of Bambu and Orca projects, are put together. A 3MF is read once when its source is opened (about 2 s for 500 000 facets); scanning folders and starting the plugin are not slower.
- **STEP parts get an Open in Orca button.** PlatePilot does not convert STEP itself (that would take a heavy engine): it hands the file to the Orca window, which opens it in Prepare and asks how finely to convert it. A STEP inside an archive is taken out into PlatePilot's folder first. Windows only, like the project hand-over.

## 0.6.3

- **Folders and files with Cyrillic or other non-Latin names on OrcaSlicer 2.5.0 and later.** Orca's plugin permission check used to break on such names (the plugin call hung and Orca could crash), so PlatePilot skipped them. Our fix for that, OrcaSlicer#16925, is in the 2.5.0 release, so there they are listed, opened and watched like any other. Older 2.5 builds (2.5.0-dev, betas) keep the old protection. Folders named config, cert or secret are still refused: Orca refuses them on purpose.

## 0.6.2

- **Folder switch in the library.** With two or more watched folders (or one plus the files added to PlatePilot), a list under the search shows the sources of one folder only, with how many each holds. It works together with All, New, ZIP, Folders and Added, and the search.

## 0.6.1

- The library lists the newest sources first. A file pasted or copied in Explorer keeps its original date, so freshness is the later of the modified and created times: a copy made today comes first.
- Files dropped or pasted into PlatePilot show under New until you open them yourself, as files that appear in a watched folder do. Before, they were marked seen right away and listed by name, often at the bottom.
- **Plates** field next to Gap, empty (auto) by default: as few plates as the parts fit on, as before. A higher number spreads the parts evenly over that many plates, largest first, in Calculate and Smart layout alike, even when they would fit on one. A number below what the parts need, or above one plate per part, is not used, and the plan says why. The field is not saved: every session starts on auto.
- Bigger plate drawings after Calculate and Smart layout, and a close-up of any plate on click: the whole plate at window size with every part numbered, a list of the parts with their sizes and turns beside it, arrows or the keyboard to go through the plates, Esc to close.

## 0.6.0

- **Smart layout (beta)**, a new button next to Calculate. It uses as many plates as the PlatePilot layout, but groups the parts of each plate in the middle of the bed instead of the front left corner: away from the edges (at least 10 mm where there is room), with wider even gaps, the plates filled about evenly. Small parts and parts tall for their footprint, which hold on worst, get the places nearest the centre. A plate too full for that keeps the dense layout, moved to the middle. Calculate, Orca auto-arrange and Open in Prepare work as before.
- The 3D preview is drawn with the graphics card (WebGL). Parts of up to 250 000 facets are shown in full, without the simplification marks of 0.5.1, and turn smoothly: a 210 000-facet part takes about 14 ms per frame instead of 65–380 ms for its 22 000-facet simplified copy.
- A depth buffer replaces sorting the facets, so back faces no longer show through.
- The previous 2D preview stays: it is used when WebGL is not available or fails, and can be chosen in Folders > 3D preview. In that mode the plugin sends a simplified copy of up to 30 000 facets, as before.
- Previews of parts within the limit are prepared about three times faster.

## 0.5.2

- New icon, "Golden PEI": light parts laid out on a textured gold bed. The Orca tab uses a simplified version that stays clear at 20 px; the page header shows the full one.

## 0.5.1

- The preview shows the whole part. Parts with more than 30 000 facets used to show only the first 30 000 facets of the file, which STL exporters write in no particular order, so many models appeared in scattered pieces. Such parts are now simplified evenly for the preview only; the project sent to Orca always has every facet.
- Larger models: an STL can be up to 100 MB (2 million facets, the most the STL reader takes) instead of 30 MB, an archive or folder up to 500 MB of STL instead of 200 MB, and a project up to 5 million facets instead of 600 000.
- Projects are written about three times faster (a 1.27-million-facet bundle: 3 s instead of 9 s; 5 million facets: about 13 s), in batches, with the same meshes as before.
- Part sizes are read in place without copying the file, about three times faster, and a preview of a 2-million-facet part needs almost no memory beyond the file itself.
- The last previews are kept, so clicking a part again shows it at once.

## 0.5.0

- Far fewer permission prompts. Checking folders no longer opens every ZIP: it only lists names and sizes in one pass, so Orca asks once per watched folder instead of once per archive and subfolder. An archive is read when you open it; a model folder is copied into PlatePilot's data folder in one step, so Orca asks once per folder instead of once per STL.
- No more "Continue scanning?" questions and no plugin consent dialog: choosing or pasting a folder is enough.
- Drag and drop: drop ZIP, STL, STEP or 3MF files or a whole folder onto PlatePilot, or paste files or paths with Ctrl+V. Dropped files are copied into PlatePilot's own folder (no permission prompts), listed under **Added** and opened right away. **Delete** removes the copy.
- A pasted folder path is added as a watched folder.
- Each watched folder shows a summary: sources, new ones, archives without models, and when it was checked.
- **New** filter and badge for sources that appeared since the last check; **Mark all as seen**.
- Folders are checked when PlatePilot opens and when you return to the tab, instead of on a timer. The interval and "ask after N archives" settings are gone.
- Archives that turned out to have no models are hidden from the library.
- Archives that were never opened are judged by their names. Model downloads (Printables `..._stls.zip`, Thingiverse `Name - 1234567.zip`, `model_files`) are listed up front. Programs, drivers and archives with unclear names wait in a collapsed group at the bottom, and search still finds them. Opening one checks it: with models it moves up, without them it disappears.
- Works around an OrcaSlicer 2.5 bug that hung the plugin or crashed Orca on folders with non-Latin names: Orca's permission check lowercases each path byte in the Windows code page, which breaks UTF-8, and then throws inside its audit hook. PlatePilot no longer touches such paths. It skips those subfolders (the folder summary shows how many) and explains why such archives can't be opened. Files dropped into PlatePilot get Latin names on disk, and the library still shows the original names. Folders named `config`, `cert` or `secret`, which Orca always refuses, are skipped as well.
- Parts longer than the bed side now fit diagonally. PlatePilot layout finds the angle at which such a part fits from its outline seen from above and writes that rotation into the project. Orca auto-arrange is started with rotation allowed when a part needs it. If Orca still leaves parts beside the plates, PlatePilot says so instead of reporting success.
- **Add files** button: pick ZIP, STL, STEP or 3MF files in the system dialog. They are copied into PlatePilot like dropped files, so any file name works and Orca does not ask for permission.
- Archives, model folders and added files have their own icons and colours in the library.
- The page no longer waits forever when the plugin does not answer, and after Orca closes during a folder check, the next start does not repeat it automatically.

## 0.4.1

- Orca auto-arrange no longer fails with "No such file or directory" when the plugin data folder has a long path: the temporary STL files now have short names, so their paths stay under the Windows 260-character limit.
- Parts arranged by Orca keep their own names in the project instead of the temporary file names (for example "001-part_name.stl").

## 0.4.0 — first public release

- English and Russian UI. The language follows OrcaSlicer, or you can pick one in the settings.
- Projects open with all their plates: the active printer, process and filament settings are written into the 3MF, so Orca no longer falls back to loading geometry only.
- The project opens in the current Orca window on Windows, without starting a second process.
- Orca auto-arrange uses the active printer and process profiles instead of the 200 × 200 mm CLI default.
- Every part is placed inside its own plate of Orca's plate grid, and STL vertices are merged.
- New folder picker on Windows. Pasted paths with quotes and environment variables work, duplicate and nested folders are detected.
- macOS and Linux: paste folder paths. The project is saved for File > Open Project.
- No default watched folder.
- Redesigned interface that follows Orca's light and dark theme.
