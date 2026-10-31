"""Behavioral checks for incremental updates, freshness and per-user isolation."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'song_index.py'
spec = importlib.util.spec_from_file_location('song_discovery_index', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

TEXT_A = 'Когда бушуют ветры, Тебя благодарю. В испытаниях и скорби Твою любовь храню.'
TEXT_B = 'Ты дал надежду сердцу, Ты путь мой осветил. С Тобой пройду долину, Ты верность сохранил.'


def song(title='Песня А', lyrics=TEXT_A, **extra):
    return {'title': title, 'lyrics': lyrics, 'complete': True, **extra}


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = mod.Index(Path(self.tmp.name) / 'data' / 'index.sqlite3', 'test')
        self.addCleanup(self.index.close)

    def start(self, library=None, sources=None, complete=True):
        return self.index.start({'sources_complete': True, 'library_complete': complete,
            'sources': sources if sources is not None else [{'name': 'Община', 'url': 'https://example.org/music', 'type': 'Библиотека'}],
            'library': library or []})

    def inventory(self, run, items, complete=True):
        return self.index.plan(run['run_id'], run['sources'][0]['id'], {'complete': complete, 'items': items})

    def put(self, run, item='a', revision='1', songs=None):
        return self.index.put(run['run_id'], run['sources'][0]['id'], item,
            {'revision': revision, 'songs': songs if songs is not None else [song()]})

    def catalog(self, run):
        return self.index.catalog(run['run_id'])

    def test_only_new_and_changed_documents_are_read(self):
        run = self.start()
        items = [{'id': 'a', 'revision': '1'}, {'id': 'b', 'revision': '1'}]
        self.assertEqual(len(self.inventory(run, items)['read_required']), 2)
        self.put(run, songs=[song(summary='Благодарность в трудностях')])
        self.put(run, 'b', songs=[song('Песня Б', TEXT_B, summary='Упование на Бога')])
        again = self.start()
        self.assertEqual(self.inventory(again, items)['read_required'], [])
        update = [{'id': 'a', 'revision': '2'}, {'id': 'b', 'revision': '1'}, {'id': 'c', 'revision': '1'}]
        plan = self.inventory(again, update)
        self.assertEqual([(i['id'], i['reason']) for i in plan['read_required']], [('a', 'changed'), ('c', 'new')])
        self.assertEqual(plan['reused'], 1)
        self.assertEqual([s['title'] for s in self.catalog(again)['songs']], ['Песня Б'])
        result = self.put(again, revision='2', songs=[song(lyrics=TEXT_A + ' Господь, веди меня.')])
        self.assertEqual(len(result['analysis_needed']), 1)
        self.assertIsNone(next(s for s in self.catalog(again)['songs'] if s['title'] == 'Песня А')['summary'])

    def test_same_text_preserves_analysis_after_document_revision(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}])
        self.put(run, songs=[song(summary='Благодарность в трудностях', topics=['Благодарность'])])
        again = self.start(); self.inventory(again, [{'id': 'a', 'revision': '2'}])
        result = self.put(again, revision='2', songs=[song('Другое название', '[Am]\nКуплет 1\n' + TEXT_A.upper())])
        self.assertEqual(result['analysis_needed'], [])
        self.assertEqual(result['analysis_preserved'], 1)
        self.assertEqual(self.catalog(again)['songs'][0]['summary'], 'Благодарность в трудностях')

    def test_analysis_reused_for_a_copy_in_another_document(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}, {'id': 'b', 'revision': '1'}])
        self.put(run, songs=[song(summary='Благодарность в трудностях')])
        copied = self.put(run, 'b', songs=[song('Другое имя')])
        self.assertEqual(copied['analysis_needed'], [])
        catalog = self.catalog(run)
        self.assertEqual(catalog['total'], 1)
        self.assertEqual(len(catalog['songs'][0]['sources']), 2)

    def test_unknown_revision_requires_new_read_even_when_text_was_cached(self):
        run = self.start(); items = [{'id': 'a', 'revision': None}]
        self.inventory(run, items); self.put(run, revision=None, songs=[song(summary='Проверенный смысл')])
        again = self.start(); plan = self.inventory(again, items)
        self.assertEqual(plan['read_required'][0]['reason'], 'unknown_revision')
        self.assertEqual(self.catalog(again)['total'], 0)
        self.assertFalse(self.catalog(again)['coverage_complete'])
        result = self.put(again, revision=None)
        self.assertEqual(result['analysis_needed'], [])
        self.assertEqual(self.catalog(again)['total'], 1)

    def test_incomplete_inventory_does_not_remove_unseen_documents(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}, {'id': 'b', 'revision': '1'}])
        self.put(run); self.put(run, 'b', songs=[song('Б', TEXT_B)])
        partial = self.start(); result = self.inventory(partial, [{'id': 'a', 'revision': '1'}], complete=False)
        self.assertEqual(result['removed'], 0)
        self.assertEqual(self.index.db.execute('SELECT active FROM documents WHERE id="b"').fetchone()[0], 1)
        self.assertEqual(self.catalog(partial)['total'], 1)
        self.assertFalse(self.catalog(partial)['coverage_complete'])
        whole = self.start(); result = self.inventory(whole, [{'id': 'a', 'revision': '1'}])
        self.assertEqual(result['removed'], 1)
        self.assertTrue(self.catalog(whole)['coverage_complete'])

    def test_failed_read_cannot_serve_old_lyrics_as_fresh(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}]); self.put(run)
        again = self.start(); self.inventory(again, [{'id': 'a', 'revision': '2'}])
        self.index.fail(again['run_id'], again['sources'][0]['id'], 'a', 'OCR unavailable')
        self.assertEqual(self.catalog(again)['total'], 0)
        self.assertFalse(self.catalog(again)['coverage_complete'])
        self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM songs').fetchone()[0], 1)

    def test_empty_complete_inventory_removes_documents(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}]); self.put(run)
        again = self.start(); result = self.inventory(again, [])
        self.assertEqual(result['removed'], 1)
        self.assertEqual(self.catalog(again)['total'], 0)
        self.assertTrue(self.catalog(again)['coverage_complete'])

    def test_source_registry_is_current_and_applications_are_excluded(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}]); self.put(run)
        changed = self.start(sources=[{'name': 'Старый', 'url': 'https://example.org/music', 'type': ' Приложение '},
            {'name': 'Еще', 'url': 'https://example.org/app', 'type': 'ПРИЛОЖЕНИЯ'},
            {'name': 'Новый', 'url': 'https://new.example.org', 'type': 'Папки'}])
        self.assertEqual(changed['excluded_applications'], 2)
        self.assertEqual(len(changed['sources']), 1)
        self.assertEqual(self.catalog(changed)['total'], 0)
        with self.assertRaises(ValueError):
            self.index.plan(changed['run_id'], run['sources'][0]['id'], {'complete': True, 'items': []})

    def test_current_own_library_excludes_aliases_and_not_just_same_titles(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}, {'id': 'b', 'revision': '1'}])
        self.put(run); self.put(run, 'b', songs=[song('Песня А', TEXT_B)])
        again = self.start(library=[song('Другое название', 'Куплет 1\nAm F C G\n' + TEXT_A.upper())])
        self.inventory(again, [{'id': 'a', 'revision': '1'}, {'id': 'b', 'revision': '1'}])
        self.assertEqual(self.catalog(again)['total'], 1)
        self.assertEqual(self.index.get(again['run_id'], self.catalog(again)['songs'][0]['id'])['lyrics'], TEXT_B)
        incomplete = self.start(complete=False)
        self.inventory(incomplete, [{'id': 'a', 'revision': '1'}, {'id': 'b', 'revision': '1'}])
        self.assertFalse(self.catalog(incomplete)['library_check_ready'])

    def test_multi_song_document_is_replaced_atomically(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}])
        self.put(run, songs=[song('А', TEXT_A), song('Б', TEXT_B)])
        again = self.start(); self.inventory(again, [{'id': 'a', 'revision': '2'}])
        with self.assertRaises(ValueError):
            self.put(again, revision='2', songs=[song(), {'title': 'broken'}])
        self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM songs').fetchone()[0], 2)
        self.put(again, revision='2', songs=[song()])
        self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM songs').fetchone()[0], 1)

    def test_stale_revision_and_analysis_cannot_overwrite_current_text(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}]); self.put(run)
        old_song = self.catalog(run)['songs'][0]
        another = self.start(); self.inventory(another, [{'id': 'a', 'revision': '2'}])
        self.assertEqual(self.catalog(run)['total'], 0)
        self.assertFalse(self.catalog(run)['coverage_complete'])
        with self.assertRaises(ValueError): self.put(run)
        self.put(another, revision='2', songs=[song(lyrics=TEXT_B, key='same')])
        with self.assertRaises(ValueError):
            self.index.analyze(old_song['id'], {'lyrics_hash': old_song['lyrics_hash'], 'summary': 'Старый смысл'})

    def test_catalog_is_not_restricted_to_keywords_and_is_paginated(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}, {'id': 'b', 'revision': '1'}])
        self.put(run, songs=[song(summary='Благодарность Богу')]); self.put(run, 'b', songs=[song('Б', TEXT_B, summary='Упование')])
        first = self.index.catalog(run['run_id'], limit=1)
        second = self.index.catalog(run['run_id'], limit=1, offset=first['next_offset'])
        self.assertEqual(first['total'], 2)
        self.assertNotEqual(first['songs'][0]['id'], second['songs'][0]['id'])
        self.assertFalse(first['lexical_filter_only'])
        self.assertEqual(self.index.catalog(run['run_id'], query='благодарность')['total'], 1)

    def test_incomplete_lyrics_are_not_verified_recommendations(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}])
        self.put(run, songs=[song(complete=False)])
        self.assertEqual(self.catalog(run)['total'], 0)
        self.assertFalse(self.catalog(run)['coverage_complete'])
        self.assertEqual(self.catalog(run)['incomplete_song_texts'], 1)
        again = self.start()
        retry = self.inventory(again, [{'id': 'a', 'revision': '1'}])
        self.assertEqual(retry['read_required'][0]['reason'], 'incomplete_text')

    def test_analyzing_one_copy_updates_all_identical_lyrics(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}, {'id': 'b', 'revision': '1'}])
        self.put(run); self.put(run, 'b')
        item = self.catalog(run)['songs'][0]
        result = self.index.analyze(item['id'], {'lyrics_hash': item['lyrics_hash'], 'summary': 'Благодарность в трудностях'})
        self.assertEqual(result['updated_copies'], 2)
        self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM songs WHERE summary IS NULL').fetchone()[0], 0)

    def test_sql_import_error_rolls_back_document_replacement(self):
        run = self.start(); self.inventory(run, [{'id': 'a', 'revision': '1'}]); self.put(run)
        again = self.start(); self.inventory(again, [{'id': 'a', 'revision': '2'}])
        with self.assertRaises(sqlite3.Error):
            self.put(again, revision='2', songs=[song(author={'not': 'a string'})])
        self.assertEqual(self.index.db.execute('SELECT lyrics FROM songs').fetchone()[0], TEXT_A)
        self.assertEqual(self.catalog(again)['total'], 0)

    def test_missing_own_lyrics_are_reported_even_with_complete_row_snapshot(self):
        run = self.start(library=[{'title': 'Песня без текста'}])
        self.inventory(run, [{'id': 'a', 'revision': '1'}]); self.put(run)
        self.assertFalse(self.catalog(run)['library_check_ready'])
        self.assertEqual(self.catalog(run)['library_missing_lyrics'], 1)

    def test_same_title_only_flags_a_possible_match(self):
        run = self.start(library=[song('Песня А', TEXT_B)])
        self.inventory(run, [{'id': 'a', 'revision': '1'}]); self.put(run)
        found = self.catalog(run)['songs']
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['possible_library_matches'][0]['title'], 'Песня А')

    def test_profile_isolation_and_future_schema_are_preserved(self):
        with self.assertRaises(ValueError): mod.Index(self.index.path, 'another user/library')
        other = Path(self.tmp.name) / 'future.sqlite3'
        db = sqlite3.connect(other); db.execute('PRAGMA user_version=999'); db.close()
        with self.assertRaises(ValueError): mod.Index(other)
        db = sqlite3.connect(other)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 999); db.close()

    def test_no_registry_update_from_partial_snapshot(self):
        run = self.start()
        with self.assertRaises(ValueError): self.index.start({'sources_complete': False, 'library_complete': True, 'sources': [], 'library': []})
        self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM sources WHERE active=1').fetchone()[0], 1)


class PathAndCliTests(unittest.TestCase):
    def test_storage_is_per_user_and_profile_outside_skill(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(mod.sys, 'platform', 'darwin'), patch.object(mod.Path, 'home', return_value=Path('/example-user')):
            first = mod.default_path('one'); second = mod.default_path('two')
            self.assertTrue(str(first).startswith('/example-user/Library/Application Support/'))
            self.assertNotEqual(first, second)
        with patch.dict(os.environ, {'SONG_DISCOVERY_DB': '/custom/music.sqlite3'}, clear=True):
            self.assertEqual(mod.default_path(), Path('/custom/music.sqlite3'))
        with patch.dict(os.environ, {'XDG_DATA_HOME': 'relative-to-repo'}, clear=True), patch.object(mod.sys, 'platform', 'linux'), patch.object(mod.Path, 'home', return_value=Path('/example-user')):
            self.assertTrue(str(mod.default_path()).startswith('/example-user/.local/share/'))

    def test_cli_creates_database_on_first_run_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); db = root / 'private' / 'index.sqlite3'
            def call(command, payload=None):
                if payload is not None:
                    input_path = root / 'input.json'; input_path.write_text(json.dumps(payload), encoding='utf-8')
                    command += ['--input', str(input_path)]
                result = subprocess.run([sys.executable, str(SCRIPT), '--db', str(db), *command], text=True, capture_output=True, check=True)
                return json.loads(result.stdout)
            snapshot = {'sources_complete': True, 'library_complete': True, 'library': [],
                'sources': [{'url': 'https://example.org/music', 'name': 'Община', 'type': 'Библиотека'}]}
            run = call(['start'], snapshot); source = run['sources'][0]['id']
            common = ['--run', run['run_id'], '--source', source]
            call(['plan', *common], {'complete': True, 'items': [{'id': 'a', 'revision': 'v1'}]})
            stored = call(['put', *common, '--item', 'a'], {'revision': 'v1', 'songs': [song()]})
            found = call(['get', '--run', run['run_id'], '--song', stored['analysis_needed'][0]])
            call(['analyze', '--song', found['id']], {'lyrics_hash': found['lyrics_hash'], 'summary': 'Верность в испытаниях'})
            catalog = call(['catalog', '--run', run['run_id']])
            self.assertEqual(catalog['songs'][0]['summary'], 'Верность в испытаниях')
            again = call(['start'], snapshot)
            reused = call(['plan', '--run', again['run_id'], '--source', source], {'complete': True, 'items': [{'id': 'a', 'revision': 'v1'}]})
            self.assertEqual(reused['read_required'], [])
            self.assertTrue(db.exists())
            if os.name != 'nt': self.assertEqual(db.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
