CREATE TEMP TABLE chosen_regions AS
SELECT * FROM (VALUES ('Москва'), ('Санкт-Петербург'), ('Екатеринбург')) AS regions(region);

CREATE TEMP TABLE regional_rows AS
SELECT selected.*, salary_rows.salary_rub
FROM selected JOIN salary_rows USING (row_id)
JOIN chosen_regions ON selected.area = chosen_regions.region;

CREATE TEMP TABLE regional_summary AS
SELECT area AS region, count(*) AS vacancies,
    count(*) FILTER (WHERE row_id IN (SELECT row_id FROM vacancy_skills)) AS with_skills,
    count(*) FILTER (WHERE row_id NOT IN (SELECT row_id FROM vacancy_skills)) AS without_skills,
    count(salary_rub) AS salary_count,
    count(salary_rub) FILTER (WHERE row_id IN (SELECT row_id FROM vacancy_skills)) AS salary_count_with_skills,
    100.0 * count(salary_rub) / count(*) AS salary_coverage_pct,
    median(salary_rub) AS median_salary_rub,
    quantile_cont(salary_rub, 0.25) AS salary_q25_rub,
    quantile_cont(salary_rub, 0.75) AS salary_q75_rub
FROM regional_rows GROUP BY area;

CREATE TEMP TABLE regional_stacks AS
SELECT area AS region, stack, len(stack) AS stack_size,
    count(*) AS vacancies, count(salary_rub) AS salary_count,
    median(salary_rub) AS median_salary_rub
FROM regional_rows JOIN memberships USING (row_id)
GROUP BY area, stack;

CREATE TEMP TABLE regional_details AS
SELECT region, stack, stack_size, regional_stacks.vacancies,
    regional_summary.vacancies AS region_vacancies,
    100.0 * regional_stacks.vacancies / regional_summary.vacancies AS demand_pct,
    regional_stacks.salary_count, regional_stacks.median_salary_rub,
    regional_stacks.salary_count >= getvariable('minimum_salary_count') AS salary_support_at_least_20
FROM regional_stacks JOIN regional_summary USING (region);

CREATE TEMP TABLE regional_top AS
SELECT *, row_number() OVER (
    PARTITION BY region, stack_size ORDER BY vacancies DESC, stack
) AS ranking
FROM regional_details
QUALIFY ranking <= 10;

CREATE TEMP TABLE comparison_stacks AS
SELECT stack, stack_size, vacancies AS global_vacancies,
    row_number() OVER (PARTITION BY stack_size ORDER BY vacancies DESC, stack) AS global_ranking
FROM stack_salary
QUALIFY global_ranking <= 10;

CREATE TEMP TABLE regional_common_stacks AS
SELECT region, comparison_stacks.stack, comparison_stacks.stack_size,
    global_ranking, global_vacancies,
    coalesce(regional_stacks.vacancies, 0) AS vacancies,
    regional_summary.vacancies AS region_vacancies,
    100.0 * coalesce(regional_stacks.vacancies, 0) / regional_summary.vacancies AS demand_pct,
    coalesce(regional_stacks.salary_count, 0) AS salary_count,
    regional_stacks.median_salary_rub
FROM regional_summary CROSS JOIN comparison_stacks
LEFT JOIN regional_stacks USING (region, stack, stack_size);
