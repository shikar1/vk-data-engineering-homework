CREATE TEMP TABLE salary_quality AS
SELECT *, CASE
    WHEN pay_from IS NULL AND pay_to IS NULL THEN 'missing_salary'
    WHEN upper(trim(coalesce(currency, ''))) NOT IN ('RUR', 'RUB') THEN 'other_currency'
    WHEN gross_bool IS NULL THEN 'unknown_tax_basis'
    WHEN gross_bool IS DISTINCT FROM getvariable('salary_gross') THEN 'other_tax_basis'
    WHEN (nullif(trim(salary_from), '') IS NOT NULL AND pay_from IS NULL)
        OR (nullif(trim(salary_to), '') IS NOT NULL AND pay_to IS NULL) THEN 'invalid_number'
    WHEN (pay_from IS NOT NULL AND (NOT isfinite(pay_from) OR pay_from <= 0))
        OR (pay_to IS NOT NULL AND (NOT isfinite(pay_to) OR pay_to <= 0)) THEN 'invalid_bound'
    WHEN pay_from > pay_to THEN 'reversed_range'
    ELSE 'included'
END AS salary_status
FROM selected;

CREATE TEMP TABLE salary_rows AS
SELECT row_id, CASE WHEN salary_status = 'included' THEN
    CASE WHEN pay_from IS NOT NULL AND pay_to IS NOT NULL
        THEN (pay_from + pay_to) / 2
        ELSE coalesce(pay_from, pay_to)
    END
END AS salary_rub
FROM salary_quality;

CREATE TEMP TABLE memberships AS
SELECT row_id, [skill] AS stack FROM vacancy_skills
UNION ALL
SELECT first_skill.row_id, [first_skill.skill, second_skill.skill] AS stack
FROM vacancy_skills AS first_skill
JOIN vacancy_skills AS second_skill
    ON first_skill.row_id = second_skill.row_id
    AND first_skill.skill < second_skill.skill
UNION ALL
SELECT first_skill.row_id, [first_skill.skill, second_skill.skill, third_skill.skill] AS stack
FROM vacancy_skills AS first_skill
JOIN vacancy_skills AS second_skill
    ON first_skill.row_id = second_skill.row_id
    AND first_skill.skill < second_skill.skill
JOIN vacancy_skills AS third_skill
    ON first_skill.row_id = third_skill.row_id
    AND second_skill.skill < third_skill.skill;

CREATE TEMP TABLE stack_salary AS
SELECT stack, len(stack) AS stack_size, count(*) AS vacancies,
    count(salary_rub) AS salary_count, median(salary_rub) AS median_salary_rub
FROM memberships
JOIN salary_rows USING (row_id)
GROUP BY stack;

CREATE TEMP TABLE baseline_candidates AS
SELECT stack, unnest(CASE WHEN stack_size = 2
    THEN [[stack[1]], [stack[2]]]
    ELSE [[stack[1], stack[2]], [stack[1], stack[3]], [stack[2], stack[3]]]
END) AS baseline_stack
FROM stack_salary WHERE stack_size IN (2, 3);

CREATE TEMP TABLE chosen_baselines AS
SELECT candidate.stack, candidate.baseline_stack,
    row_number() OVER (
        PARTITION BY candidate.stack
        ORDER BY baseline.vacancies DESC, candidate.baseline_stack
    ) AS preference
FROM baseline_candidates AS candidate
JOIN stack_salary AS baseline ON candidate.baseline_stack = baseline.stack;

CREATE TEMP TABLE salary_effects AS
SELECT combination.stack_size, array_to_string(combination.stack, ' + ') AS stack,
    array_to_string(baseline.stack, ' + ') AS baseline_stack,
    list_filter(combination.stack, skill -> NOT list_contains(baseline.stack, skill))[1] AS added_skill,
    combination.vacancies, combination.salary_count, combination.median_salary_rub,
    baseline.vacancies AS baseline_vacancies, baseline.salary_count AS baseline_salary_count,
    baseline.median_salary_rub AS baseline_median_salary_rub,
    combination.median_salary_rub - baseline.median_salary_rub AS difference_rub,
    100 * (combination.median_salary_rub / baseline.median_salary_rub - 1) AS difference_pct,
    combination.salary_count >= getvariable('minimum_salary_count')
        AND baseline.salary_count >= getvariable('minimum_salary_count') AS eligible
FROM stack_salary AS combination
JOIN chosen_baselines AS chosen ON combination.stack = chosen.stack AND chosen.preference = 1
JOIN stack_salary AS baseline ON chosen.baseline_stack = baseline.stack;
