# Changelog

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
