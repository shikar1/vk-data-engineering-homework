import argparse
import csv
import hashlib
import json
import os
import resource
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from mapreduce import S3


def sql_literal(value):
    return "'" + value.replace("'", "''") + "'"


def write_csv(path, columns, records):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(records)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-prefix", default="mapreduce/devops/20261021T135359197166Z")
    args = parser.parse_args()
    output = Path("/output")
    reference = output / "mapreduce-reference"
    reference.mkdir(parents=True)
    client = S3()
    with client.request("GET", "hh-lake", args.reference_prefix + "/manifest.json") as response:
        manifest_bytes = response.read()
    baseline = json.loads(manifest_bytes)
    (reference / "manifest.json").write_bytes(manifest_bytes)
    if baseline["source"] != "s3://hh-lake/it_vacancies_2024.csv" or baseline["role"] != "DevOps-инженер":
        raise ValueError("Reference source or role does not match the SQL query")
    connection = duckdb.connect(config={"threads": 2, "memory_limit": "1GB"})
    connection.execute("LOAD httpfs")
    settings = {
        "s3_access_key_id": os.environ["RUSTFS_ACCESS_KEY"],
        "s3_secret_access_key": os.environ["RUSTFS_SECRET_KEY"],
        "s3_region": "us-east-1",
        "s3_endpoint": "host.docker.internal:9000",
        "s3_url_style": "path",
        "s3_use_ssl": False,
    }
    for name, value in settings.items():
        connection.execute(f"SET {name} = ?", [value])
    sql = Path("/app/duckdb.sql").read_text(encoding="utf-8")
    print(f"DUCKDB: {duckdb.__version__}\nSOURCE: s3://hh-lake/it_vacancies_2024.csv", flush=True)
    started = time.perf_counter()
    connection.execute(sql)
    compute_seconds = time.perf_counter() - started
    total = connection.execute("SELECT count(*) FROM raw").fetchone()[0]
    matched = connection.execute("SELECT count(*) FROM selected").fetchone()[0]
    empty = connection.execute("SELECT count(*) FROM selected WHERE row_id NOT IN (SELECT row_id FROM vacancy_skills)").fetchone()[0]
    if (total, matched, empty) != (baseline["total"], baseline["matched"], baseline["without_skills"]):
        raise ValueError(f"Source counts differ: total={total}, matched={matched}, empty={empty}")
    report = {
        "duckdb_version": duckdb.__version__, "source": baseline["source"],
        "reference_prefix": args.reference_prefix, "total": total,
        "matched": matched, "without_skills": empty,
        "compute_seconds": compute_seconds,
        "normalization": "SQL lower + Unicode whitespace collapse; equivalence on this dataset checked against every MapReduce result",
        "tables": {},
    }
    prefix = "duckdb/devops/" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    all_equal = True
    for size, table in ((1, "skills"), (2, "pairs"), (3, "triples")):
        filename = table + ".csv"
        with client.request("GET", "hh-lake", baseline["files"][filename]["key"]) as response:
            original = response.read()
        if hashlib.sha256(original).hexdigest() != baseline["files"][filename]["sha256"]:
            raise ValueError(f"Reference hash mismatch: {filename}")
        original_path = reference / filename
        original_path.write_bytes(original)
        columns = [f"skill_{number}" for number in range(1, size + 1)] + ["count"]
        schema = {column: "BIGINT" if column == "count" else "VARCHAR" for column in columns}
        schema_sql = "{" + ", ".join(f"{sql_literal(column)}: {sql_literal(kind)}" for column, kind in schema.items()) + "}"
        connection.execute(f"CREATE TEMP TABLE reference_{table} AS SELECT * FROM read_csv({sql_literal(str(original_path))}, header = true, auto_detect = false, columns = {schema_sql})")
        differences = connection.execute(f"""
            SELECT 'duckdb_minus_mapreduce' AS direction, * FROM (
                SELECT * FROM {table} EXCEPT ALL SELECT * FROM reference_{table}
            )
            UNION ALL
            SELECT 'mapreduce_minus_duckdb' AS direction, * FROM (
                SELECT * FROM reference_{table} EXCEPT ALL SELECT * FROM {table}
            )
        """).fetchall()
        write_csv(output / (table + "-differences.csv"), ["direction", *columns], differences)
        ordered_columns = ", ".join(columns[:-1])
        records = connection.execute(f"SELECT * FROM {table} ORDER BY count DESC, {ordered_columns}").fetchall()
        write_csv(output / filename, columns, records)
        occurrences = sum(record[-1] for record in records)
        equal = not differences
        all_equal = all_equal and equal
        report["tables"][table] = {"rows": len(records), "occurrences": occurrences, "differences": len(differences), "equal": equal}
        print(f"\n{filename}: rows={len(records)}, occurrences={occurrences}, differences={len(differences)}, equal={equal}", flush=True)
        for record in records[:10]:
            print("  " + " + ".join(record[:-1]) + f": {record[-1]}")
    report["all_equal"] = all_equal
    report["process_peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    report["profiling_note"] = "Single diagnostic run; not the required three-run benchmark. Compute time covers SQL source read and aggregations; RSS covers the process through verification."
    (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not all_equal:
        raise ValueError("Full comparison failed; inspect differences CSV files")
    for filename in ("skills.csv", "pairs.csv", "triples.csv", "verification.json"):
        payload = (output / filename).read_bytes()
        with client.request("PUT", "hh-lake", prefix + "/" + filename, payload):
            pass
        with client.request("GET", "hh-lake", prefix + "/" + filename) as response:
            if response.read() != payload:
                raise ValueError(f"Output read-back mismatch: {filename}")
    print(f"\nTOTAL: {total}\nMATCHED: {matched}\nWITHOUT_SKILLS: {empty}\nFULL_COMPARISON: PASS\nRESULT: s3://hh-lake/{prefix}/\nSQL_SECONDS: {compute_seconds:.3f}\nPROCESS_PEAK_RSS_MIB: {report['process_peak_rss_mib']:.1f}", flush=True)
    connection.close()


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
