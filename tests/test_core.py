from __future__ import annotations

import io
import json
import os
from pathlib import Path
import random
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

from PIL import Image

from splash_patcher.discovery import Installation, make_replacement, scan
from splash_patcher.patcher import add_padding_chunk, exact_size_deflate, patch_jar
from splash_patcher.installer import run_helper
from splash_patcher.transactions import Manager, atomic_copy, clear_cache, digest, execute, read_json, workspace_lock


def png(size=(80, 50), color=None):
    if color:
        im = Image.new('RGB', size, color)
    else:
        im = Image.frombytes('RGB', size, random.Random(42).randbytes(size[0]*size[1]*3))
    out = io.BytesIO()
    im.save(out, format='PNG')
    return out.getvalue()


def create_jar(path, method=zipfile.ZIP_DEFLATED, descriptor=False, signed=False):
    class NonSeekable(io.BytesIO):
        def seekable(self):
            return False
        def seek(self, *args):
            raise io.UnsupportedOperation()
    output = NonSeekable() if descriptor else io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=method) as jar:
        jar.writestr('art/logo.png', png())
        jar.writestr('art/logo@2x.png', png((160, 100)))
        jar.writestr('icons/unusual.png', png((8, 8)))
        jar.writestr('Some.class', b'\xca\xfe\xba\xbe' + bytes(range(256))*10)
        jar.comment = b'JetBrains-like opaque trailer: offsets must not move'
        if signed:
            jar.writestr('META-INF/ART.SF', b'signature')
    path.write_bytes(output.getvalue())


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ide_path = self.root / 'IDE'
        (self.ide_path / 'lib').mkdir(parents=True)
        (self.ide_path / 'product-info.json').write_text(json.dumps({
            'name': 'Test IDE', 'buildNumber': '263.1', 'dataDirectoryName': 'TestIDE2026.3',
            'launch': [{'launcherPath': 'bin/testide64.exe'}]}))
        self.ide = Installation.load(self.ide_path)
        self.jar = self.ide_path / 'lib' / 'art.jar'
        create_jar(self.jar)
        self.manager = Manager(self.ide, self.root / 'workspace')
        self.changes = {'lib/art.jar': {'art/logo.png': png(color='blue')}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_patch_preserves_offsets_size_non_targets_and_pixels(self):
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            for descriptor in (False, True):
                with self.subTest(method=method, descriptor=descriptor):
                    create_jar(self.jar, method, descriptor)
                    before = self.jar.read_bytes()
                    output = self.root / f'out-{method}-{descriptor}.jar'
                    patch_jar(self.jar, output, self.changes['lib/art.jar'])
                    self.assertEqual(before, self.jar.read_bytes())
                    self.assertEqual(len(before), output.stat().st_size)
                    with zipfile.ZipFile(self.jar) as a, zipfile.ZipFile(output) as b:
                        self.assertEqual(a.start_dir, b.start_dir)
                        self.assertEqual(a.comment, b.comment)
                        self.assertIsNone(b.testzip())
                        for x, y in zip(a.infolist(), b.infolist()):
                            self.assertEqual((x.header_offset, x.compress_size), (y.header_offset, y.compress_size))
                            if x.filename != 'art/logo.png':
                                self.assertEqual(a.read(x), b.read(y))
                        with Image.open(io.BytesIO(b.read('art/logo.png'))) as im:
                            self.assertEqual(im.getpixel((0, 0)), (0, 0, 255))

    def test_raw_changes_are_limited_to_selected_payload_and_metadata(self):
        output = self.root / 'out.jar'
        before = self.jar.read_bytes()
        patch_jar(self.jar, output, self.changes['lib/art.jar'])
        after = output.read_bytes()
        with zipfile.ZipFile(self.jar) as jar:
            info = jar.getinfo('art/logo.png')
            name_len, extra_len = struct.unpack_from('<HH', before, info.header_offset + 26)
            start = info.header_offset + 30 + name_len + extra_len
            cd = jar.start_dir
            allowed = set(range(start, start + info.compress_size))
            allowed.update(range(info.header_offset+14, info.header_offset+26))
            allowed.update(range(cd+16, cd+28))
        actual = {i for i, (a,b) in enumerate(zip(before,after)) if a != b}
        self.assertTrue(actual <= allowed)

    def test_padding_and_deflate_boundaries(self):
        image = png((1, 1), 'red')
        for target in (100, 65539, 65540, 65541, 131080, 131081):
            stream, padded = exact_size_deflate(image, target)
            self.assertEqual(len(stream), target)
            decoder = zlib.decompressobj(-15)
            self.assertEqual(decoder.decompress(stream), padded)
            self.assertTrue(decoder.eof)
            self.assertEqual(decoder.unused_data, b'')
        for gap in range(1,12):
            with self.assertRaises(ValueError):
                add_padding_chunk(image, len(image)+gap)

    def test_oversize_refused_without_output_or_source_changes(self):
        initial = digest(self.jar)
        too_big = add_padding_chunk(png(), 100_000)
        with self.assertRaises(ValueError):
            patch_jar(self.jar, self.root/'bad.jar', {'art/logo.png': too_big})
        self.assertEqual(digest(self.jar), initial)
        self.assertFalse((self.root/'bad.jar').exists())

    def test_signed_and_duplicate_archives_refused(self):
        create_jar(self.jar, signed=True)
        with self.assertRaisesRegex(ValueError, 'Signed'):
            patch_jar(self.jar, self.root/'signed.jar', self.changes['lib/art.jar'])
        create_jar(self.jar)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(self.jar, 'a') as jar:
                jar.writestr('art/logo.png', png())
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            patch_jar(self.jar, self.root/'duplicate.jar', self.changes['lib/art.jar'])

    def test_source_and_existing_outputs_refused(self):
        with self.assertRaises(ValueError):
            patch_jar(self.jar, self.jar, self.changes['lib/art.jar'])
        output = self.root/'out.jar'
        output.write_bytes(b'preserve me')
        with self.assertRaises(ValueError):
            patch_jar(self.jar, output, self.changes['lib/art.jar'])
        self.assertEqual(output.read_bytes(), b'preserve me')

    def test_invalid_png_dimensions_refused(self):
        with self.assertRaisesRegex(ValueError, 'Expected'):
            patch_jar(self.jar, self.root/'bad.jar', {'art/logo.png': png((2,2), 'red')})

    def test_scan_candidates_and_all_pngs(self):
        resources, warnings, count = scan(self.ide)
        self.assertEqual(count, 1)
        self.assertFalse(warnings)
        self.assertEqual(len(resources), 2)
        self.assertEqual(resources[0].pair_key, resources[1].pair_key)
        self.assertEqual(len(scan(self.ide, True)[0]), 3)

    def test_resize_fit_and_crop(self):
        image_path = self.root/'input.png'
        image_path.write_bytes(png((100,50), 'red'))
        for mode in ('Fit','Crop'):
            with Image.open(io.BytesIO(make_replacement(image_path, (50,50), mode))) as im:
                self.assertEqual(im.size, (50,50))
                self.assertEqual(im.getpixel((0,0)), (0,0,0) if mode == 'Fit' else (255,0,0))

    def test_confirmation_required(self):
        with self.assertRaisesRegex(ValueError, 'Confirm'):
            self.manager.prepare(self.changes)

    def test_apply_repeat_restore_round_trip(self):
        original = self.jar.read_bytes()
        first = self.manager.prepare(self.changes, confirmed_originals=True)
        self.assertEqual(original, self.jar.read_bytes())
        execute(first)
        self.assertNotEqual(original, self.jar.read_bytes())
        backup = self.manager.folder / self.manager.state()['jars']['lib/art.jar']['backup']
        self.assertEqual(original, backup.read_bytes())
        execute(self.manager.prepare({'lib/art.jar': {'art/logo@2x.png': png((160,100), 'green')}}))
        with zipfile.ZipFile(self.jar) as jar:
            with Image.open(io.BytesIO(jar.read('art/logo.png'))) as im:
                self.assertEqual(im.getpixel((0,0)), (0,0,255))
        execute(self.manager.prepare(None))
        self.assertEqual(original, self.jar.read_bytes())
        self.assertEqual(original, backup.read_bytes())
        self.assertFalse(self.manager.pending.exists())

    def test_helper_subprocess(self):
        plan = self.manager.prepare(self.changes, confirmed_originals=True)
        run_helper(plan)
        self.assertEqual(read_json(plan)['status'], 'complete')

    def test_import_existing_original(self):
        original = self.root/'original.jar'
        original.write_bytes(self.jar.read_bytes())
        out = self.root/'patched.jar'
        patch_jar(self.jar, out, self.changes['lib/art.jar'])
        self.jar.write_bytes(out.read_bytes())
        plan = self.manager.prepare({'lib/art.jar': {'art/logo.png': png(color='green')}},
                                    originals={'lib/art.jar': original})
        execute(plan)
        execute(self.manager.prepare(None))
        self.assertEqual(original.read_bytes(), self.jar.read_bytes())

    def test_external_modification_and_build_update_refused(self):
        execute(self.manager.prepare(self.changes, confirmed_originals=True))
        self.jar.write_bytes(self.jar.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'outside this app'):
            self.manager.prepare(None)
        data = json.loads((self.ide_path/'product-info.json').read_text())
        data['buildNumber'] = '264.1'
        (self.ide_path/'product-info.json').write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'build changed'):
            Manager(Installation.load(self.ide_path), self.root/'workspace').state()

    def test_corrupt_backup_refused(self):
        execute(self.manager.prepare(self.changes, confirmed_originals=True))
        backup = self.manager.folder/self.manager.state()['jars']['lib/art.jar']['backup']
        backup.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'backup was changed'):
            self.manager.prepare(None)

    def test_multi_jar_failure_rolls_back(self):
        second = self.ide_path/'lib'/'second.jar'
        create_jar(second)
        originals = {p: p.read_bytes() for p in (self.jar,second)}
        changes = {r: self.changes['lib/art.jar'] for r in ('lib/art.jar','lib/second.jar')}
        plan = self.manager.prepare(changes, confirmed_originals=True)
        calls = 0
        def fail_second(source, target, expected):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError('simulated locked JAR')
            return atomic_copy(source,target,expected)
        with patch('splash_patcher.transactions.atomic_copy', side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, 'rolled back'):
                execute(plan)
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse(self.manager.pending.exists())

    def test_crash_recovery(self):
        before = self.jar.read_bytes()
        plan = self.manager.prepare(self.changes, confirmed_originals=True)
        item = read_json(plan)['items'][0]
        self.jar.write_bytes(Path(item['after']).read_bytes())  # crash after replace, before journal commit
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            self.manager.prepare(self.changes)
        execute(self.manager.recover(), recover=True)
        self.assertEqual(before, self.jar.read_bytes())
        self.assertFalse(self.manager.pending.exists())

    def test_discard_prepared_operation(self):
        before = self.jar.read_bytes()
        self.manager.prepare(self.changes, confirmed_originals=True)
        execute(self.manager.recover(), recover=True)
        self.assertEqual(before, self.jar.read_bytes())

    def test_staging_tamper_refused(self):
        before = self.jar.read_bytes()
        plan = self.manager.prepare(self.changes, confirmed_originals=True)
        item = read_json(plan)['items'][0]
        Path(item['after']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            execute(plan)
        self.assertEqual(before, self.jar.read_bytes())

    def test_destination_escape_refused(self):
        outside = self.root/'outside.jar'
        create_jar(outside)
        with self.assertRaisesRegex(ValueError, 'Invalid installation'):
            self.manager.prepare({'../outside.jar': self.changes['lib/art.jar']}, confirmed_originals=True)

    def test_workspace_lock(self):
        with workspace_lock(self.manager.folder):
            with self.assertRaisesRegex(ValueError, 'Another operation'):
                with workspace_lock(self.manager.folder):
                    pass

    def test_cache_rename_and_guard(self):
        cache = self.root/'user'/'splash'
        cache.mkdir(parents=True)
        (cache/'cached-image').write_bytes(b'old image')
        clear_cache(self.ide,cache)
        self.assertFalse(cache.exists())
        renamed = list(cache.parent.glob('splash.backup-*'))
        self.assertEqual(len(renamed),1)
        self.assertEqual((renamed[0]/'cached-image').read_bytes(), b'old image')
        with self.assertRaises(ValueError):
            clear_cache(self.ide,self.root/'other')

    def test_running_ide_refused(self):
        with patch('splash_patcher.transactions.ensure_closed', side_effect=ValueError('Close IDE')):
            with self.assertRaisesRegex(ValueError, 'Close IDE'):
                self.manager.prepare(self.changes, confirmed_originals=True)
        self.assertFalse(self.manager.manifest.exists())


if __name__ == '__main__':
    unittest.main()
