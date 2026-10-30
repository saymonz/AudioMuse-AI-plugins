#!/usr/bin/env python3
"""Build deterministic plugin release archives and update plugin.json checksums."""

import hashlib
import io
import json
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_ROOT = os.path.join(ROOT, 'plugins')
RELEASES_ROOT = os.path.join(ROOT, 'releases')
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def build_plugin_zip(plugin_dir):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(plugin_dir):
            dirnames[:] = sorted(d for d in dirnames if d != '__pycache__' and not d.startswith('.'))
            for filename in sorted(filenames):
                if filename == 'plugin.json' or filename.endswith(('.pyc', '.pyo')):
                    continue
                full_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(full_path, plugin_dir).replace(os.sep, '/')
                with open(full_path, 'rb') as fh:
                    data = fh.read()
                info = zipfile.ZipInfo(rel_path, date_time=FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, data)
    return buf.getvalue()


def main():
    os.makedirs(RELEASES_ROOT, exist_ok=True)
    manifest_path = os.path.join(ROOT, 'manifest.json')
    with open(manifest_path, 'r', encoding='utf-8') as fh:
        catalog = json.load(fh)

    for entry in catalog.get('plugins', []):
        plugin_id = entry['id']
        plugin_dir = os.path.join(PLUGINS_ROOT, plugin_id)
        pjson_path = os.path.join(plugin_dir, 'plugin.json')
        with open(pjson_path, 'r', encoding='utf-8') as fh:
            doc = json.load(fh)

        latest = doc['versions'][0]
        version = latest['version']
        archive_bytes = build_plugin_zip(plugin_dir)
        checksum = hashlib.md5(archive_bytes, usedforsecurity=False).hexdigest()

        zip_name = f"{plugin_id}-{version}.zip"
        zip_path = os.path.join(RELEASES_ROOT, zip_name)
        with open(zip_path, 'wb') as fh:
            fh.write(archive_bytes)

        latest['checksum'] = checksum
        with open(pjson_path, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(doc, fh, indent=2)
            fh.write('\n')

        print(f"Built {zip_name} ({len(archive_bytes)} bytes, md5={checksum})")


if __name__ == '__main__':
    main()
