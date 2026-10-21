import argparse
import ast
import csv
import hashlib
import hmac
import io
import itertools
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


class S3:
    def __init__(self):
        self.endpoint = os.environ.get("S3_ENDPOINT", "http://host.docker.internal:9000").rstrip("/")
        self.access = os.environ["RUSTFS_ACCESS_KEY"]
        self.secret = os.environ["RUSTFS_SECRET_KEY"]

    def request(self, method, bucket, key, data=None):
        path = "/" + urllib.parse.quote(bucket, safe="") + "/" + urllib.parse.quote(key, safe="/~")
        host = urllib.parse.urlsplit(self.endpoint).netloc
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        day = stamp[:8]
        digest = hashlib.sha256(data if data is not None else b"").hexdigest()
        headers = {"host": host, "x-amz-content-sha256": digest, "x-amz-date": stamp}
        names = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        canonical = "\n".join([method, path, "", canonical_headers, names, digest])
        scope = f"{day}/us-east-1/s3/aws4_request"
        message = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        signing_key = ("AWS4" + self.secret).encode()
        for component in [day, "us-east-1", "s3", "aws4_request"]:
            signing_key = hmac.new(signing_key, component.encode(), hashlib.sha256).digest()
        signature = hmac.new(signing_key, message.encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={self.access}/{scope}, SignedHeaders={names}, Signature={signature}"
        request = urllib.request.Request(self.endpoint + path, data=data, headers=headers, method=method)
        return urllib.request.urlopen(request, timeout=120)


def source_rows(client, bucket, key):
    response = client.request("GET", bucket, key)
    stream = io.TextIOWrapper(response, encoding="utf-8-sig", newline="")
    first_line = stream.readline()
    delimiter = csv.Sniffer().sniff(first_line, delimiters=",;\t|").delimiter
    return stream, csv.DictReader(itertools.chain([first_line], stream), delimiter=delimiter)


def parse_skills(value):
    value = (value or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            skills = json.loads(value)
        except json.JSONDecodeError:
            skills = ast.literal_eval(value)
        if not isinstance(skills, list) or not all(isinstance(skill, str) for skill in skills):
            raise ValueError("Expected a list of skill strings")
    else:
        skills = value.split(";")
    return sorted({" ".join(skill.split()).casefold() for skill in skills if skill.strip()})


def mapper(skills, fail=False):
    if fail:
        raise RuntimeError("INJECTED_MAPPER_FAILURE")
    for size in (1, 2, 3):
        for combination in itertools.combinations(skills, size):
            yield size, combination


def table_bytes(counts, size):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([f"skill_{number}" for number in range(1, size + 1)] + ["count"])
    for combination, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        writer.writerow([*combination, count])
    return buffer.getvalue().encode("utf-8-sig")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--bucket", default="hh-lake")
    parser.add_argument("--key", default="it_vacancies_2024.csv")
    parser.add_argument("--role", default="DevOps-инженер")
    parser.add_argument("--role-column", default="role")
    parser.add_argument("--skills-column", default="key_skills")
    parser.add_argument("--expected-total", type=int, default=229388)
    parser.add_argument("--expected-matched", type=int, default=5158)
    parser.add_argument("--fail-after-matches", type=int, default=0)
    parser.add_argument("--output-dir", default="/output")
    parser.add_argument("--output-prefix")
    args = parser.parse_args()
    if args.fail_after_matches < 0:
        parser.error("--fail-after-matches must be nonnegative")
    csv.field_size_limit(16 * 1024 * 1024)
    client = S3()
    stream, rows = source_rows(client, args.bucket, args.key)
    with stream:
        if args.audit:
            width = len(rows.fieldnames)
            records = blank = malformed = 0
            for record in rows.reader:
                records += 1
                blank += not record
                malformed += bool(record) and len(record) != width
            print(json.dumps({"csv_records_after_header": records, "empty_records": blank, "nonempty_records": records - blank, "wrong_width_records": malformed, "physical_lines": rows.reader.line_num}, indent=2))
            return
        if args.inspect:
            print("COLUMNS:", json.dumps(rows.fieldnames, ensure_ascii=False))
            for row in itertools.islice(rows, 3):
                print(json.dumps({name: value[:500] for name, value in row.items() if name and ("skill" in name.lower() or "role" in name.lower())}, ensure_ascii=False))
            return
        for column in (args.role_column, args.skills_column):
            if column not in rows.fieldnames:
                raise ValueError(f"Missing column {column!r}; available: {rows.fieldnames}")
        counters = {size: Counter() for size in (1, 2, 3)}
        emitted = Counter()
        total = matched = empty = 0
        for row in rows:
            total += 1
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed CSV record: {total}")
            if total % 50000 == 0:
                print(f"PROGRESS: {total} records; {matched} matched", flush=True)
            if (row[args.role_column] or "").strip().casefold() != args.role.casefold():
                continue
            matched += 1
            skills = parse_skills(row[args.skills_column])
            empty += not skills
            for size, combination in mapper(skills, fail=args.fail_after_matches > 0 and matched >= args.fail_after_matches):
                counters[size][combination] += 1
            for size in (1, 2, 3):
                emitted[size] += math.comb(len(skills), size)
    if (total, matched) != (args.expected_total, args.expected_matched):
        raise ValueError(f"Source check failed: total={total}, matched={matched}; expected {args.expected_total}, {args.expected_matched}")
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    prefix = args.output_prefix or "mapreduce/devops/" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    manifest = {"source": f"s3://{args.bucket}/{args.key}", "role": args.role, "total": total, "matched": matched, "without_skills": empty, "normalization": "whitespace collapsed, casefold, unique skills per vacancy; unordered combinations", "files": {}}
    for size, filename in ((1, "skills.csv"), (2, "pairs.csv"), (3, "triples.csv")):
        counts = counters[size]
        if sum(counts.values()) != emitted[size] or any(count > matched for count in counts.values()):
            raise ValueError(f"Map/Reduce invariant failed for size {size}")
        payload = table_bytes(counts, size)
        (output / filename).write_bytes(payload)
        object_key = f"{prefix}/{filename}"
        with client.request("PUT", args.bucket, object_key, payload):
            pass
        with client.request("GET", args.bucket, object_key) as response:
            restored = response.read()
        if restored != payload:
            raise ValueError(f"S3 read-back mismatch: {filename}")
        restored_rows = list(csv.DictReader(io.StringIO(restored.decode("utf-8-sig"))))
        restored_counts = {tuple(row[f"skill_{number}"] for number in range(1, size + 1)): int(row["count"]) for row in restored_rows}
        if restored_counts != dict(counts) or len(restored_rows) != len(counts):
            raise ValueError(f"CSV round-trip mismatch: {filename}")
        manifest["files"][filename] = {"key": object_key, "rows": len(counts), "occurrences": emitted[size], "sha256": hashlib.sha256(payload).hexdigest(), "read_back_verified": True}
        print(f"\n{filename}: {len(counts)} rows; {emitted[size]} occurrences; S3 read-back OK")
        for combination, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]:
            print(f"  {' + '.join(combination)}: {count}")
    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    (output / "manifest.json").write_bytes(payload)
    with client.request("PUT", args.bucket, f"{prefix}/manifest.json", payload):
        pass
    with client.request("GET", args.bucket, f"{prefix}/manifest.json") as response:
        if response.read() != payload:
            raise ValueError("Manifest read-back mismatch")
    print(f"\nTOTAL: {total}\nMATCHED: {matched}\nWITHOUT_SKILLS: {empty}\nRESULT: s3://{args.bucket}/{prefix}/\nVERIFIED: all output objects downloaded and checked")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message = f"FAILED: {type(error).__name__}: {error}"
        for name in ("RUSTFS_ACCESS_KEY", "RUSTFS_SECRET_KEY"):
            secret = os.environ.get(name)
            if secret:
                message = message.replace(secret, "[REDACTED]")
        print(message, file=sys.stderr)
        sys.exit(1)
