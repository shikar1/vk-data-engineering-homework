import hashlib
import itertools
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from mapreduce import S3


def validate_regions(connection):
    rows = connection.execute("""
        SELECT regional_rows.row_id, area, salary_rub,
            coalesce(list(skill ORDER BY skill) FILTER (WHERE skill IS NOT NULL), []) AS skills
        FROM regional_rows LEFT JOIN vacancy_skills USING (row_id)
        GROUP BY regional_rows.row_id, area, salary_rub
    """).fetchall()
    expected = Counter()
    salaries = defaultdict(list)
    region_counts = Counter()
    for row_id, region, salary, skills in rows:
        region_counts[region] += 1
        if salary is not None:
            salaries[region].append(salary)
        for size in (1, 2, 3):
            expected.update((region, stack) for stack in itertools.combinations(skills, size))
    result = connection.execute("SELECT region, stack, vacancies FROM regional_stacks")
    checked = 0
    while records := result.fetchmany(2048):
        for region, stack, count in records:
            if expected.pop((region, tuple(stack)), None) != count:
                raise ValueError(f"Independent regional frequency mismatch: {region}, {stack}")
            checked += 1
    if expected:
        raise ValueError("Regional SQL output is missing combinations")
    for region, count, salary_count, median in connection.execute("SELECT region, vacancies, salary_count, median_salary_rub FROM regional_summary").fetchall():
        expected_median = statistics.median(salaries[region]) if salaries[region] else None
        if (count, salary_count, median) != (region_counts[region], len(salaries[region]), expected_median):
            raise ValueError(f"Regional summary mismatch: {region}")
    top = connection.execute("SELECT region, stack, salary_count, median_salary_rub FROM regional_top").fetchall()
    for region, stack, count, median in top:
        sample = [salary for row_id, row_region, salary, skills in rows if row_region == region and salary is not None and set(stack).issubset(skills)]
        expected_median = statistics.median(sample) if sample else None
        if (count, median) != (len(sample), expected_median):
            raise ValueError(f"Regional top salary mismatch: {region}, {stack}")
    return {"full_frequency_match": True, "frequency_rows_checked": checked,
            "regional_summary_match": True, "top_stack_salary_medians_checked": len(top),
            "method": "Independent Python combinations/Counter for every regional frequency; statistics.median for summaries and top stacks; uses S3-derived normalized row data"}


def run_regions(connection, output, total, matched, export_query):
    connection.execute(Path("/app/regions.sql").read_text(encoding="utf-8"))
    counts = dict(connection.execute("SELECT region, vacancies FROM regional_summary").fetchall())
    if counts != {"Москва": 3007, "Санкт-Петербург": 686, "Екатеринбург": 188}:
        raise ValueError(f"Regional population changed: {counts}")
    validation = validate_regions(connection)
    overview = export_query(connection, output, "regional-overview.csv", "SELECT * FROM regional_summary ORDER BY vacancies DESC")
    for size, name in ((1, "skills"), (2, "pairs"), (3, "triples")):
        export_query(connection, output, f"regional-{name}.csv", f"""
            SELECT region, array_to_string(stack, ' + ') AS stack, stack_size,
                vacancies, region_vacancies, demand_pct, salary_count,
                median_salary_rub, salary_support_at_least_20
            FROM regional_details WHERE stack_size = {size}
            ORDER BY region, vacancies DESC, stack
        """)
    top = export_query(connection, output, "regional-top-stacks.csv", """
        SELECT region, stack_size, ranking, array_to_string(stack, ' + ') AS stack,
            vacancies, region_vacancies, demand_pct, salary_count,
            median_salary_rub, salary_support_at_least_20
        FROM regional_top ORDER BY region, stack_size, ranking
    """)
    common = export_query(connection, output, "regional-common-stacks.csv", """
        SELECT region, stack_size, global_ranking, array_to_string(stack, ' + ') AS stack,
            global_vacancies, vacancies, region_vacancies, demand_pct,
            salary_count, median_salary_rub
        FROM regional_common_stacks ORDER BY stack_size, global_ranking, region
    """)
    growth = export_query(connection, output, "regional-combination-growth.csv", """
        SELECT region, stack_size, count(*) AS unique_combinations, sum(vacancies) AS occurrences
        FROM regional_stacks GROUP BY region, stack_size ORDER BY region, stack_size
    """)
    export_query(connection, output, "regional-salary-quality.csv", """
        SELECT area AS region, salary_status, count(*) AS vacancies
        FROM salary_quality JOIN chosen_regions ON area = region
        GROUP BY area, salary_status ORDER BY area, vacancies DESC
    """)
    prefix = "regions/devops/" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    metadata = {
        "source": "s3://hh-lake/it_vacancies_2024.csv", "role": "DevOps-инженер",
        "total": total, "matched": matched, "regions": list(counts),
        "region_selection": "Three largest area labels by DevOps row count; exact area matches, no city/oblast merging",
        "salary_cohort": "RUR/RUB, gross=false; midpoint or sole supplied positive finite bound; invalid/reversed ranges excluded; same salary.sql as global analysis",
        "demand_denominator": "All role-matching rows of the region, including rows without skills or comparable salaries",
        "common_stacks": "Same global top 10 combinations per size evaluated in all three regions, including zero counts",
        "limitations": "Observed source rows, not unique vacancies or causal regional effects. Missing salaries/skills, experience, employer and remote work can affect comparisons. Low-support medians remain visible with sample counts.",
        "overview": overview, "validation": validation, "s3_prefix": prefix,
    }
    lines = ["# DevOps: сравнение трёх регионов", "",
             "Москва, Санкт-Петербург и Екатеринбург выбраны как три наиболее представленных значения area. Сравниваются точные значения поля, без объединения с областями.", "",
             "Зарплаты: RUR/RUB, gross=false; середина вилки или единственная указанная положительная граница. Правила совпадают с общим зарплатным анализом.", "",
             "| Регион | Вакансий | С навыками | Зарплат в выборке | Покрытие зарплат | Медиана, руб. |", "|---|---:|---:|---:|---:|---:|"]
    for row in overview:
        median_text = f"{row['median_salary_rub']:,.0f}" if row["median_salary_rub"] is not None else "нет данных"
        lines.append(f"| {row['region']} | {row['vacancies']} | {row['with_skills']} | {row['salary_count']} | {row['salary_coverage_pct']:.2f}% | {median_text} |")
    lines.extend(["", "Доля востребованности считается от всех вакансий роли в регионе, включая записи без навыков. Для зарплат каждого сочетания используется только сопоставимая зарплатная подвыборка; её размер показан отдельно.", ""])
    for region in counts:
        lines.extend(["## " + region, ""])
        for size, label in ((1, "Навык"), (2, "Пара"), (3, "Тройка")):
            leader = next(row for row in top if row["region"] == region and row["stack_size"] == size and row["ranking"] == 1)
            lines.append(f"- {label}: {leader['stack']} — {leader['vacancies']} вакансий, {leader['demand_pct']:.2f}% от региона; зарплатных наблюдений {leader['salary_count']}.")
        lines.append("")
    salary_order = sorted([row for row in overview if row["median_salary_rub"] is not None], key=lambda row: row["median_salary_rub"], reverse=True)
    lines.extend(["## Наблюдения по своим данным", ""])
    if len(salary_order) > 1:
        highest, lowest = salary_order[0], salary_order[-1]
        difference = highest["median_salary_rub"] - lowest["median_salary_rub"]
        lines.append(f"- Максимальная медиана в выбранной зарплатной подвыборке у {highest['region']}, минимальная у {lowest['region']}; разница {difference:,.0f} руб. Размеры подвыборок нужно учитывать при сравнении.")
    lines.append(f"- На три региона приходится {sum(counts.values())} из {matched} записей DevOps ({100 * sum(counts.values()) / matched:.2f}%). Абсолютные частоты зависят от размеров регионов, поэтому таблицы содержат также доли.")
    skill_groups = defaultdict(list)
    for row in common:
        if row["stack_size"] == 1:
            skill_groups[row["stack"]].append(row)
    gap_skill, gap_rows = max(skill_groups.items(), key=lambda item: max(row["demand_pct"] for row in item[1]) - min(row["demand_pct"] for row in item[1]))
    maximum = max(gap_rows, key=lambda row: row["demand_pct"])
    minimum = min(gap_rows, key=lambda row: row["demand_pct"])
    lines.append(f"- Среди десяти наиболее частых навыков всей DevOps-выборки максимальный разрыв региональных долей у {gap_skill}: {maximum['region']} — {maximum['demand_pct']:.2f}%, {minimum['region']} — {minimum['demand_pct']:.2f}%; разница {maximum['demand_pct'] - minimum['demand_pct']:.2f} процентного пункта.")
    for row in overview:
        lines.append(f"- {row['region']}: навыки не указаны в {row['without_skills']} записях ({100 * row['without_skills'] / row['vacancies']:.2f}%). Это ограничивает сравнение спроса, а не доказывает отсутствие требований к навыкам.")
    lines.extend(["", "## Проверки и ограничения", "",
                  f"Python независимо пересчитал все {validation['frequency_rows_checked']} региональных частот навыков, пар и троек; различий с SQL нет. Медианы регионов и {validation['top_stack_salary_medians_checked']} строк региональных топов проверены через statistics.median.", "",
                  "Таблица regional-common-stacks.csv сравнивает одни и те же глобальные топ-10 навыков, пар и троек во всех регионах, включая нулевые частоты. regional-combination-growth.csv показывает рост числа сочетаний: вакансия с n навыками даёт C(n,1), C(n,2), C(n,3) вкладов.", "",
                  "Медианы сочетаний с менее чем 20 зарплатами помечены недостаточной поддержкой; они приводятся с размером выборки, но не считаются надёжным рейтингом зарплат. Различия регионов описательные: опыт, работодатель и удалённая работа не контролируются; причинный эффект региона не установлен."])
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output / "analysis.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
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
                raise ValueError(f"Regional S3 read-back mismatch: {path.name}")
        uploaded[path.name] = {"key": key, "sha256": hashlib.sha256(payload).hexdigest(), "read_back_verified": True}
    completion = json.dumps({"complete": True, "files": uploaded}, ensure_ascii=False, indent=2).encode("utf-8")
    (output / "completion.json").write_bytes(completion)
    with client.request("PUT", "hh-lake", prefix + "/completion.json", completion):
        pass
    with client.request("GET", "hh-lake", prefix + "/completion.json") as response:
        if response.read() != completion:
            raise ValueError("Regional completion marker read-back mismatch")
    print("\nREGIONAL OVERVIEW:", json.dumps(overview, ensure_ascii=False, indent=2), flush=True)
    print("REGIONAL VALIDATION:", json.dumps(validation, ensure_ascii=False), flush=True)
    print(f"REGIONAL_ANALYSIS: PASS\nS3_READ_BACK: PASS\nRESULT: s3://hh-lake/{prefix}/", flush=True)
    connection.close()
