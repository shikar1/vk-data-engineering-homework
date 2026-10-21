import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from duckdb_run import write_csv
from mapreduce import S3
from regional_analysis import run_regions


def export_query(connection, output, filename, query):
    result = connection.execute(query)
    columns = [column[0] for column in result.description]
    records = result.fetchall()
    write_csv(output / filename, columns, records)
    return [dict(zip(columns, record)) for record in records]


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--regions", action="store_true")
    args = parser.parse_args()
    output = Path("/output")
    connection = duckdb.connect(config={"threads": 2, "memory_limit": "1GB"})
    connection.execute("LOAD httpfs")
    for name, value in {
        "s3_access_key_id": os.environ["RUSTFS_ACCESS_KEY"],
        "s3_secret_access_key": os.environ["RUSTFS_SECRET_KEY"],
        "s3_region": "us-east-1", "s3_endpoint": "host.docker.internal:9000",
        "s3_url_style": "path", "s3_use_ssl": False,
    }.items():
        connection.execute(f"SET {name} = ?", [value])
    print("SOURCE: s3://hh-lake/it_vacancies_2024.csv; ROLE: DevOps-инженер", flush=True)
    connection.execute(Path("/app/analysis-source.sql").read_text(encoding="utf-8"))
    total = connection.execute("SELECT count(*) FROM raw").fetchone()[0]
    matched = connection.execute("SELECT count(*) FROM selected").fetchone()[0]
    if (total, matched) != (229388, 5158):
        raise ValueError(f"Source counts changed: {total}, {matched}")
    profile = export_query(connection, output, "salary-profile.csv", """
        SELECT coalesce(currency, '<missing>') AS currency,
            coalesce(gross, '<missing>') AS gross, count(*) AS vacancies,
            count(*) FILTER (WHERE pay_from > 0 OR pay_to > 0) AS with_positive_salary,
            count(*) FILTER (WHERE pay_from > 0 AND pay_to > 0 AND pay_from > pay_to) AS reversed_ranges
        FROM selected GROUP BY currency, gross ORDER BY vacancies DESC
    """)
    areas = export_query(connection, output, "area-profile.csv", """
        SELECT coalesce(area, '<missing>') AS area, count(*) AS vacancies
        FROM selected GROUP BY area ORDER BY vacancies DESC, area
    """)
    print("SALARY PROFILE:", json.dumps(profile, ensure_ascii=False, indent=2), flush=True)
    print("TOP AREAS:", json.dumps(areas[:10], ensure_ascii=False, indent=2), flush=True)
    if args.inspect:
        return
    connection.execute("SET VARIABLE salary_gross = false")
    connection.execute("SET VARIABLE minimum_salary_count = 20")
    connection.execute(Path("/app/salary.sql").read_text(encoding="utf-8"))
    check = connection.execute("""
        SELECT stack_size, count(*), sum(vacancies)
        FROM stack_salary GROUP BY stack_size ORDER BY stack_size
    """).fetchall()
    if check != [(1, 1401, 28336), (2, 20454, 134651), (3, 144024, 506549)]:
        raise ValueError(f"Skill counts no longer match verified calculations: {check}")
    invalid = connection.execute("""
        SELECT count(*) FROM salary_effects
        WHERE vacancies > baseline_vacancies OR salary_count > baseline_salary_count
    """).fetchone()[0]
    if invalid:
        raise ValueError("A combination cannot occur more often than its baseline subset")
    if args.regions:
        run_regions(connection, output, total, matched, export_query)
        return
    quality = export_query(connection, output, "salary-quality.csv", """
        SELECT salary_status, count(*) AS vacancies
        FROM salary_quality GROUP BY salary_status ORDER BY vacancies DESC
    """)
    overview = export_query(connection, output, "salary-overview.csv", """
        SELECT count(*) AS vacancies, count(salary_rub) AS salary_count,
            median(salary_rub) AS median_salary_rub,
            count(salary_rub) FILTER (WHERE row_id IN (SELECT row_id FROM vacancy_skills)) AS salary_count_with_skills
        FROM salary_rows
    """)[0]
    if overview["salary_count"] < 20:
        raise ValueError("Fewer than 20 comparable salary observations")
    export_query(connection, output, "stack-salaries.csv", """
        SELECT stack_size, array_to_string(stack, ' + ') AS stack,
            vacancies, salary_count, median_salary_rub
        FROM stack_salary ORDER BY stack_size, vacancies DESC, stack
    """)
    for size, name in ((2, "pairs"), (3, "triples")):
        export_query(connection, output, f"{name}-salary-effects.csv", f"""
            SELECT * FROM salary_effects WHERE stack_size = {size}
            ORDER BY eligible DESC, difference_pct DESC NULLS LAST, stack
        """)
    rankings = {}
    for direction, comparison, ordering in (("increases", ">", "DESC"), ("decreases", "<", "ASC")):
        rankings[direction] = export_query(connection, output, f"salary-{direction}.csv", f"""
            SELECT * EXCLUDE (ranking) FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY stack_size ORDER BY difference_pct {ordering}, salary_count DESC, stack
                ) AS ranking
                FROM salary_effects WHERE eligible AND difference_pct {comparison} 0
            ) WHERE ranking <= 10 ORDER BY stack_size, ranking
        """)
    coverage = export_query(connection, output, "salary-ranking-coverage.csv", """
        SELECT stack_size, count(*) AS all_combinations,
            count(*) FILTER (WHERE eligible) AS eligible_combinations,
            count(*) FILTER (WHERE eligible AND difference_pct > 0) AS positive,
            count(*) FILTER (WHERE eligible AND difference_pct < 0) AS negative,
            count(*) FILTER (WHERE eligible AND difference_pct = 0) AS unchanged
        FROM salary_effects GROUP BY stack_size ORDER BY stack_size
    """)
    prefix = "salary/devops/" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    metadata = {
        "source": "s3://hh-lake/it_vacancies_2024.csv", "role": "DevOps-инженер",
        "total": total, "matched": matched, "duckdb_version": duckdb.__version__,
        "currency": "RUR/RUB", "gross": False, "minimum_salary_count": 20,
        "salary_proxy": "Midpoint of two bounds; the supplied bound when only one is present. Positive finite bounds only; reversed ranges excluded.",
        "baseline": "Most frequent contained single skill for a pair; most frequent contained pair for a triple. Frequency counted over all 5158 role-matching rows; lexical tie break. Baseline includes combination observations.",
        "ranking": "Median difference relative to baseline median, descending/ascending percent. Both salary counts must be at least 20.",
        "limitations": "Observational association, not causal effect. No exchange-rate or tax conversion. Experience, region and employer are not controlled. Frequencies count source rows rather than unique vacancy IDs. One-sided salary ranges are approximate.",
        "overview": overview, "quality": quality, "coverage": coverage,
        "s3_prefix": prefix,
    }
    lines = [
        "# Зарплаты DevOps: результаты анализа", "",
        f"Источник: {metadata['source']}. Выбрано {matched} вакансий из {total} строк.", "",
        f"Сопоставимая выборка: {overview['salary_count']} вакансий, RUR/RUB, gross=false. Из них с навыками: {overview['salary_count_with_skills']}.",
        f"Общая медиана оценочной зарплаты: {overview['median_salary_rub']:,.0f} руб.", "",
        "Оценка зарплаты — середина вилки; при единственной границе используется эта граница. Зарплаты до налогов и иностранные валюты не смешиваются с основной выборкой.", "",
        "Для пары база — наиболее частый входящий навык; для тройки — наиболее частая входящая пара. Частота выбирается по всем вакансиям роли, медиана — по сопоставимой зарплатной выборке. При равенстве частот используется лексикографический порядок. База включает вакансии расширенного сочетания.", "",
        "Рейтинг строится по процентной разнице медиан. Минимум 20 наблюдений зарплаты у сочетания и базы. Это порог поддержки, а не проверка статистической значимости.", "",
    ]
    for direction, title in (("increases", "Сочетания с более высокой медианой"), ("decreases", "Сочетания с более низкой медианой")):
        lines.extend(["## " + title, ""])
        for size, label in ((2, "Пары"), (3, "Тройки")):
            lines.extend(["### " + label, ""])
            for record in [item for item in rankings[direction] if item["stack_size"] == size][:5]:
                lines.append(f"- {record['stack']}: {record['median_salary_rub']:,.0f} руб. (n={record['salary_count']}); база {record['baseline_stack']}: {record['baseline_median_salary_rub']:,.0f} руб. (n={record['baseline_salary_count']}); добавлен {record['added_skill']}, разница {record['difference_pct']:+.2f}% ({record['difference_rub']:+,.0f} руб.).")
            lines.append("")
    lines.extend([
        "## Ограничения интерпретации", "",
        "Разница показывает связь сочетания навыков с указанной зарплатой в этой выборке. Она не доказывает, что добавление навыка само по себе повышает или понижает зарплату. На неё могут влиять опыт, регион, работодатель, тип вакансии и неполнота указанных навыков. Большинство вакансий не раскрывает зарплату. Исходные записи не удаляются по vacancy_id.", "",
        "CSV с полными результатами содержат число зарплатных наблюдений и медиану базы. Низкая поддержка отмечена eligible=false и не попадает в итоговые рейтинги.",
    ])
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output / "analysis.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    client = S3()
    uploaded = {}
    for path in sorted(output.iterdir()):
        if path.suffix not in (".csv", ".json", ".md"):
            continue
        payload = path.read_bytes()
        key = prefix + "/" + path.name
        with client.request("PUT", "hh-lake", key, payload):
            pass
        with client.request("GET", "hh-lake", key) as response:
            if response.read() != payload:
                raise ValueError(f"S3 read-back mismatch: {path.name}")
        uploaded[path.name] = {"key": key, "sha256": hashlib.sha256(payload).hexdigest(), "read_back_verified": True}
    completion = json.dumps({"complete": True, "files": uploaded}, ensure_ascii=False, indent=2).encode("utf-8")
    (output / "completion.json").write_bytes(completion)
    with client.request("PUT", "hh-lake", prefix + "/completion.json", completion):
        pass
    with client.request("GET", "hh-lake", prefix + "/completion.json") as response:
        if response.read() != completion:
            raise ValueError("Completion marker read-back mismatch")
    print("\nSALARY OVERVIEW:", json.dumps(overview, ensure_ascii=False), flush=True)
    print("RANKING COVERAGE:", json.dumps(coverage, ensure_ascii=False), flush=True)
    for direction, records in rankings.items():
        print("\n" + direction.upper(), flush=True)
        for size in (2, 3):
            for record in [item for item in records if item["stack_size"] == size][:3]:
                print(f"  {record['stack']} | base={record['baseline_stack']} | {record['median_salary_rub']:.0f} vs {record['baseline_median_salary_rub']:.0f} RUB | {record['difference_pct']:+.2f}% | n={record['salary_count']}", flush=True)
    print(f"\nSALARY_ANALYSIS: PASS\nRESULT: s3://hh-lake/{prefix}/\nS3_READ_BACK: PASS", flush=True)
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
