CREATE MACRO normalize_skill(value) AS
    lower(trim(regexp_replace(coalesce(value, ''), '[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+', ' ', 'g')));

CREATE TEMP TABLE raw AS
SELECT row_number() OVER () AS row_id, vacancy_id, role, area,
    salary_from, salary_to, currency, gross, key_skills
FROM read_csv(
    's3://hh-lake/it_vacancies_2024.csv',
    auto_detect = false, header = true, delim = ',', quote = '"', escape = '"',
    parallel = false, max_line_size = 16777216, buffer_size = 67108864,
    columns = {
        'source': 'VARCHAR', 'vacancy_id': 'VARCHAR', 'status': 'VARCHAR',
        'snapshot_date': 'VARCHAR', 'published_at': 'VARCHAR', 'area': 'VARCHAR',
        'employer_id': 'VARCHAR', 'employer_inn': 'VARCHAR', 'employer_ogrn': 'VARCHAR',
        'employer_name': 'VARCHAR', 'name': 'VARCHAR', 'role': 'VARCHAR',
        'sphere': 'VARCHAR', 'salary_from': 'VARCHAR', 'salary_to': 'VARCHAR',
        'currency': 'VARCHAR', 'gross': 'VARCHAR', 'experience': 'VARCHAR',
        'schedule': 'VARCHAR', 'employment': 'VARCHAR', 'key_skills': 'VARCHAR',
        'description': 'VARCHAR', 'url': 'VARCHAR'
    }
);

CREATE TEMP TABLE selected AS
SELECT *, try_cast(salary_from AS DOUBLE) AS pay_from,
    try_cast(salary_to AS DOUBLE) AS pay_to,
    try_cast(gross AS BOOLEAN) AS gross_bool
FROM raw
WHERE lower(trim(role)) = 'devops-инженер';

CREATE TEMP TABLE vacancy_skills AS
SELECT DISTINCT row_id, normalize_skill(token) AS skill
FROM selected
CROSS JOIN UNNEST(string_split(coalesce(key_skills, ''), ';')) AS parts(token)
WHERE normalize_skill(token) <> '';
