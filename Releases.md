# Wingflight Lua EdgeTX/OpenTX Updater Release Notes

## 1.0.4
- Version bump for release alignment; no updater-relevant changes this cycle.

## 1.0.3
- Version bump for release alignment; no updater-relevant changes this cycle.

## 1.0.2
- Rebranded the updater for Wingflight, including the application title, release metadata, bundled logo, Windows icon, README image, and packaged artifact names.
- Updated downloads to use `WingFlight/wingflight-lua-edgetx` release, snapshot, branch, pull request, and master sources.
- Updated install handling for Wingflight's EdgeTX/OpenTX layout, including `SCRIPTS/WF`, `SCRIPTS/TOOLS/wf.lua`, and Wingflight widgets.
- Preserved `SCRIPTS/WF/settings.lua` during stale-file cleanup.
- Removed the remote logo refresh so the bundled Wingflight logo is not replaced by stale published assets.

## 1.0.1
- Development channel can now install directly from any repository branch, not just master
- Development channel now lists open pull requests (including forks) as installable options
- Version list no longer fails to load entirely if a single GitHub API call (releases, branches, or pull requests) times out

## 1.0.0
- Initial release of the standalone Wingflight Lua EdgeTX/OpenTX updater, with packaged updater builds for installing release, snapshot, and development Lua versions on EdgeTX/OpenTX radios.
