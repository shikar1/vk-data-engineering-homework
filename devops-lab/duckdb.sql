CREATE MACRO normalize_skill(value) AS
    lower(trim(regexp_replace(coalesce(value, ''), '[\s\p{Z}\x{0085}\x{001C}-\x{001F}]+', ' ', 'g')));

CREATE TEMP TABLE raw AS
SELECT row_number() OVER () AS row_id, role, key_skills
FROM read_csv(
    's3://hh-lake/it_vacancies_2024.csv',
    auto_detect = false,
    header = true,
    delim = ',',
    quote = '"',
    escape = '"',
    parallel = false,
    max_line_size = 16777216,
    buffer_size = 67108864,
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
SELECT row_id, key_skills
FROM raw
WHERE lower(trim(role)) = 'devops-инженер';

CREATE TEMP TABLE vacancy_skills AS
SELECT DISTINCT row_id, normalize_skill(token) AS skill
FROM selected
CROSS JOIN UNNEST(string_split(coalesce(key_skills, ''), ';')) AS parts(token)
WHERE normalize_skill(token) <> '';

CREATE TEMP TABLE skills AS
SELECT skill AS skill_1, count(*) AS count
FROM vacancy_skills
GROUP BY skill;

CREATE TEMP TABLE pairs AS
SELECT first_skill.skill AS skill_1, second_skill.skill AS skill_2, count(*) AS count
FROM vacancy_skills AS first_skill
JOIN vacancy_skills AS second_skill
    ON first_skill.row_id = second_skill.row_id
    AND first_skill.skill < second_skill.skill
GROUP BY first_skill.skill, second_skill.skill;

CREATE TEMP TABLE triples AS
SELECT first_skill.skill AS skill_1, second_skill.skill AS skill_2,
    third_skill.skill AS skill_3, count(*) AS count
FROM vacancy_skills AS first_skill
JOIN vacancy_skills AS second_skill
    ON first_skill.row_id = second_skill.row_id
    AND first_skill.skill < second_skill.skill
JOIN vacancy_skills AS third_skill
    ON first_skill.row_id = third_skill.row_id
    AND second_skill.skill < third_skill.skill
GROUP BY first_skill.skill, second_skill.skill, third_skill.skill;
