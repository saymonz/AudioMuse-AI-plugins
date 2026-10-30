# AudioMuse-AI Plugins Catalog

Custom plugin repository for [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI).

> [!WARNING]
> **Experimental Core Branch Required**  
> The `playlist_naming` plugin relies on the `register_clustering_provider('playlist_naming', ...)` plugin seam. This core extension is currently implemented on the experimental branch [`feature/plugin-clustering-provider`](https://github.com/saymonz/AudioMuse-AI/tree/feature/plugin-clustering-provider) and deployed via custom overlay images [`ghcr.io/saymonz/audiomuse-ai`](https://github.com/saymonz/AudioMuse-AI/pkgs/container/audiomuse-ai).
>
> It will **not function on stock upstream AudioMuse-AI releases** until the underlying core hook is merged into upstream.

## Installation

1. Open AudioMuse-AI as an administrator and go to **Plugins > Repositories**.
2. Add the catalog URL:
   ```text
   https://raw.githubusercontent.com/saymonz/AudioMuse-AI-plugins/main/manifest.json
   ```
3. Switch to the **Catalog** tab, install **Playlist Naming**, and click **Apply now (restart)**.
4. Configure your prefix, suffix, or bare-name preferences from **Plugins > Installed > Playlist Naming > Settings**.

## Available Plugins

### `playlist_naming` — Playlist Naming

Customizes how AudioMuse-AI names and cleans up clustering playlists without modifying core configuration:

- **Prefix & Virtual Folders**: Organize generated playlists under a folder or prefix (for example `Audiomuse/` for Navidrome/Subsonic clients such as Feishin, or `[AM] `).
- **Custom or Empty Suffix**: Replace `_automatic` with your own suffix (such as `_mix`) or leave it empty (`""`) for clean titles.
- **Bare Names (`prefix=""`, `suffix=""`)**: Safely supported via per-server playlist ID tracking (`plugin_playlist_naming__playlists`), ensuring personal playlists are never touched during cleanup.
- **AI Naming History Preservation**: Strips configured affixes before feeding historical playlist names back into AI naming prompts so duplicate avoidance keeps working cleanly.
- **Standalone & Container Ready**: Zero external pip dependencies (`requirements: []`).

## Building Releases Locally

```bash
python3 scripts/build_catalog.py
```
