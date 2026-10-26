# Changelog

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
