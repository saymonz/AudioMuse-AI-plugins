# AudioMuse-AI Playlist Naming Plugin
# SPDX-License-Identifier: AGPL-3.0-only

"""Customizable clustering playlist naming and cleanup plugin for AudioMuse-AI.

Uses only the public ``plugin.api`` surface and ``register_clustering_provider``:
* Supports prefixes and virtual folders (for example ``Audiomuse/``), custom
  suffixes (for example ``_mix``), or completely bare names (empty prefix and
  empty suffix).
* Tracks every created playlist ID per music server in ``table('playlists')``
  so bare names and prefix/suffix changes clean up prior runs safely without
  ever touching personal playlists.
* Strips configured affixes via ``unformat_name`` so AI playlist-naming history
  continues avoiding previously generated titles.
"""

import html

from flask import Blueprint, redirect, request, url_for

from plugin.api import (
    active_server_id,
    get_db,
    get_setting,
    logger,
    manage_plugins_url,
    render_page,
    set_setting,
    table,
)

bp = Blueprint('playlist_naming', __name__)

_DEFAULT_SERVER_KEY = '__default__'
_MAX_AFFIX_LEN = 80


def _sanitize_affix(value):
    if not isinstance(value, str):
        return ''
    cleaned = value.replace('\r', '').replace('\n', '').replace('\x00', '')
    return cleaned[:_MAX_AFFIX_LEN]


def _playlist_id(playlist):
    if isinstance(playlist, str):
        return playlist.strip()
    if isinstance(playlist, dict):
        raw = playlist.get('Id') or playlist.get('id')
        return str(raw).strip() if raw is not None else ''
    return ''


def _playlist_name(playlist):
    if isinstance(playlist, dict):
        raw = playlist.get('Name') or playlist.get('name')
        return str(raw) if raw is not None else ''
    return ''


def _ensure_playlists_table(db):
    tbl = table('playlists')
    cur = db.cursor()
    try:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {tbl} ("
            "server_id TEXT NOT NULL, "
            "playlist_id TEXT NOT NULL, "
            "playlist_name TEXT NOT NULL, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (server_id, playlist_id)"
            ")"
        )
        db.commit()
    finally:
        cur.close()


def migrate(db):
    _ensure_playlists_table(db)


def _on_worker_boot():
    try:
        _ensure_playlists_table(get_db())
    except Exception:
        logger.exception('playlist_naming: could not ensure playlists table on worker start')


def _load_tracked_ids(server_key):
    tbl = table('playlists')
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SAVEPOINT pn_read_tracked")
        try:
            cur.execute(
                f"SELECT playlist_id FROM {tbl} WHERE server_id = %s",
                (server_key,),
            )
            ids = {str(row[0]) for row in cur.fetchall() if row and row[0]}
            cur.execute("RELEASE SAVEPOINT pn_read_tracked")
            return ids
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT pn_read_tracked")
            logger.exception('playlist_naming: could not read tracked playlist ids')
            return set()
    finally:
        cur.close()


class _PlaylistNamer:
    def __init__(
        self,
        server_key,
        prefix,
        suffix,
        clean_matching_affixes,
        clean_legacy_automatic,
        tracked_ids,
    ):
        self.server_key = server_key
        self.prefix = prefix
        self.suffix = suffix
        self.clean_matching_affixes = bool(clean_matching_affixes)
        self.clean_legacy_automatic = bool(clean_legacy_automatic)
        self.tracked_ids = set(tracked_ids)

    def format_name(self, base_name, chunk_index=None):
        stem = f"{base_name} ({chunk_index})" if chunk_index is not None else str(base_name)
        formatted = f"{self.prefix}{stem}{self.suffix}"
        return formatted if formatted.strip() else stem

    def unformat_name(self, playlist_name):
        if not isinstance(playlist_name, str) or not playlist_name:
            return None
        if not self.prefix and not self.suffix:
            return None if playlist_name.endswith('_automatic') else playlist_name
        min_len = len(self.prefix) + len(self.suffix)
        if len(playlist_name) <= min_len:
            return None
        if self.prefix and not playlist_name.startswith(self.prefix):
            return None
        if self.suffix and not playlist_name.endswith(self.suffix):
            return None
        end = len(playlist_name) - len(self.suffix) if self.suffix else len(playlist_name)
        return playlist_name[len(self.prefix):end]

    def should_delete(self, playlist):
        pid = _playlist_id(playlist)
        if pid and pid in self.tracked_ids:
            return True
        name = _playlist_name(playlist)
        if not name:
            return False
        if self.clean_legacy_automatic and name.endswith('_automatic'):
            return True
        if self.clean_matching_affixes and (self.prefix or self.suffix):
            min_len = len(self.prefix) + len(self.suffix)
            if (
                len(name) > min_len
                and name.startswith(self.prefix)
                and name.endswith(self.suffix)
            ):
                return True
        return False

    def on_created(self, playlist, playlist_name):
        pid = _playlist_id(playlist)
        if not pid:
            return
        self.tracked_ids.add(pid)
        tbl = table('playlists')
        db = get_db()
        cur = db.cursor()
        try:
            cur.execute("SAVEPOINT pn_write_created")
            try:
                cur.execute(
                    f"INSERT INTO {tbl} (server_id, playlist_id, playlist_name) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (server_id, playlist_id) DO UPDATE SET "
                    "playlist_name = EXCLUDED.playlist_name, "
                    "created_at = CURRENT_TIMESTAMP",
                    (self.server_key, pid, str(playlist_name)),
                )
                cur.execute("RELEASE SAVEPOINT pn_write_created")
                db.commit()
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT pn_write_created")
                logger.exception(
                    'playlist_naming: failed to record created playlist %s', pid
                )
        finally:
            cur.close()

    def after_cleanup(self, deleted_ids, playlists):
        deleted_set = {str(i) for i in (deleted_ids or []) if i}
        present_ids = {_playlist_id(p) for p in (playlists or [])}
        present_ids.discard('')
        surviving = sorted((present_ids - deleted_set) & self.tracked_ids)
        self.tracked_ids = set(surviving)
        tbl = table('playlists')
        db = get_db()
        cur = db.cursor()
        try:
            cur.execute("SAVEPOINT pn_prune_tracked")
            try:
                if surviving:
                    cur.execute(
                        f"DELETE FROM {tbl} "
                        "WHERE server_id = %s AND NOT (playlist_id = ANY(%s))",
                        (self.server_key, surviving),
                    )
                else:
                    cur.execute(
                        f"DELETE FROM {tbl} WHERE server_id = %s",
                        (self.server_key,),
                    )
                cur.execute("RELEASE SAVEPOINT pn_prune_tracked")
                db.commit()
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT pn_prune_tracked")
                logger.exception('playlist_naming: failed to prune tracked playlist rows')
        finally:
            cur.close()


def _build_namer():
    server_key = active_server_id() or _DEFAULT_SERVER_KEY
    prefix = _sanitize_affix(get_setting('prefix', ''))
    suffix = _sanitize_affix(get_setting('suffix', '_automatic'))
    clean_matching = bool(get_setting('clean_matching_affixes', True))
    clean_legacy = bool(get_setting('clean_legacy_automatic', False))
    tracked_ids = _load_tracked_ids(server_key)
    return _PlaylistNamer(
        server_key=server_key,
        prefix=prefix,
        suffix=suffix,
        clean_matching_affixes=clean_matching,
        clean_legacy_automatic=clean_legacy,
        tracked_ids=tracked_ids,
    )


def _render_settings_html(prefix, suffix, clean_matching, clean_legacy, saved=False):
    esc_prefix = html.escape(prefix, quote=True)
    esc_suffix = html.escape(suffix, quote=True)
    checked_matching = 'checked' if clean_matching else ''
    checked_legacy = 'checked' if clean_legacy else ''
    back_url = html.escape(manage_plugins_url(), quote=True)
    action_url = html.escape(url_for('playlist_naming.settings'), quote=True)
    banner = (
        '<div style="padding:0.75rem 1rem;margin-bottom:1.25rem;border-radius:6px;'
        'background:rgba(46,160,67,0.18);border:1px solid rgba(46,160,67,0.5);">'
        'Settings saved. The next clustering run will use this naming format immediately '
        '(no container restart required).'
        '</div>'
        if saved
        else ''
    )
    sample_single = html.escape(f"{prefix}Midnight Jazz{suffix}" or "Midnight Jazz")
    sample_chunk = html.escape(f"{prefix}Midnight Jazz (1){suffix}" or "Midnight Jazz (1)")
    return f"""
    <div style="max-width:680px;">
        {banner}
        <form method="post" action="{action_url}" style="display:flex;flex-direction:column;gap:1.1rem;">
            <div>
                <label for="pn-prefix" style="display:block;font-weight:600;margin-bottom:0.35rem;">
                    Playlist Prefix
                </label>
                <input id="pn-prefix" name="prefix" type="text" maxlength="{_MAX_AFFIX_LEN}"
                       value="{esc_prefix}" placeholder="e.g. Audiomuse/ or [AM] "
                       style="width:100%;padding:0.55rem 0.7rem;border-radius:6px;border:1px solid #444;background:rgba(0,0,0,0.2);color:inherit;">
                <small style="opacity:0.8;display:block;margin-top:0.3rem;">
                    Use a trailing slash such as <code>Audiomuse/</code> for virtual folders in Subsonic/Navidrome clients (e.g. Feishin), or leave empty.
                </small>
            </div>

            <div>
                <label for="pn-suffix" style="display:block;font-weight:600;margin-bottom:0.35rem;">
                    Playlist Suffix
                </label>
                <input id="pn-suffix" name="suffix" type="text" maxlength="{_MAX_AFFIX_LEN}"
                       value="{esc_suffix}" placeholder="Leave empty for no suffix (default: _automatic)"
                       style="width:100%;padding:0.55rem 0.7rem;border-radius:6px;border:1px solid #444;background:rgba(0,0,0,0.2);color:inherit;">
                <small style="opacity:0.8;display:block;margin-top:0.3rem;">
                    Clear this field to remove <code>_automatic</code>. Leaving both prefix and suffix empty is safe: created playlist IDs are tracked per server in the database.
                </small>
            </div>

            <div style="padding:0.85rem 1rem;border-radius:6px;border:1px solid rgba(128,128,128,0.3);background:rgba(128,128,128,0.08);">
                <div style="font-weight:600;margin-bottom:0.35rem;">Live Preview</div>
                <div>Standard playlist: <code id="pn-preview-single">{sample_single}</code></div>
                <div style="margin-top:0.25rem;">Split playlist: <code id="pn-preview-chunk">{sample_chunk}</code></div>
            </div>

            <label style="display:flex;align-items:flex-start;gap:0.6rem;cursor:pointer;">
                <input type="checkbox" name="clean_matching_affixes" value="1" {checked_matching} style="margin-top:0.25rem;">
                <span>
                    <strong>Also clean up playlists matching prefix/suffix</strong><br>
                    <small style="opacity:0.8;">When at least one of prefix or suffix is non-empty, also remove existing playlists matching that pattern. Automatically bypassed when both prefix and suffix are empty.</small>
                </span>
            </label>

            <label style="display:flex;align-items:flex-start;gap:0.6rem;cursor:pointer;">
                <input type="checkbox" name="clean_legacy_automatic" value="1" {checked_legacy} style="margin-top:0.25rem;">
                <span>
                    <strong>Also clean up legacy <code>_automatic</code> playlists</strong><br>
                    <small style="opacity:0.8;">Enable when migrating away from the built-in <code>_automatic</code> suffix so previous automatic playlists are removed on the next clustering run.</small>
                </span>
            </label>

            <div style="display:flex;gap:0.75rem;align-items:center;margin-top:0.5rem;">
                <button type="submit" class="btn btn-primary" style="padding:0.55rem 1.2rem;cursor:pointer;">
                    Save Settings
                </button>
                <a href="{back_url}" style="opacity:0.85;">Back to Manage Plugins</a>
            </div>
        </form>
    </div>
    <script>
    (function() {{
        const p = document.getElementById('pn-prefix');
        const s = document.getElementById('pn-suffix');
        const outSingle = document.getElementById('pn-preview-single');
        const outChunk = document.getElementById('pn-preview-chunk');
        function update() {{
            const pv = p ? p.value : '';
            const sv = s ? s.value : '';
            const single = (pv + 'Midnight Jazz' + sv).trim() ? (pv + 'Midnight Jazz' + sv) : 'Midnight Jazz';
            const chunk = (pv + 'Midnight Jazz (1)' + sv).trim() ? (pv + 'Midnight Jazz (1)' + sv) : 'Midnight Jazz (1)';
            if (outSingle) outSingle.textContent = single;
            if (outChunk) outChunk.textContent = chunk;
        }}
        if (p) p.addEventListener('input', update);
        if (s) s.addEventListener('input', update);
    }})();
    </script>
    """


@bp.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        prefix = _sanitize_affix(request.form.get('prefix', ''))
        suffix = _sanitize_affix(request.form.get('suffix', ''))
        clean_matching = request.form.get('clean_matching_affixes') == '1'
        clean_legacy = request.form.get('clean_legacy_automatic') == '1'
        set_setting('prefix', prefix)
        set_setting('suffix', suffix)
        set_setting('clean_matching_affixes', clean_matching)
        set_setting('clean_legacy_automatic', clean_legacy)
        return redirect(url_for('playlist_naming.settings', saved='1'))

    prefix = _sanitize_affix(get_setting('prefix', ''))
    suffix = _sanitize_affix(get_setting('suffix', '_automatic'))
    clean_matching = bool(get_setting('clean_matching_affixes', True))
    clean_legacy = bool(get_setting('clean_legacy_automatic', False))
    saved = request.args.get('saved') == '1'
    body = _render_settings_html(prefix, suffix, clean_matching, clean_legacy, saved=saved)
    return render_page(body, title='Playlist Naming - Settings')


def register(ctx):
    ctx.on_install(migrate)
    ctx.on_worker_start(_on_worker_boot)
    ctx.add_blueprint(bp)
    ctx.register_clustering_provider('playlist_naming', _build_namer, cache=False)
