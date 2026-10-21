import argparse
import csv
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path

from mapreduce import S3, mapper, parse_skills, source_rows, table_bytes


def calculate_mapreduce(output):
    csv.field_size_limit(16 * 1024 * 1024)
    client = S3()
    stream, rows = source_rows(client, "hh-lake", "it_vacancies_2024.csv")
    counts = {size: Counter() for size in (1, 2, 3)}
    emitted = Counter()
    total = matched = empty = 0
    with stream:
        for row in rows:
            total += 1
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed CSV record: {total}")
            if (row["role"] or "").strip().casefold() != "devops-инженер":
                continue
            matched += 1
            skills = parse_skills(row["key_skills"])
            empty += not skills
            for size, combination in mapper(skills):
                counts[size][combination] += 1
            for size in (1, 2, 3):
                emitted[size] += math.comb(len(skills), size)
    tables = {}
    for size, name in ((1, "skills"), (2, "pairs"), (3, "triples")):
        if sum(counts[size].values()) != emitted[size]:
            raise ValueError("MapReduce combination invariant failed")
        (output / (name + ".csv")).write_bytes(table_bytes(counts[size], size))
        tables[name] = {"rows": len(counts[size]), "occurrences": emitted[size]}
    return {"total": total, "matched": matched, "without_skills": empty, "tables": tables}


def calculate_duckdb(output):
    import duckdb

    from duckdb_run import write_csv

    connection = duckdb.connect(config={"threads": 2, "memory_limit": "1GB"})
    connection.execute("LOAD httpfs")
    for name, value in {
        "s3_access_key_id": os.environ["RUSTFS_ACCESS_KEY"],
        "s3_secret_access_key": os.environ["RUSTFS_SECRET_KEY"],
        "s3_region": "us-east-1", "s3_endpoint": "host.docker.internal:9000",
        "s3_url_style": "path", "s3_use_ssl": False,
    }.items():
        connection.execute(f"SET {name} = ?", [value])
    connection.execute(Path("/app/duckdb.sql").read_text(encoding="utf-8"))
    total = connection.execute("SELECT count(*) FROM raw").fetchone()[0]
    matched = connection.execute("SELECT count(*) FROM selected").fetchone()[0]
    empty = connection.execute("SELECT count(*) FROM selected WHERE row_id NOT IN (SELECT row_id FROM vacancy_skills)").fetchone()[0]
    tables = {}
    for size, name in ((1, "skills"), (2, "pairs"), (3, "triples")):
        columns = [f"skill_{number}" for number in range(1, size + 1)] + ["count"]
        ordering = ", ".join(columns[:-1])
        records = connection.execute(f"SELECT * FROM {name} ORDER BY count DESC, {ordering}").fetchall()
        write_csv(output / (name + ".csv"), columns, records)
        tables[name] = {"rows": len(records), "occurrences": sum(record[-1] for record in records)}
    connection.close()
    return {"total": total, "matched": matched, "without_skills": empty, "tables": tables}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("method", choices=["mapreduce", "duckdb"])
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True)
    result = calculate_mapreduce(output) if args.method == "mapreduce" else calculate_duckdb(output)
    if (result["total"], result["matched"], result["without_skills"]) != (229388, 5158, 1624):
        raise ValueError("Source control counts differ")
    (output / "counts.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("BENCHMARK_WORKER: PASS", flush=True)


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
