"""End-to-end unit tests for the playlist_naming plugin against AudioMuse-AI's plugin API."""

import json
import os
import sys

import pytest
from flask import Flask

PLUGIN_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(PLUGIN_REPO_ROOT, 'scripts')
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import build_catalog
import config
import database
import tasks._playlist_namer as core_namer
from plugin import api as plugin_api
from plugin import manager as plugin_mgr


class _FakeDB:
    """In-memory stand-in for PostgreSQL tracking plugin_playlist_naming__playlists."""

    def __init__(self):
        self.rows = {}
        self.commits = 0

    def commit(self):
        self.commits += 1

    def cursor(self):
        return _FakeCursor(self)


class _FakeCursor:
    def __init__(self, db):
        self._db = db
        self._result = []

    def execute(self, sql, params=None):
        text = ' '.join(sql.split())
        if text.startswith(('SAVEPOINT ', 'RELEASE SAVEPOINT ', 'ROLLBACK TO SAVEPOINT ', 'CREATE TABLE')):
            self._result = []
            return
        if text.startswith('SELECT playlist_id FROM '):
            server_id = params[0]
            self._result = [
                (pid,) for (sid, pid) in sorted(self._db.rows) if sid == server_id
            ]
            return
        if text.startswith('INSERT INTO '):
            server_id, playlist_id, name = params
            self._db.rows[(server_id, playlist_id)] = name
            self._result = []
            return
        if text.startswith('DELETE FROM ') and 'playlist_id = ANY(' in text:
            server_id, gone = params
            for pid in gone:
                self._db.rows.pop((server_id, pid), None)
            self._result = []
            return
        raise AssertionError(f"Unexpected SQL: {text}")

    def fetchall(self):
        return list(self._result)

    def close(self):
        return None


@pytest.fixture
def loaded_plugin(tmp_path, monkeypatch):
    plugins_dir = str(tmp_path / 'plugins_dir')
    os.makedirs(plugins_dir, exist_ok=True)
    monkeypatch.setattr(config, 'PLUGINS_DIR', plugins_dir)
    monkeypatch.setattr(config, 'PLUGINS_ENABLED', True)

    fake_db = _FakeDB()
    settings_store = {}
    monkeypatch.setattr(database, 'get_db', lambda: fake_db)
    monkeypatch.setattr(plugin_api, 'get_db', lambda: fake_db)
    monkeypatch.setattr(
        database, 'get_plugin_settings', lambda pid: dict(settings_store.get(pid, {}))
    )
    monkeypatch.setattr(
        database,
        'set_plugin_settings',
        lambda pid, data: settings_store.__setitem__(pid, dict(data)),
    )

    plugin_src = os.path.join(PLUGIN_REPO_ROOT, 'plugins', 'playlist_naming')
    with open(os.path.join(plugin_src, 'plugin.json'), 'r', encoding='utf-8') as fh:
        manifest = json.load(fh)
    package_bytes = build_catalog.build_plugin_zip(plugin_src)

    mgr = plugin_mgr.PluginManager()
    mgr.setup_namespace()
    mgr._materialize_one('playlist_naming', 'test-md5', package_bytes)
    mgr.records = {
        'playlist_naming': {
            'id': 'playlist_naming',
            'name': manifest['name'],
            'version': manifest['versions'][0]['version'],
            'manifest': manifest,
            'checksum': 'test-md5',
            'requirements': [],
            'enabled': True,
            'settings': {},
            'source_repo': None,
            'load_status': None,
            'error': None,
        }
    }
    monkeypatch.setattr(mgr, '_persist_status', lambda *a, **kw: None)

    app = Flask(__name__)
    app.secret_key = 'x' * 32
    monkeypatch.setattr(plugin_api, 'manage_plugins_url', lambda: '/plugins')
    monkeypatch.setattr(
        plugin_api, 'render_page', lambda body, title=None, active='plugins': f"<h1>{title}</h1>{body}"
    )

    import flask_app
    monkeypatch.setattr(flask_app, 'app', app)

    mgr.load('web', flask_app=app)
    assert mgr.records['playlist_naming']['load_status'] == 'ok'
    assert mgr.get_settings_endpoint('playlist_naming') == 'playlist_naming.settings'

    monkeypatch.setattr(plugin_mgr, 'plugin_manager', mgr)
    yield app, mgr, fake_db, settings_store
    mgr._purge_modules('playlist_naming')


class TestPlaylistNamingPlugin:
    def test_virtual_folder_prefix_and_empty_suffix(self, loaded_plugin):
        app, _mgr, fake_db, settings_store = loaded_plugin
        settings_store['playlist_naming'] = {
            'prefix': 'Audiomuse/',
            'suffix': '',
            'clean_matching_affixes': True,
            'clean_legacy_automatic': False,
        }

        with app.app_context():
            namer = core_namer.get_playlist_namer()
            assert namer.owner == 'playlist_naming'
            assert namer.format_name('Midnight Jazz') == 'Audiomuse/Midnight Jazz'
            assert namer.format_name('Midnight Jazz', chunk_index=2) == 'Audiomuse/Midnight Jazz (2)'
            assert namer.unformat_name('Audiomuse/Midnight Jazz') == 'Midnight Jazz'
            assert namer.unformat_name('Legacy_automatic') == 'Legacy_automatic'

            assert namer.should_delete({'Id': '1', 'Name': 'Audiomuse/Midnight Jazz'}) is True
            assert namer.should_delete({'Id': '2', 'Name': 'Personal Mix'}) is False

            namer.on_created({'Id': 'pl-10', 'Name': 'Audiomuse/Midnight Jazz'}, 'Audiomuse/Midnight Jazz')
            assert ('__default__', 'pl-10') in fake_db.rows

    def test_bare_names_mode_never_deletes_untracked_playlists(self, loaded_plugin):
        app, _mgr, fake_db, settings_store = loaded_plugin
        settings_store['playlist_naming'] = {
            'prefix': '',
            'suffix': '',
            'clean_matching_affixes': True,
            'clean_legacy_automatic': False,
        }

        with app.app_context():
            namer1 = core_namer.get_playlist_namer()
            assert namer1.format_name('Velvet Soul') == 'Velvet Soul'
            assert namer1.format_name('Velvet Soul', chunk_index=1) == 'Velvet Soul (1)'
            namer1.on_created({'Id': 'auto-1', 'Name': 'Velvet Soul'}, 'Velvet Soul')
            namer1.on_created({'Id': 'auto-2', 'Name': 'Synth Nights'}, 'Synth Nights')

            # Next clustering run resolves a fresh namer from DB
            namer2 = core_namer.get_playlist_namer()
            server_playlists = [
                {'Id': 'user-fav', 'Name': 'Favorites'},
                {'Id': 'auto-1', 'Name': 'Velvet Soul'},
                {'Id': 'auto-2', 'Name': 'Synth Nights'},
            ]
            to_delete = [p['Id'] for p in server_playlists if namer2.should_delete(p)]
            assert to_delete == ['auto-1', 'auto-2']

            namer2.after_cleanup(['auto-1'], server_playlists)
            assert set(fake_db.rows.keys()) == {('__default__', 'auto-2')}

    def test_empty_listing_never_forgets_tracked_ids(self, loaded_plugin):
        app, _mgr, fake_db, settings_store = loaded_plugin
        settings_store['playlist_naming'] = {'prefix': '', 'suffix': ''}

        with app.app_context():
            run1 = core_namer.get_playlist_namer()
            run1.on_created({'Id': 'auto-1', 'Name': 'Velvet Soul'}, 'Velvet Soul')
            run1.on_created({'Id': 'auto-2', 'Name': 'Synth Nights'}, 'Synth Nights')

            # Media server unreachable: get_all_playlists() returned [] and nothing was deleted
            run2 = core_namer.get_playlist_namer()
            run2.after_cleanup([], [])
            assert set(fake_db.rows) == {('__default__', 'auto-1'), ('__default__', 'auto-2')}

            # A real listing still forgets playlists the user removed by hand
            run3 = core_namer.get_playlist_namer()
            run3.after_cleanup([], [{'Id': 'auto-2', 'Name': 'Synth Nights'}])
            assert set(fake_db.rows) == {('__default__', 'auto-2')}

    def test_prefix_transition_still_cleans_previously_tracked_ids(self, loaded_plugin):
        app, _mgr, _fake_db, settings_store = loaded_plugin
        settings_store['playlist_naming'] = {'prefix': 'OldFolder/', 'suffix': ''}

        with app.app_context():
            run1 = core_namer.get_playlist_namer()
            run1.on_created({'Id': 'old-id-1', 'Name': 'OldFolder/Rock'}, 'OldFolder/Rock')

            # Admin switches prefix to NewFolder/ without restarting worker
            settings_store['playlist_naming'] = {'prefix': 'NewFolder/', 'suffix': ''}
            run2 = core_namer.get_playlist_namer()
            assert run2.format_name('Rock') == 'NewFolder/Rock'
            assert run2.should_delete({'Id': 'old-id-1', 'Name': 'OldFolder/Rock'}) is True
            assert run2.should_delete({'Id': 'user-id', 'Name': 'My Rock'}) is False

    def test_clean_legacy_automatic_catches_single_and_split_playlists(self, loaded_plugin):
        app, _mgr, _fake_db, settings_store = loaded_plugin
        settings_store['playlist_naming'] = {
            'prefix': 'Audiomuse/',
            'suffix': '',
            'clean_matching_affixes': True,
            'clean_legacy_automatic': True,
        }

        with app.app_context():
            namer = core_namer.get_playlist_namer()
            assert namer.should_delete({'Id': '1', 'Name': 'Chill_automatic'}) is True
            assert namer.should_delete({'Id': '2', 'Name': 'Chill_automatic (1)'}) is True
            assert namer.should_delete({'Id': '3', 'Name': 'Chill_automatic (12)'}) is True
            assert namer.should_delete({'Id': '4', 'Name': 'My_automatic_favorites'}) is False

    def test_settings_get_and_post_roundtrip(self, loaded_plugin):
        app, _mgr, _fake_db, settings_store = loaded_plugin
        client = app.test_client()

        resp = client.get('/plugins/playlist_naming/settings')
        assert resp.status_code == 200
        assert b'Playlist Naming - Settings' in resp.data

        post_resp = client.post(
            '/plugins/playlist_naming/settings',
            data={
                'prefix': 'Audiomuse/',
                'suffix': '',
                'clean_matching_affixes': '1',
                'clean_legacy_automatic': '1',
            },
            follow_redirects=True,
        )
        assert post_resp.status_code == 200
        assert b'Settings saved.' in post_resp.data
        assert settings_store['playlist_naming'] == {
            'prefix': 'Audiomuse/',
            'suffix': '',
            'clean_matching_affixes': True,
            'clean_legacy_automatic': True,
        }
