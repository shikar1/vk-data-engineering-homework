import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from mapreduce import S3


def dump_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def measured_process(command, log_path):
    with log_path.open("w", encoding="utf-8") as log:
        started = time.perf_counter()
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        process_id, status, usage = os.wait4(process.pid, 0)
        seconds = time.perf_counter() - started
        process.returncode = os.waitstatus_to_exitcode(status)
    if process_id != process.pid:
        raise ValueError("Unexpected child process")
    return {"exit_code": process.returncode, "wall_seconds": seconds,
            "peak_rss_mib": usage.ru_maxrss / 1024,
            "user_cpu_seconds": usage.ru_utime, "system_cpu_seconds": usage.ru_stime}


def source_identity(client):
    with client.request("HEAD", "hh-lake", "it_vacancies_2024.csv") as response:
        return {name: response.headers.get(name) for name in ("ETag", "Content-Length", "Last-Modified")}


def verify_tables(directory, reference):
    for filename, metadata in reference["files"].items():
        digest = hashlib.sha256((directory / filename).read_bytes()).hexdigest()
        if digest != metadata["sha256"]:
            raise ValueError(f"Full table SHA256 mismatch: {directory.name}/{filename}")


def fault_test(client, output, prefix, reference):
    failed_output = output / "fault-failed"
    failed_prefix = prefix + "/fault-failed"
    command = [sys.executable, "-u", "/app/mapreduce.py", "--output-dir", str(failed_output), "--output-prefix", failed_prefix, "--fail-after-matches", "100"]
    failure = measured_process(command, output / "fault-failed.log")
    message = (output / "fault-failed.log").read_text(encoding="utf-8")
    if failure["exit_code"] == 0 or "INJECTED_MAPPER_FAILURE" not in message:
        raise ValueError("Mapper failure was not observed")
    if failed_output.exists() and any(failed_output.iterdir()):
        raise ValueError("Failed mapper produced local results")
    for filename in ("skills.csv", "pairs.csv", "triples.csv", "manifest.json"):
        try:
            with client.request("HEAD", "hh-lake", failed_prefix + "/" + filename):
                pass
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        else:
            raise ValueError("Failed mapper published an S3 result")
    print("FAULT INJECTION: expected nonzero exit; no CSV/manifest locally or in S3", flush=True)
    recovered_output = output / "fault-recovered"
    recovered_prefix = prefix + "/fault-recovered"
    command = [sys.executable, "-u", "/app/mapreduce.py", "--output-dir", str(recovered_output), "--output-prefix", recovered_prefix]
    recovery = measured_process(command, output / "fault-recovered.log")
    if recovery["exit_code"] != 0:
        raise ValueError("Mapper recovery run failed; inspect fault-recovered.log")
    verify_tables(recovered_output, reference)
    return {"injected_after_matching_row": 100, "failure": failure, "failed_outputs_absent": True,
            "recovery": recovery, "recovery_all_tables_equal": True,
            "recovery_s3_read_back_passed": "VERIFIED: all output objects downloaded and checked" in (output / "fault-recovered.log").read_text(encoding="utf-8"),
            "recovery_policy": "Manual full restart from the beginning; no automatic retry/checkpoint or distributed worker replacement", "recovery_prefix": recovered_prefix}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--reference-prefix", default="mapreduce/devops/20261021T135359197166Z")
    args = parser.parse_args()
    if args.attempts < 3:
        parser.error("At least three attempts are required")
    output = Path("/output")
    client = S3()
    identity = source_identity(client)
    reference_key = args.reference_prefix.rstrip("/") + "/manifest.json"
    with client.request("GET", "hh-lake", reference_key) as response:
        reference = json.load(response)
    (output / "reference-manifest.json").write_text(json.dumps(reference, ensure_ascii=False, indent=2), encoding="utf-8")
    prefix = "benchmark/devops/" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    runs = []
    for attempt in range(1, args.attempts + 1):
        methods = ("mapreduce", "duckdb") if attempt % 2 else ("duckdb", "mapreduce")
        for method in methods:
            if source_identity(client) != identity:
                raise ValueError("Source object changed during benchmark")
            directory = output / f"{method}-{attempt}"
            print(f"START: {method}, attempt {attempt}/{args.attempts}", flush=True)
            metrics = measured_process([sys.executable, "-u", "/app/benchmark_worker.py", method, "--output-dir", str(directory)], output / f"{method}-{attempt}.log")
            if metrics["exit_code"] != 0:
                raise ValueError(f"Worker failed: {method}-{attempt}; inspect its log")
            verify_tables(directory, reference)
            run = {"method": method, "attempt": attempt, **metrics, "all_tables_equal": True}
            runs.append(run)
            dump_csv(output / "runs.csv", runs)
            print(f"DONE: {method} #{attempt}: {metrics['wall_seconds']:.3f} s, peak RSS {metrics['peak_rss_mib']:.2f} MiB, all table hashes equal", flush=True)
    summary = []
    for method in ("mapreduce", "duckdb"):
        records = [row for row in runs if row["method"] == method]
        times = [row["wall_seconds"] for row in records]
        peaks = [row["peak_rss_mib"] for row in records]
        summary.append({"method": method, "attempts": len(records),
                        "median_wall_seconds": statistics.median(times), "mean_wall_seconds": statistics.mean(times),
                        "min_wall_seconds": min(times), "max_wall_seconds": max(times),
                        "stdev_wall_seconds": statistics.stdev(times),
                        "median_peak_rss_mib": statistics.median(peaks), "max_peak_rss_mib": max(peaks)})
    dump_csv(output / "summary.csv", summary)
    print("BENCHMARK SERIES COMPLETE; testing mapper failure and recovery", flush=True)
    failure = fault_test(client, output, prefix, reference)
    if not failure["recovery_s3_read_back_passed"] or source_identity(client) != identity:
        raise ValueError("Recovery verification failed or source changed")
    (output / "fault-test.json").write_text(json.dumps(failure, indent=2), encoding="utf-8")
    configuration = {
        "python": platform.python_version(), "duckdb": importlib.metadata.version("duckdb"),
        "platform": platform.platform(), "machine": platform.machine(),
        "container_cpu_quota": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
        "container_memory_limit_bytes": Path("/sys/fs/cgroup/memory.max").read_text().strip(),
        "source_identity_before_and_after": identity, "attempts_per_method": args.attempts,
        "timing_scope": "Fresh child Python process creation through termination: imports, connection setup, S3 read, normalization, 1/2/3-skill aggregation, sorting and local canonical CSV export. Same scope for both methods.",
        "memory_scope": "Exact child lifetime peak RSS from Linux wait4 rusage, MiB. Includes runtime, libraries and CSV buffers; excludes controller and RustFS server. Not whole-machine or GPU memory.",
        "excluded": "Docker startup/image build, parent reference validation, S3 result upload, read-back verification and fault/recovery tests are outside the six timed benchmark runs.",
        "parallelism": "Container allowance 2 CPUs, 2 GiB RAM. DuckDB threads=2 and memory_limit=1GB; CSV parallel=false. MapReduce is single-process and single-threaded. This compares these implementations, not engines at identical internal parallelism.",
        "cache_policy": "No OS/RustFS cache clearing or discarded warm-up. Alternating method order across rounds; each run is a fresh process in the same container. Earlier lab runs already read the source; no cold-cache performance claim.",
        "verification": "All three complete sorted CSV SHA256 hashes match the verified MapReduce manifest on every measured run.",
        "s3_prefix": prefix,
    }
    (output / "configuration.json").write_text(json.dumps(configuration, indent=2), encoding="utf-8")
    mapreduce_summary, duckdb_summary = summary
    faster = min(summary, key=lambda row: row["median_wall_seconds"])
    slower = max(summary, key=lambda row: row["median_wall_seconds"])
    ratio = slower["median_wall_seconds"] / faster["median_wall_seconds"]
    lines = ["# Профилирование MapReduce и DuckDB", "",
             "Границы замера одинаковы: от создания свежего Python-процесса до его завершения. Включены импорты, подключение, чтение S3, нормализация, расчёт всех трёх размеров стека, сортировка и локальная запись CSV. Загрузка в S3 и сверка результатов выполняются вне замера.", "",
             "Пик памяти — RSS дочернего процесса за всю его жизнь по Linux wait4, в МиБ. Память RustFS и управляющего процесса не включается. Контейнер: 2 CPU, 2 ГиБ RAM; DuckDB использует до 2 потоков, MapReduce — один.", "",
             "| Метод | Попытка | Время, с | Пик RSS, МиБ | Полное совпадение |", "|---|---:|---:|---:|---|"]
    for row in runs:
        lines.append(f"| {row['method']} | {row['attempt']} | {row['wall_seconds']:.3f} | {row['peak_rss_mib']:.2f} | да |")
    lines.extend(["", "## Сводка", "", "| Метод | Медиана времени, с | Мин–макс, с | Медиана пика RSS, МиБ | Максимальный пик, МиБ |", "|---|---:|---:|---:|---:|"])
    for row in summary:
        lines.append(f"| {row['method']} | {row['median_wall_seconds']:.3f} | {row['min_wall_seconds']:.3f}–{row['max_wall_seconds']:.3f} | {row['median_peak_rss_mib']:.2f} | {row['max_peak_rss_mib']:.2f} |")
    lines.extend(["", f"В этой серии {faster['method']} быстрее {slower['method']} по медиане времени примерно в {ratio:.2f} раза. Это сравнение конкретных реализаций и условий запуска.", "",
                  "MapReduce потоково разбирает CSV и хранит счётчики сочетаний. DuckDB использует собственные CSV-буферы, SQL-операторы и соединения. Эти различия могут объяснять расход памяти; отдельной трассировки аллокаций здесь нет. Разницу времени нельзя приписать только языку или только многопоточности.", "",
                  "Кеши ОС и RustFS не очищались, порядок методов чередовался; предыдущие этапы уже читали CSV. Это повторные замеры в текущем окружении, а не тест холодного хранилища. Три попытки показывают разброс, но не дают строгого доказательства превосходства во всех условиях.", "",
                  "## Отказ маппера", "",
                  "В реальный mapper внесена управляемая ошибка после 100-й подходящей вакансии. Процесс завершился с ненулевым кодом и меткой INJECTED_MAPPER_FAILURE. CSV и manifest отсутствовали как локально, так и в специально выделенном S3-префиксе (HEAD вернул 404).", "",
                  "Затем выполнен полный повторный запуск без ошибки: все три таблицы совпали с эталоном и были сохранены и перечитаны из S3. Это проверка корректного прерывания и ручного перезапуска однопроцессной реализации. Автоматическое восстановление распределённого кластера и контрольные точки не реализованы. Ошибка и восстановление не входят в сравнительные замеры.", "",
                  "Файлы runs.csv, summary.csv, configuration.json и fault-test.json содержат исходные измерения и настройки. Индивидуальные логи и CSV всех попыток сохранены рядом."])
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    uploaded = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "run.log":
            continue
        payload = path.read_bytes()
        key = prefix + "/" + path.relative_to(output).as_posix()
        with client.request("PUT", "hh-lake", key, payload):
            pass
        with client.request("GET", "hh-lake", key) as response:
            if response.read() != payload:
                raise ValueError(f"Benchmark read-back mismatch: {path.name}")
        uploaded[path.relative_to(output).as_posix()] = {"key": key, "sha256": hashlib.sha256(payload).hexdigest(), "read_back_verified": True}
    completion = json.dumps({"complete": True, "files": uploaded}, indent=2).encode("utf-8")
    (output / "completion.json").write_bytes(completion)
    with client.request("PUT", "hh-lake", prefix + "/completion.json", completion):
        pass
    with client.request("GET", "hh-lake", prefix + "/completion.json") as response:
        if response.read() != completion:
            raise ValueError("Benchmark completion marker mismatch")
    print("\nBENCHMARK SUMMARY:", json.dumps(summary, indent=2), flush=True)
    print(f"BENCHMARK: PASS\nMAPPER_FAILURE_AND_RECOVERY: PASS\nRESULT: s3://hh-lake/{prefix}/", flush=True)


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
