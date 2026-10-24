"""Real ZooKeeper experiments. No synthetic success logs or expected-result copying."""
import argparse
import csv
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import socket
import time
import traceback
import urllib.error
from collections import Counter

from kazoo.client import KazooClient
from kazoo.exceptions import BadVersionError, NodeExistsError

WORK = Path('/work')
LOGS = Path('/logs')
csv.field_size_limit(16 * 1024 * 1024)


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


def event(run, who, kind, **fields):
    value = dict(time=time.time(), who=who, event=kind, **fields)
    line = json.dumps(value, ensure_ascii=False)
    print(line, flush=True)
    LOGS.mkdir(exist_ok=True)
    with (LOGS / f'{run}-{who}.jsonl').open('a', encoding='utf-8') as out:
        out.write(line + '\n')


def client():
    zk = KazooClient(hosts=os.getenv('ZK_HOSTS', 'zk1:2181,zk2:2181,zk3:2181'), timeout=10)
    zk.start(timeout=20)
    return zk


def checked(transaction):
    results = transaction.commit()
    # A failed multi returns exceptions in its result list, rather than raising.
    for result in results:
        if isinstance(result, BadVersionError):
            raise result
    for result in results:
        if isinstance(result, Exception):
            raise result
    return results


def previous():
    spec = importlib.util.spec_from_file_location('previous_mapreduce', '/previous/mapreduce.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare():
    """Download the original source once; never derive input from reference results."""
    module = previous()
    WORK.mkdir(exist_ok=True)
    target = WORK / 'source.csv'
    part = WORK / 'source.csv.part'
    source = module.S3()
    for attempt in range(30):
        try:
            response = source.request('GET', 'hh-lake', 'it_vacancies_2024.csv')
            break
        except urllib.error.HTTPError as error:
            detail = error.read(4096).decode('utf-8', errors='replace')
            error.close()
            print(f'RustFS HTTP {error.code}: {detail}', flush=True)
            # RustFS can accept connections before the storage quorum is ready.
            # Retry only availability errors, never hide auth or missing objects.
            if error.code not in (502, 503, 504) or attempt == 29:
                raise
            print(f'Waiting for RustFS storage readiness: attempt {attempt + 1}/30', flush=True)
            time.sleep(2)
        except urllib.error.URLError:
            if attempt == 29:
                raise
            print(f'Waiting for RustFS at {source.endpoint}: attempt {attempt + 1}/30', flush=True)
            time.sleep(2)
    with response as src, part.open('wb') as dst:
        while block := src.read(1024 * 1024):
            dst.write(block)
    part.replace(target)
    hasher = hashlib.sha256()
    with target.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            hasher.update(block)
    digest = hasher.hexdigest()
    (WORK / 'source.json').write_bytes(encode({'sha256': digest, 'bytes': target.stat().st_size}))
    print('SOURCE_READY', digest, target.stat().st_size, flush=True)


def init(run):
    zk = client()
    try:
        root = '/homework/' + run
        zk.ensure_path('/homework')
        # Unique run ids: never reset a live epoch or erase earlier evidence.
        zk.create(root)
        for name, data in [('epoch', b'0'), ('checkpoint', b'{}'), ('result', b'{}')]:
            zk.create(root + '/' + name, data)
    finally:
        zk.stop()
        zk.close()


def coordinate(run, who, fencing):
    root = '/homework/' + run
    zk = client()
    zk.add_listener(lambda state: event(run, who, 'SESSION', state=state))
    module = previous()
    try:
        deadline = time.monotonic() + 240
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('Leader acquisition timeout')
            _, stat = zk.get(root + '/epoch')
            tx = zk.transaction()
            tx.create(root + '/leader', who.encode(), ephemeral=True)
            tx.set_data(root + '/epoch', str(stat.version + 1).encode(), version=stat.version)
            try:
                checked(tx)
                epoch = stat.version + 1
                break
            except (NodeExistsError, BadVersionError):
                time.sleep(0.2)
        event(run, who, 'LEADER', epoch=epoch, session_timeout_seconds=10, fencing=fencing)
        cp = json.loads(zk.get(root + '/checkpoint')[0])
        counts = Counter()
        offset = matched = empty = 0
        source_meta = json.loads((WORK / 'source.json').read_text())
        if cp:
            saved = json.loads((WORK / cp['file']).read_text(encoding='utf-8'))
            if saved['source'] != source_meta:
                raise ValueError('Checkpoint source mismatch')
            offset, matched, empty = saved['offset'], saved['matched'], saved['empty']
            counts.update({tuple(pair): count for pair, count in saved['counts']})
        event(run, who, 'RESUME', offset=offset, pairs=len(counts))
        total = offset
        with (WORK / 'source.csv').open(encoding='utf-8-sig', newline='') as stream:
            first = stream.readline()
            delimiter = csv.Sniffer().sniff(first, delimiters=',;\t|').delimiter
            rows = csv.DictReader(itertools.chain([first], stream), delimiter=delimiter)
            for total, row in enumerate(rows, 1):
                if total <= offset:
                    continue
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f'Malformed CSV record {total}')
                if (row['role'] or '').strip().casefold() == 'DevOps-инженер'.casefold():
                    matched += 1
                    skills = module.parse_skills(row['key_skills'])
                    empty += not skills
                    counts.update(itertools.combinations(skills, 2))
                if total % 10000 == 0:
                    name = f'{run}-{who}-checkpoint-{total}.json'
                    snapshot = dict(offset=total, matched=matched, empty=empty,
                                    source=source_meta, counts=list(counts.items()))
                    # Immutable data first, then atomic fenced pointer publication.
                    (WORK / name).write_bytes(encode(snapshot))
                    def publish_checkpoint():
                        tx = zk.transaction()
                        tx.check(root + '/epoch', epoch)
                        tx.set_data(root + '/checkpoint', encode({'file': name, 'offset': total}))
                        return checked(tx)
                    try:
                        # Rebuild each transaction if reconnection is needed after pause.
                        # Repeating the same immutable checkpoint pointer is idempotent.
                        zk.retry(publish_checkpoint)
                        event(run, who, 'CHECKPOINT', offset=total)
                    except BadVersionError:
                        # Deliberately keep stale computation alive for the experiment.
                        # It cannot corrupt the current leader's checkpoint.
                        event(run, who, 'STALE_CHECKPOINT_REJECTED', offset=total)
                    time.sleep(1)
        payload = module.table_bytes(counts, 2)
        digest = hashlib.sha256(payload).hexdigest()
        reference_dir = Path('/previous/results-20261021-165303-339')
        manifest = json.loads((reference_dir / 'manifest.json').read_text(encoding='utf-8'))
        expected = manifest['files']['pairs.csv']
        actual = (total, matched, empty, len(counts), sum(counts.values()), digest)
        wanted = (manifest['total'], manifest['matched'], manifest['without_skills'],
                  expected['rows'], expected['occurrences'], expected['sha256'])
        if actual != wanted or payload != (reference_dir / 'pairs.csv').read_bytes():
            raise AssertionError(f'Result differs from HW3: {actual} != {wanted}')
        # Each writer has its own immutable candidate, not a shared mutable CSV.
        filename = f'{run}-{who}-pairs.csv'
        (LOGS / filename).write_bytes(payload)
        result = dict(who=who, epoch=epoch, file=filename, sha256=digest,
                      total=total, matched=matched, empty=empty,
                      unique_pairs=len(counts), occurrences=sum(counts.values()))
        event(run, who, 'VERIFIED_HW3', **{k: v for k, v in result.items() if k != 'who'})
        try:
            if fencing:
                # The result receiver is ZooKeeper itself in this implementation.
                # Check + result update are ONE atomic transaction at that receiver.
                tx = zk.transaction()
                tx.check(root + '/epoch', epoch)
                tx.set_data(root + '/result', encode(result))
                checked(tx)
            else:
                zk.set(root + '/result', encode(result))
            stat = zk.get(root + '/result')[1]
            event(run, who, 'WROTE_RESULT', message='записал результат', version=stat.version, epoch=epoch)
        except BadVersionError:
            event(run, who, 'BadVersionError', message='запись устаревшего лидера отклонена', epoch=epoch)
    finally:
        zk.stop()
        zk.close()


def probe(expect):
    for host in ['zk1', 'zk2', 'zk3']:
        try:
            with socket.create_connection((host, 2181), timeout=2) as sock:
                sock.sendall(b'srvr')
                sock.settimeout(2)
                chunks = []
                while chunk := sock.recv(8192):
                    chunks.append(chunk)
                print(host, b''.join(chunks).decode(), flush=True)
        except OSError as error:
            print(host, type(error).__name__, str(error), flush=True)
    zk = KazooClient(hosts=os.getenv('ZK_HOSTS'), timeout=10)
    success = False
    try:
        zk.start(timeout=8)
        zk.create('/quorum-test-', b'ok', ephemeral=True, sequence=True)
        success = True
        print('QUORUM_WRITE_OK', flush=True)
    except Exception as error:
        print('QUORUM_UNAVAILABLE', type(error).__name__, flush=True)
    finally:
        zk.stop()
        zk.close()
    if success != (expect == 'up'):
        raise AssertionError(f'Expected quorum {expect}; write success={success}')


def inspect(run, fencing):
    zk = client()
    try:
        pointer, stat = zk.get('/homework/' + run + '/result')
        value = json.loads(pointer)
        events = []
        for who in ['a', 'b']:
            events.extend(json.loads(line) for line in (LOGS / f'{run}-{who}.jsonl').read_text(encoding='utf-8').splitlines())
        writes = [e for e in events if e['event'] == 'WROTE_RESULT']
        rejected = [e for e in events if e['event'] == 'BadVersionError']
        resumed = [e for e in events if e['who'] == 'b' and e['event'] == 'RESUME']
        verified = [e for e in events if e['event'] == 'VERIFIED_HW3']
        assert len(writes) == (1 if fencing else 2), writes
        assert stat.version == (1 if fencing else 2), stat
        assert len(rejected) == (1 if fencing else 0), rejected
        assert value['who'] == ('b' if fencing else 'a'), value
        assert resumed and resumed[0]['offset'] > 0, resumed
        assert len(verified) == 2 and len({e['sha256'] for e in verified}) == 1
        timing = json.loads((LOGS / f'{run}-pause.json').read_text(encoding='utf-8-sig'))
        promoted = next(e['time'] for e in events if e['who'] == 'b' and e['event'] == 'LEADER')
        summary = dict(run=run, fencing=fencing, result=value, version=stat.version,
                       writes=len(writes), rejections=len(rejected), checkpoint_offset=resumed[0]['offset'],
                       failover_seconds=promoted-timing['pause_completed'],
                       pause_command_seconds=timing['pause_completed']-timing['pause_started'],
                       note='Failover measured from successful docker pause return; command duration reported separately.',
                       status='PASS')
        (LOGS / f'{run}-summary.json').write_bytes(encode(summary))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        zk.stop()
        zk.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'init', 'coordinate', 'probe', 'inspect'])
    parser.add_argument('--run')
    parser.add_argument('--who', default='a')
    parser.add_argument('--fencing', type=int, choices=[0, 1], default=int(os.getenv('FENCING', '0')))
    parser.add_argument('--expect', choices=['up', 'down'], default='up')
    args = parser.parse_args()
    if args.command in ['init', 'coordinate', 'inspect'] and not args.run:
        parser.error('--run required')
    try:
        if args.command == 'prepare': prepare()
        elif args.command == 'init': init(args.run)
        elif args.command == 'coordinate': coordinate(args.run, args.who, args.fencing)
        elif args.command == 'probe': probe(args.expect)
        else: inspect(args.run, args.fencing)
    except Exception:
        LOGS.mkdir(exist_ok=True)
        error_path = LOGS / f'{time.time_ns()}-{args.command}-error.txt'
        error_path.write_text(traceback.format_exc(), encoding='utf-8')
        raise
