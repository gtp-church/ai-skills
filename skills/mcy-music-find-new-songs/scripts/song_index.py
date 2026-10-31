#!/usr/bin/env python3
"""Local, connector-independent cache. JSON in/out; no network or API credentials."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

DEFAULT_PROFILE = '1YelbhnrZTGDl1uHC8oG43-Y9XXjEagWbEDdSTrflzkc:1111321200:0'
SCHEMA_VERSION = 1
ANALYSIS_VERSION = '1'
APP_TYPES = {'приложение', 'приложения'}
CHORD = re.compile(r'[A-H](?:#|b)?(?:m|maj|min|dim|aug|sus|add)?\d*(?:/[A-H](?:#|b)?)?')
LABEL = re.compile(r'(?:куплет|припев|бридж|мост|вступление|проигрыш|кода|повтор|verse|chorus|bridge|intro|outro)(?:\s*\d+)?\s*[:.\-]?', re.I)


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def normalized(text):
    lines = []
    for line in unicodedata.normalize('NFKC', text or '').splitlines():
        line = re.sub(r'\[([^\]]+)\]', lambda m: '' if CHORD.fullmatch(m[1].strip()) else m[0], line)
        tokens = line.split()
        if LABEL.fullmatch(line.strip()) or (tokens and all(CHORD.fullmatch(t) for t in tokens)):
            continue
        lines.append(line)
    text = '\n'.join(lines).casefold().replace('ё', 'е')
    return ' '.join(re.findall(r'[^\W_]+', text, re.UNICODE))


def source_key(url):
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError('Each source needs an absolute URL')
    host = (parts.hostname or '').lower()
    if host in {'drive.google.com', 'docs.google.com'}:
        match = re.search(r'/(?:folders|d)/([\w-]+)', parts.path)
        file_id = match[1] if match else dict(parse_qsl(parts.query)).get('id')
        if file_id:
            if '/spreadsheets/' in parts.path:
                params = dict(parse_qsl(parts.query)); params.update(parse_qsl(parts.fragment))
                return 'sheet:' + file_id + ':' + params.get('gid', '0')
            return 'drive:' + file_id
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if not k.lower().startswith('utm_') and k.lower() not in {'si', 'usp'}]
    if host in {'youtube.com', 'www.youtube.com', 'm.youtube.com'} and dict(params).get('list'):
        return 'youtube:playlist:' + dict(params)['list']
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/') or '/',
                       urlencode(sorted(params)), ''))


def default_path(profile=DEFAULT_PROFILE):
    if os.environ.get('SONG_DISCOVERY_DB'):
        return Path(os.environ['SONG_DISCOVERY_DB']).expanduser()
    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    elif sys.platform == 'win32':
        configured = Path(os.environ.get('LOCALAPPDATA', ''))
        base = configured if configured.is_absolute() else Path.home() / 'AppData' / 'Local'
    else:
        configured = Path(os.environ.get('XDG_DATA_HOME', ''))
        base = configured if configured.is_absolute() else Path.home() / '.local' / 'share'
    return base / 'church-skills' / 'song-discovery' / digest(profile)[:16] / 'index.sqlite3'


SCHEMA = '''
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, kind TEXT NOT NULL,
  active INTEGER NOT NULL, registry_checked_at TEXT NOT NULL
);
CREATE TABLE runs (
  id TEXT PRIMARY KEY, started_at TEXT NOT NULL, library_complete INTEGER NOT NULL
);
CREATE TABLE library (
  run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL, title TEXT NOT NULL,
  normalized_title TEXT NOT NULL, lyrics_hash TEXT, PRIMARY KEY(run_id,id)
);
CREATE INDEX library_hash ON library(run_id,lyrics_hash);
CREATE TABLE run_sources (
  run_id TEXT NOT NULL REFERENCES runs(id), source_id TEXT NOT NULL REFERENCES sources(id),
  inventory_complete INTEGER NOT NULL DEFAULT 0, scanned_at TEXT,
  PRIMARY KEY(run_id,source_id)
);
CREATE TABLE documents (
  source_id TEXT NOT NULL REFERENCES sources(id), id TEXT NOT NULL,
  title TEXT NOT NULL, url TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  remote_revision TEXT, cached_revision TEXT, fetched_run TEXT, fetched_at TEXT,
  PRIMARY KEY(source_id,id)
);
CREATE TABLE run_documents (
  run_id TEXT NOT NULL, source_id TEXT NOT NULL, item_id TEXT NOT NULL,
  revision TEXT, state TEXT NOT NULL, error TEXT,
  PRIMARY KEY(run_id,source_id,item_id),
  FOREIGN KEY(run_id,source_id) REFERENCES run_sources(run_id,source_id),
  FOREIGN KEY(source_id,item_id) REFERENCES documents(source_id,id)
);
CREATE TABLE songs (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, item_id TEXT NOT NULL, song_key TEXT NOT NULL,
  title TEXT NOT NULL, author TEXT, lyrics TEXT NOT NULL, lyrics_hash TEXT NOT NULL,
  complete INTEGER NOT NULL, summary TEXT, topics TEXT NOT NULL, links TEXT NOT NULL,
  analysis_version TEXT, FOREIGN KEY(source_id,item_id) REFERENCES documents(source_id,id),
  UNIQUE(source_id,item_id,song_key)
);
CREATE INDEX songs_hash ON songs(lyrics_hash);
'''


class Index:
    def __init__(self, path, profile=DEFAULT_PROFILE):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        try:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                if version == 0:
                    if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                        raise ValueError('Not a Song Discovery database')
                    for statement in SCHEMA.split(';'):
                        if statement.strip(): self.db.execute(statement)
                    self.db.execute('INSERT INTO settings VALUES (?,?)', ('profile', profile))
                    self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                elif version != SCHEMA_VERSION:
                    raise ValueError(f'Unsupported database schema: {version}')
                owner = self.db.execute("SELECT value FROM settings WHERE key='profile'").fetchone()
                if not owner or owner[0] != profile:
                    raise ValueError('Database belongs to a different profile; use a separate --db')
        except Exception:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def require_run(self, run_id):
        row = self.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise ValueError('Unknown run; start with a fresh registry/library snapshot')
        return row

    def require_source(self, run_id, source_id):
        self.require_run(run_id)
        row = self.db.execute('''SELECT s.* FROM sources s JOIN run_sources r ON r.source_id=s.id
            WHERE r.run_id=? AND s.id=? AND s.active=1''', (run_id, source_id)).fetchone()
        if not row:
            raise ValueError('Source is absent or excluded in this run')
        return row

    def start(self, payload):
        if payload.get('sources_complete') is not True:
            raise ValueError('A complete freshly read source registry is required')
        if not isinstance(payload.get('library_complete'), bool):
            raise ValueError('Set library_complete explicitly')
        raw_sources = payload.get('sources'); library = payload.get('library')
        if not isinstance(raw_sources, list) or not isinstance(library, list):
            raise ValueError('sources and library must be arrays')
        sources = {}; excluded = 0
        for raw in raw_sources:
            kind = str(raw.get('type', '')).strip()
            if kind.casefold() in APP_TYPES:
                excluded += 1; continue
            url = str(raw.get('url', '')).strip()
            if not url or url.upper() == 'TBA':
                continue
            key = source_key(url)
            sources[key] = {'id': key, 'name': str(raw.get('name') or url), 'url': url, 'type': kind}
        run_id = uuid.uuid4().hex; stamp = now()
        with self.db:
            self.db.execute('INSERT INTO runs VALUES (?,?,?)', (run_id, stamp, int(payload['library_complete'])))
            self.db.execute('UPDATE sources SET active=0')
            for source in sources.values():
                self.db.execute('''INSERT INTO sources VALUES (?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,url=excluded.url,kind=excluded.kind,active=1,registry_checked_at=excluded.registry_checked_at''',
                    (source['id'], source['name'], source['url'], source['type'], stamp))
                self.db.execute('INSERT INTO run_sources(run_id,source_id) VALUES (?,?)', (run_id, source['id']))
            for i, song in enumerate(library):
                title = str(song.get('title') or '').strip(); lyrics = str(song.get('lyrics') or '')
                if not title and not lyrics.strip():
                    continue
                text = normalized(lyrics)
                self.db.execute('INSERT INTO library VALUES (?,?,?,?,?)',
                    (run_id, str(song.get('id', i)), title, normalized(title), digest(text) if text and song.get('complete', True) else None))
        return {'run_id': run_id, 'db': str(self.path), 'sources': list(sources.values()),
                'excluded_applications': excluded, 'library_complete': payload['library_complete']}

    def plan(self, run_id, source_id, payload):
        source = self.require_source(run_id, source_id)
        if not isinstance(payload.get('complete'), bool) or not isinstance(payload.get('items'), list):
            raise ValueError('Inventory requires items and explicit complete flag')
        items = payload['items']; ids = set(); prepared = []
        for item in items:
            item_id = str(item.get('id') or '').strip()
            if not item_id or item_id in ids:
                raise ValueError('Inventory item IDs must be present and unique')
            ids.add(item_id)
            revision = item.get('revision')
            if revision is not None and (not isinstance(revision, str) or not revision.strip()):
                raise ValueError('revision must be a reliable nonempty string or null')
            prepared.append((item_id, str(item.get('title') or item_id), str(item.get('url') or source['url']), revision))
        required = []; reused = 0; removed = 0
        with self.db:
            self.db.execute('DELETE FROM run_documents WHERE run_id=? AND source_id=?', (run_id, source_id))
            for item_id, title, url, revision in prepared:
                old = self.db.execute('SELECT * FROM documents WHERE source_id=? AND id=?', (source_id, item_id)).fetchone()
                incomplete = self.db.execute('SELECT 1 FROM songs WHERE source_id=? AND item_id=? AND complete=0 LIMIT 1',
                    (source_id, item_id)).fetchone()
                valid = bool(old and old['fetched_at'] and not incomplete and revision is not None and revision == old['cached_revision'])
                state = 'ready' if valid else 'pending'
                self.db.execute('''INSERT INTO documents(source_id,id,title,url,remote_revision) VALUES (?,?,?,?,?)
                    ON CONFLICT(source_id,id) DO UPDATE SET title=excluded.title,url=excluded.url,
                    remote_revision=excluded.remote_revision,active=1''', (source_id, item_id, title, url, revision))
                self.db.execute('INSERT INTO run_documents VALUES (?,?,?,?,?,NULL)',
                    (run_id, source_id, item_id, revision, state))
                if valid:
                    reused += 1
                else:
                    reason = ('new' if not old or not old['fetched_at'] else
                              'unknown_revision' if revision is None else
                              'incomplete_text' if incomplete and revision == old['cached_revision'] else 'changed')
                    required.append({'id': item_id, 'title': title, 'url': url, 'revision': revision, 'reason': reason})
            if payload['complete']:
                active = self.db.execute('SELECT id FROM documents WHERE source_id=? AND active=1', (source_id,)).fetchall()
                missing = [r[0] for r in active if r[0] not in ids]
                for item_id in missing:
                    self.db.execute('UPDATE documents SET active=0 WHERE source_id=? AND id=?', (source_id, item_id))
                removed = len(missing)
            self.db.execute('UPDATE run_sources SET inventory_complete=?,scanned_at=? WHERE run_id=? AND source_id=?',
                (int(payload['complete']), now(), run_id, source_id))
        return {'run_id': run_id, 'source_id': source_id, 'read_required': required, 'reused': reused,
                'removed': removed, 'inventory_complete': payload['complete']}

    def document(self, run_id, source_id, item_id):
        self.require_source(run_id, source_id)
        row = self.db.execute('''SELECT d.*,r.revision,r.state FROM documents d JOIN run_documents r
            ON r.source_id=d.source_id AND r.item_id=d.id WHERE r.run_id=? AND d.source_id=? AND d.id=?''',
            (run_id, source_id, item_id)).fetchone()
        if not row:
            raise ValueError('Item was not listed in this run; call plan first')
        return row

    def put(self, run_id, source_id, item_id, payload):
        row = self.document(run_id, source_id, item_id)
        if 'revision' not in payload or payload['revision'] != row['revision'] or row['remote_revision'] != row['revision']:
            raise ValueError('Revision changed; refresh inventory before importing this document')
        if not isinstance(payload.get('songs'), list):
            raise ValueError('songs must contain every extracted song from this document')
        previous = {r['lyrics_hash']: dict(r) for r in self.db.execute('SELECT * FROM songs WHERE source_id=? AND item_id=?', (source_id, item_id))}
        prepared = []; keys = set(); preserved = 0
        for song in payload['songs']:
            title = str(song.get('title') or '').strip(); lyrics = song.get('lyrics')
            if not title or not isinstance(lyrics, str) or not normalized(lyrics):
                raise ValueError('Each song requires a title and nonempty extracted lyrics')
            if not isinstance(song.get('complete'), bool):
                raise ValueError('Set complete explicitly for every song text')
            text_hash = digest(normalized(lyrics)); key = str(song.get('key') or text_hash)
            if key in keys:
                raise ValueError('Song keys must be unique within a document')
            keys.add(key); old = previous.get(text_hash)
            if not old or not old['summary']:
                shared = self.db.execute('SELECT * FROM songs WHERE lyrics_hash=? AND summary IS NOT NULL AND analysis_version=? LIMIT 1',
                    (text_hash, ANALYSIS_VERSION)).fetchone()
                if shared:
                    old = dict(shared)
            summary = song.get('summary'); topics = song.get('topics', [])
            if summary is not None and not isinstance(summary, str):
                raise ValueError('summary must be text')
            if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
                raise ValueError('topics must be an array of strings')
            analysis_version = ANALYSIS_VERSION if summary else None
            if summary is None and old and old['analysis_version'] == ANALYSIS_VERSION:
                summary = old['summary']; topics = json.loads(old['topics']); analysis_version = old['analysis_version']
                preserved += bool(summary)
            links = song.get('links', [])
            if not isinstance(links, list) or not all(isinstance(link, str) for link in links):
                raise ValueError('links must be an array of verified URLs')
            song_id = digest(source_id + '\0' + item_id + '\0' + key)
            prepared.append((song_id, source_id, item_id, key, title, song.get('author'), lyrics, text_hash,
                int(song['complete']), summary, json.dumps(topics, ensure_ascii=False), json.dumps(links, ensure_ascii=False), analysis_version))
        with self.db:
            # Recheck inside the transaction: a newer inventory must not be overwritten.
            self.db.execute('BEGIN IMMEDIATE')
            current = self.document(run_id, source_id, item_id)
            if current['remote_revision'] != payload['revision']:
                raise ValueError('Concurrent inventory change; plan again')
            self.db.execute('DELETE FROM songs WHERE source_id=? AND item_id=?', (source_id, item_id))
            self.db.executemany('INSERT INTO songs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', prepared)
            self.db.execute('UPDATE documents SET cached_revision=?,fetched_run=?,fetched_at=? WHERE source_id=? AND id=?',
                (payload['revision'], run_id, now(), source_id, item_id))
            self.db.execute("UPDATE run_documents SET state='ready',error=NULL WHERE run_id=? AND source_id=? AND item_id=?", (run_id, source_id, item_id))
        return {'imported': len(prepared), 'analysis_preserved': preserved,
                'analysis_needed': [r[0] for r in prepared if r[8] and not r[9]]}

    def fail(self, run_id, source_id, item_id, reason):
        self.document(run_id, source_id, item_id)
        if not reason.strip():
            raise ValueError('Describe why the document could not be read')
        with self.db:
            self.db.execute("UPDATE run_documents SET state='error',error=? WHERE run_id=? AND source_id=? AND item_id=?",
                (reason, run_id, source_id, item_id))
        return {'state': 'error', 'cached_content_retained': True}

    def analyze(self, song_id, payload):
        row = self.db.execute('SELECT * FROM songs WHERE id=?', (song_id,)).fetchone()
        if not row or row['lyrics_hash'] != payload.get('lyrics_hash'):
            raise ValueError('Song text changed or is absent; read it again before analyzing')
        if not isinstance(payload.get('summary'), str) or not payload['summary'].strip():
            raise ValueError('A nonempty meaning summary is required')
        topics = payload.get('topics', [])
        if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
            raise ValueError('topics must be an array of strings')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            current = self.db.execute('SELECT lyrics_hash FROM songs WHERE id=?', (song_id,)).fetchone()
            if not current or current[0] != row['lyrics_hash']:
                raise ValueError('Song changed during analysis; read it again')
            result = self.db.execute('UPDATE songs SET summary=?,topics=?,analysis_version=? WHERE lyrics_hash=?',
                (payload['summary'], json.dumps(topics, ensure_ascii=False), ANALYSIS_VERSION, row['lyrics_hash']))
        return {'id': song_id, 'analysis_version': ANALYSIS_VERSION, 'updated_copies': result.rowcount}

    def fresh_rows(self, run_id, song_id=None, include_lyrics=False):
        self.require_run(run_id)
        columns = ('g.*' if include_lyrics else 'g.id,g.source_id,g.item_id,g.song_key,g.title,g.author,g.lyrics_hash,g.complete,g.summary,g.topics,g.links,g.analysis_version')
        restriction = ' AND g.id=?' if song_id else ''
        args = (run_id, run_id, song_id) if song_id else (run_id, run_id)
        return self.db.execute(f'''SELECT {columns},s.name AS source_name,d.url AS document_url FROM songs g
            JOIN documents d ON d.source_id=g.source_id AND d.id=g.item_id
            JOIN sources s ON s.id=g.source_id
            JOIN run_documents r ON r.source_id=g.source_id AND r.item_id=g.item_id
            WHERE r.run_id=? AND r.state='ready' AND d.active=1 AND s.active=1 AND d.remote_revision IS r.revision AND
              ((r.revision IS NOT NULL AND r.revision=d.cached_revision) OR
               (r.revision IS NULL AND d.cached_revision IS NULL AND d.fetched_run=?))
            {restriction} ORDER BY g.id''', args).fetchall()

    def status(self, run_id):
        run = self.require_run(run_id)
        fresh = self.fresh_rows(run_id)
        incomplete = sum(not r['complete'] for r in fresh)
        missing_library = self.db.execute('SELECT COUNT(*) FROM library WHERE run_id=? AND lyrics_hash IS NULL', (run_id,)).fetchone()[0]
        sources = []
        for source in self.db.execute('''SELECT s.*,r.inventory_complete,r.scanned_at FROM sources s
                JOIN run_sources r ON r.source_id=s.id WHERE r.run_id=?''', (run_id,)):
            counts = dict(self.db.execute('SELECT state,COUNT(*) FROM run_documents WHERE run_id=? AND source_id=? GROUP BY state', (run_id, source['id'])))
            # Zero-song documents can also be verified, so use document revisions for coverage.
            pending = self.db.execute('''SELECT COUNT(*) FROM run_documents r JOIN documents d
                ON d.source_id=r.source_id AND d.id=r.item_id WHERE r.run_id=? AND r.source_id=? AND
                (r.state!='ready' OR d.active=0 OR d.remote_revision IS NOT r.revision OR NOT ((r.revision IS NOT NULL AND r.revision=d.cached_revision) OR
                (r.revision IS NULL AND d.cached_revision IS NULL AND d.fetched_run=?)))''', (run_id, source['id'], run_id)).fetchone()[0]
            sources.append({'id': source['id'], 'name': source['name'], 'inventory_complete': bool(source['inventory_complete']),
                'scanned': bool(source['scanned_at']), 'states': counts, 'unverified_documents': pending,
                'fresh': bool(source['active'] and source['inventory_complete'] and source['scanned_at'] and pending == 0)})
        return {'run_id': run_id, 'db': str(self.path), 'library_complete': bool(run['library_complete']),
                'sources': sources, 'coverage_complete': all(s['fresh'] for s in sources) and incomplete == 0,
                'incomplete_song_texts': incomplete, 'library_missing_lyrics': missing_library,
                'current_song_copies': sum(bool(r['complete']) for r in fresh)}

    def catalog(self, run_id, query=None, limit=100, offset=0):
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError('Use limit 1..1000 and nonnegative offset')
        status = self.status(run_id)
        own = self.db.execute('SELECT * FROM library WHERE run_id=?', (run_id,)).fetchall()
        hashes = {r['lyrics_hash'] for r in own if r['lyrics_hash']}
        by_title = {}
        for row in own:
            by_title.setdefault(row['normalized_title'], []).append({'id': row['id'], 'title': row['title']})
        groups = {}
        for row in self.fresh_rows(run_id):
            if not row['complete'] or row['lyrics_hash'] in hashes:
                continue
            record = groups.setdefault(row['lyrics_hash'], {'id': row['id'], 'title': row['title'], 'author': row['author'],
                'lyrics_hash': row['lyrics_hash'], 'summary': row['summary'], 'topics': json.loads(row['topics']),
                'analysis_needed': not bool(row['summary']) or row['analysis_version'] != ANALYSIS_VERSION,
                'possible_library_matches': [], 'sources': []})
            if not record['summary'] and row['summary']:
                record.update(summary=row['summary'], topics=json.loads(row['topics']), analysis_needed=row['analysis_version'] != ANALYSIS_VERSION)
            for match in by_title.get(normalized(row['title']), []):
                if match not in record['possible_library_matches']:
                    record['possible_library_matches'].append(match)
            record['sources'].append({'song_id': row['id'], 'name': row['source_name'], 'title': row['title'],
                'url': row['document_url'], 'links': json.loads(row['links'])})
        rows = sorted(groups.values(), key=lambda r: (r['title'].casefold(), r['id']))
        if query:
            tokens = normalized(query).split()
            ranked = []
            for row in rows:
                haystack = normalized(' '.join([row['title'], row['summary'] or '', *row['topics']]))
                score = sum(t in haystack for t in tokens)
                if score:
                    ranked.append((score, row))
            rows = [r for _, r in sorted(ranked, key=lambda pair: (-pair[0], pair[1]['id']))]
        total = len(rows); stop = offset + limit
        return {**status, 'songs': rows[offset:stop], 'total': total,
                'next_offset': stop if stop < total else None,
                'lexical_filter_only': bool(query),
                'library_check_ready': status['library_complete'] and status['library_missing_lyrics'] == 0}

    def get(self, run_id, song_id):
        for row in self.fresh_rows(run_id, song_id=song_id, include_lyrics=True):
            if row['id'] == song_id:
                result = dict(row); result['topics'] = json.loads(result['topics']); result['links'] = json.loads(result['links'])
                return result
        raise ValueError('Song is not verified against the inventory of this run')


def read_json(path):
    if path == '-':
        return json.load(sys.stdin)
    with open(path, encoding='utf-8') as stream:
        return json.load(stream)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, help='Override the per-user database path')
    parser.add_argument('--profile', default=DEFAULT_PROFILE, help='Namespace for a library and source registry')
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('start', help='Start a run with fresh source/library snapshots')
    start.add_argument('--input', required=True)
    for name in ('plan', 'put', 'fail'):
        command = commands.add_parser(name)
        command.add_argument('--run', required=True); command.add_argument('--source', required=True)
        if name != 'plan':
            command.add_argument('--item', required=True)
        if name != 'fail':
            command.add_argument('--input', required=True)
        else:
            command.add_argument('--reason', required=True)
    analysis = commands.add_parser('analyze')
    analysis.add_argument('--song', required=True); analysis.add_argument('--input', required=True)
    catalog = commands.add_parser('catalog', help='Compact meaning summaries; no keyword filter by default')
    catalog.add_argument('--run', required=True); catalog.add_argument('--query')
    catalog.add_argument('--limit', type=int, default=100); catalog.add_argument('--offset', type=int, default=0)
    status = commands.add_parser('status'); status.add_argument('--run', required=True)
    get = commands.add_parser('get'); get.add_argument('--run', required=True); get.add_argument('--song', required=True)
    args = parser.parse_args(argv)
    index = None
    try:
        index = Index(args.db or default_path(args.profile), args.profile)
        if args.command == 'start': result = index.start(read_json(args.input))
        elif args.command == 'plan': result = index.plan(args.run, args.source, read_json(args.input))
        elif args.command == 'put': result = index.put(args.run, args.source, args.item, read_json(args.input))
        elif args.command == 'fail': result = index.fail(args.run, args.source, args.item, args.reason)
        elif args.command == 'analyze': result = index.analyze(args.song, read_json(args.input))
        elif args.command == 'catalog': result = index.catalog(args.run, args.query, args.limit, args.offset)
        elif args.command == 'status': result = index.status(args.run)
        else: result = index.get(args.run, args.song)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, sqlite3.Error, TypeError, KeyError, AttributeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if index is not None:
            index.close()


if __name__ == '__main__':
    raise SystemExit(main())
