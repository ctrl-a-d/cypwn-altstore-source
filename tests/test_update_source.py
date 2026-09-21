import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts import update_source as cleaner

URL = 'https://example.github.io/cypwn-altstore-source/source.json'


def app(version='1.0', bundle='com.example.a', **extra):
    return dict(name='App ' + version, bundleIdentifier=bundle, developerName='Example',
                localizedDescription='', version=version, versionDate='2026-01-01',
                downloadURL='https://example.com/' + version + '.ipa',
                iconURL='https://example.com/icon.png', size=100, **extra)


def source(*apps):
    return dict(name='Upstream', apps=list(apps), news=[])


class CleanerTests(unittest.TestCase):
    def setUp(self):
        self.quiet = patch('sys.stdout', new_callable=io.StringIO)
        self.quiet.start()
        self.addCleanup(self.quiet.stop)

    def clean(self, *apps):
        return cleaner.transform(source(*apps), URL)['apps']

    def test_distinct_apps_and_unknown_metadata_preserved(self):
        entries = [app(extra={'localized': ['hello']}), app(bundle='com.example.b')]
        original = copy.deepcopy(entries)
        self.assertEqual(self.clean(*entries), original)
        self.assertEqual(entries, original)

    def test_newest_numeric(self):
        for versions in [('1.0', '2.0'), ('2.0.9', '2.0.10'), ('1.9', '1.10')]:
            with self.subTest(versions=versions):
                for entries in [list(map(app, versions)), list(reversed(list(map(app, versions))))]:
                    self.assertEqual(self.clean(*entries), [app(versions[1])])

    def test_semver(self):
        self.assertLess(cleaner.version_key('2.0.0-rc.9'), cleaner.version_key('2.0.0-rc.10'))
        self.assertLess(cleaner.version_key('2.0.0-rc.10'), cleaner.version_key('2.0.0'))
        self.assertEqual(cleaner.version_key('v2.0.0+build.1'), cleaner.version_key('2'))

    def test_opaque_version_uses_date(self):
        old, new = app('weird!'), app('1.0.1o')
        new['versionDate'] = '2026-02-01'
        self.assertEqual(self.clean(new, old), [new])

    def test_timezone_date_comparison(self):
        old, new = app('unknown'), app('another')
        old['versionDate'] = '2026-01-01T02:00:00+03:00'
        new['versionDate'] = '2026-01-01T00:00:00Z'
        self.assertEqual(self.clean(old, new), [new])

    def test_build_fallback(self):
        self.assertEqual(self.clean(app('odd', buildVersion='9'), app('other', buildVersion='10')),
                         [app('other', buildVersion='10')])

    def test_ambiguous_duplicate_refused(self):
        with self.assertRaisesRegex(ValueError, 'Ambiguous duplicate'):
            self.clean(app('opaque'), app('other'))

    def test_identical_duplicates_allowed(self):
        self.assertEqual(self.clean(app(), app()), [app()])

    def test_modern_versions_preserved_and_first_is_current(self):
        a, b = app(), app('2')
        for entry in (a, b):
            entry['versions'] = [{
                'version': entry['version'], 'date': entry['versionDate'],
                'size': entry['size'], 'downloadURL': entry['downloadURL']}]
            for key in ('version', 'versionDate', 'size', 'downloadURL'):
                del entry[key]
        self.assertEqual(self.clean(a, b), [b])

    def test_case_sensitive_bundle_ids(self):
        self.assertEqual(len(self.clean(app(bundle='com.A'), app(bundle='com.a'))), 2)

    def test_invalid_shapes_fields_and_dates(self):
        cases = [None, [], {}, source(), dict(name='X', apps={})]
        for field, value in [('size', True), ('versionDate', 'yesterday'), ('downloadURL', 'javascript:bad'),
                             ('iconURL', None), ('bundleIdentifier', ''), ('versions', []), ('tintColor', 'red')]:
            entry = app()
            entry[field] = value
            cases.append(source(entry))
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                cleaner.transform(value, URL)

    def test_strict_json(self):
        for data in ['<html>Error</html>', '{"apps":[],"apps":[]}', '{"a":NaN}']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                cleaner.parse_json(data)

    def test_truncation_guard_and_override(self):
        baseline = source(*(app(bundle=f'com.example.{i}') for i in range(10)))
        cleaner.guard_count(source(*baseline['apps'][:8]), baseline, min_apps=1)
        with self.assertRaisesRegex(ValueError, '20%'):
            cleaner.guard_count(source(*baseline['apps'][:7]), baseline, min_apps=1)
        cleaner.guard_count(source(app()), baseline, min_apps=1, allow_large_drop=True)
        with self.assertRaises(ValueError):
            cleaner.guard_count(source(app()), None)

    def test_failed_update_preserves_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output, upstream = Path(directory)/'source.json', Path(directory)/'input.json'
            original = json.dumps(cleaner.transform(source(app()), URL)).encode()
            output.write_bytes(original)
            for data in ['not json', json.dumps(source()), json.dumps(source(app('odd'), app('unknown')))]:
                upstream.write_text(data)
                with self.assertRaises(ValueError):
                    cleaner.main(['--input', str(upstream), '--output', str(output), '--source-url', URL, '--min-apps', '1'])
                self.assertEqual(output.read_bytes(), original)
            with patch.object(cleaner, 'fetch_json', side_effect=URLError('offline')):
                with self.assertRaises(URLError):
                    cleaner.main(['--output', str(output), '--source-url', URL])
            self.assertEqual(output.read_bytes(), original)

    def test_change_detection_ignores_json_key_order(self):
        with tempfile.TemporaryDirectory() as directory:
            output, upstream, github = [Path(directory)/name for name in ['source.json', 'input.json', 'output']]
            upstream.write_text(json.dumps(source(app())))
            args = ['--input', str(upstream), '--output', str(output), '--source-url', URL, '--min-apps', '1']
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(github)}):
                cleaner.main(args)
                output.write_text(json.dumps(json.loads(output.read_text()), sort_keys=True))
                cleaner.main(args)
            self.assertEqual(github.read_text(), 'changed=true\nchanged=false\n')

    def test_published_baseline_errors_fail_closed(self):
        for code in (403, 404, 500):
            error = HTTPError(URL, code, 'error', {}, None)
            with patch.object(cleaner, 'urlopen', side_effect=error), patch.object(cleaner.time, 'sleep'):
                with self.assertRaises(HTTPError):
                    cleaner.fetch_json(URL)
                if code == 404:
                    self.assertIsNone(cleaner.fetch_json(URL, allow_missing=True))
                else:
                    with self.assertRaises(HTTPError):
                        cleaner.fetch_json(URL, allow_missing=True)

    def test_atomic_replace_failure_preserves_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'source.json'
            target.write_text('original')
            with patch.object(cleaner.os, 'replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    cleaner.write_atomic(target, source(app()))
            self.assertEqual(target.read_text(), 'original')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_logging_cannot_emit_workflow_commands(self):
        stream = io.StringIO()
        with patch('sys.stdout', stream):
            cleaner.log('hello\n::error::injected\r::warning::no')
        self.assertEqual(len(stream.getvalue().splitlines()), 1)
        self.assertTrue(stream.getvalue().startswith('[cleaner] '))


if __name__ == '__main__':
    unittest.main()
