import argparse
import csv
import io
import json
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image as RasterImage

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Flowable, Image, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle


ROOT = Path(__file__).resolve().parent
RESULTS = {
    "mapreduce": ROOT / "results-20261021-165303-339",
    "duckdb": ROOT / "duckdb-results-20261021-170136-951",
    "salary": ROOT / "salary-results-20261021-170808-263",
    "regions": ROOT / "region-results-20261021-171356-038",
    "benchmark": ROOT / "benchmark-results-20261021-172020-944",
}
NAVY = colors.black
BLUE = colors.black
TEAL = colors.HexColor("#666666")
MUTED = colors.HexColor("#444444")
PALE = colors.HexColor("#F2F2F2")
LINE = colors.HexColor("#CCCCCC")
REPOSITORY_URL = "https://github.com/shikar1/vk-data-engineering-homework"
WIDTH = A4[0] - 88


def register_fonts():
    windows = Path("C:/Windows/Fonts")
    linux = Path("/usr/share/fonts/truetype/dejavu")
    families = {
        "Body": [windows / "arial.ttf", linux / "DejaVuSans.ttf"],
        "BodyBold": [windows / "arialbd.ttf", linux / "DejaVuSans-Bold.ttf"],
        "Mono": [windows / "consola.ttf", linux / "DejaVuSansMono.ttf"],
    }
    for name, candidates in families.items():
        source = next((path for path in candidates if path.exists()), None)
        if source is None:
            raise FileNotFoundError(f"No Cyrillic font for {name}; install fonts-dejavu-core")
        pdfmetrics.registerFont(TTFont(name, str(source)))
    pdfmetrics.registerFontFamily("Body", normal="Body", bold="BodyBold", italic="Body", boldItalic="BodyBold")


def load_json(group, filename):
    return json.loads((RESULTS[group] / filename).read_text(encoding="utf-8"))


def load_csv(group, filename):
    with (RESULTS[group] / filename).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def number(value, decimals=0):
    if value is None or value == "":
        return "нет данных"
    return f"{float(value):,.{decimals}f}".replace(",", " ").replace(".", ",")


def clean(value):
    return str(value).replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")


class ComparisonPlot(Flowable):
    def __init__(self, runs):
        super().__init__()
        self.runs = runs
        self.width = WIDTH
        self.height = 220

    def draw(self):
        canvas = self.canv
        panel_width = (self.width - 32) / 2
        for panel, field, title, unit in ((0, "wall_seconds", "Время полного процесса", "с"), (1, "peak_rss_mib", "Пик RSS процесса", "МиБ")):
            left = panel * (panel_width + 32) + 36
            bottom = 36
            plot_width = panel_width - 45
            plot_height = 140
            maximum = max(float(row[field]) for row in self.runs) * 1.12
            canvas.setFont("BodyBold", 9)
            canvas.setFillColor(NAVY)
            canvas.drawString(panel * (panel_width + 32), 205, title)
            canvas.setFont("Body", 8)
            canvas.drawString(panel * (panel_width + 32), 191, unit)
            for tick in range(5):
                ordinate = bottom + plot_height * tick / 4
                canvas.setStrokeColor(LINE)
                canvas.line(left, ordinate, left + plot_width, ordinate)
                canvas.setFillColor(MUTED)
                canvas.drawRightString(left - 5, ordinate - 3, number(maximum * tick / 4))
            for attempt in (1, 2, 3):
                abscissa = left + (attempt - 1) * plot_width / 2
                canvas.setFillColor(MUTED)
                canvas.drawCentredString(abscissa, bottom - 15, str(attempt))
            canvas.drawCentredString(left + plot_width / 2, 5, "Номер попытки")
            for method, color in (("mapreduce", BLUE), ("duckdb", TEAL)):
                points = []
                for row in sorted((row for row in self.runs if row["method"] == method), key=lambda row: int(row["attempt"])):
                    points.append((left + (int(row["attempt"]) - 1) * plot_width / 2,
                                   bottom + float(row[field]) / maximum * plot_height))
                canvas.setStrokeColor(color)
                canvas.setFillColor(color)
                canvas.setLineWidth(1.5)
                if method == "duckdb":
                    canvas.setDash(4, 2)
                else:
                    canvas.setDash()
                for previous, following in zip(points, points[1:]):
                    canvas.line(*previous, *following)
                canvas.setDash()
                for abscissa, ordinate in points:
                    if method == "duckdb":
                        canvas.rect(abscissa - 3, ordinate - 3, 6, 6, stroke=1, fill=1)
                    else:
                        canvas.circle(abscissa, ordinate, 3, stroke=1, fill=1)


class RegionalPlot(Flowable):
    def __init__(self, overview, triples):
        super().__init__()
        self.overview = overview
        self.triples = triples
        self.width = WIDTH
        self.height = 210

    def draw(self):
        canvas = self.canv
        panels = [("Медиана зарплаты, тыс. руб.", [float(row["median_salary_rub"]) / 1000 for row in self.overview]),
                  ("Docker + Kubernetes + Linux, %", [self.triples[row["region"]] for row in self.overview])]
        for panel, (title, values) in enumerate(panels):
            left = panel * 263 + 34
            plot_width = 192
            bottom = 40
            plot_height = 120
            maximum = max(values) * 1.2
            canvas.setFillColor(NAVY)
            canvas.setFont("BodyBold", 9)
            canvas.drawString(panel * 263, 190, title)
            canvas.setFont("Body", 7.5)
            for tick in range(4):
                ordinate = bottom + plot_height * tick / 3
                canvas.setStrokeColor(LINE)
                canvas.line(left, ordinate, left + plot_width, ordinate)
                canvas.setFillColor(MUTED)
                canvas.drawRightString(left - 5, ordinate - 3, number(maximum * tick / 3))
            for index, (value, label) in enumerate(zip(values, ["Москва", "СПб", "Екатеринбург"])):
                abscissa = left + index * 65 + 9
                height = value / maximum * plot_height
                canvas.setFillColor([BLUE, TEAL, colors.HexColor("#BBBBBB")][index])
                canvas.setStrokeColor(colors.black)
                canvas.rect(abscissa, bottom, 34, height, stroke=1, fill=1)
                canvas.setFillColor(NAVY)
                canvas.drawCentredString(abscissa + 17, bottom + height + 6, number(value, 1))
                canvas.drawCentredString(abscissa + 17, bottom - 15, label)


def build(author, destination):
    register_fonts()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", fontName="BodyBold", fontSize=27, leading=34, textColor=NAVY, spaceAfter=18))
    styles.add(ParagraphStyle(name="ReportHeading", fontName="BodyBold", fontSize=17, leading=22, textColor=NAVY, spaceAfter=12))
    styles.add(ParagraphStyle(name="ReportSubheading", fontName="BodyBold", fontSize=11, leading=15, textColor=NAVY, spaceBefore=10, spaceAfter=6))
    styles.add(ParagraphStyle(name="ReportBody", fontName="Body", fontSize=9.4, leading=14, spaceAfter=8, textColor=NAVY))
    styles.add(ParagraphStyle(name="ReportSmall", fontName="Body", fontSize=8, leading=11, spaceAfter=6, textColor=MUTED))
    styles.add(ParagraphStyle(name="ReportCell", fontName="Body", fontSize=8, leading=11, textColor=NAVY))
    styles.add(ParagraphStyle(name="ReportCellHeader", fontName="BodyBold", fontSize=8, leading=10.5, textColor=colors.white))
    styles.add(ParagraphStyle(name="ReportCode", fontName="Mono", fontSize=7.3, leading=10, spaceAfter=9, textColor=NAVY, backColor=PALE, borderPadding=7))
    story = []

    def paragraph(text, small=False):
        return Paragraph(escape(clean(text)).replace("\n", "<br/>"), styles["ReportSmall" if small else "ReportBody"])

    def add(text, small=False):
        story.append(paragraph(text, small))

    def heading(text, sub=False):
        story.append(Paragraph(escape(text), styles["ReportSubheading" if sub else "ReportHeading"]))

    def page(title):
        if story:
            story.append(PageBreak())
        heading(title)

    def table(headers, rows, weights=None):
        widths = [WIDTH / len(headers)] * len(headers) if weights is None else [WIDTH * weight / sum(weights) for weight in weights]
        cells = [[Paragraph(escape(clean(value)).replace("\n", "<br/>"), styles["ReportCellHeader"]) for value in headers]]
        cells.extend([[Paragraph(escape(clean(value)).replace("\n", "<br/>"), styles["ReportCell"]) for value in row] for row in rows])
        widget = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT")
        widget.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE]),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 6),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("LINEBELOW", (0, -1), (-1, -1), 0.5, LINE)]))
        story.extend([widget, Spacer(1, 10)])

    def code(text):
        story.append(Preformatted(clean(text), styles["ReportCode"], maxLineLength=101, splitChars=" "))

    def repository_link(prefix):
        story.append(Paragraph(
            f'<b>{escape(prefix)} <link href="{REPOSITORY_URL}" color="#000000">{REPOSITORY_URL}</link></b>',
            styles["ReportSmall"],
        ))

    def image(filename, maximum_height):
        buffer = io.BytesIO()
        with RasterImage.open(ROOT / "evidence" / filename) as source:
            source.convert("L").save(buffer, format="PNG")
        buffer.seek(0)
        widget = Image(buffer)
        factor = min(WIDTH / widget.imageWidth, maximum_height / widget.imageHeight)
        widget.drawWidth = widget.imageWidth * factor
        widget.drawHeight = widget.imageHeight * factor
        widget.hAlign = "LEFT"
        story.extend([widget, Spacer(1, 7)])

    baseline = load_json("mapreduce", "manifest.json")
    verification = load_json("duckdb", "verification.json")
    salary = load_json("salary", "analysis.json")
    regional = load_json("regions", "analysis.json")
    configuration = load_json("benchmark", "configuration.json")
    fault = load_json("benchmark", "fault-test.json")
    runs = load_csv("benchmark", "runs.csv")
    summary = load_csv("benchmark", "summary.csv")
    overview = load_csv("regions", "regional-overview.csv")
    regional_top = load_csv("regions", "regional-top-stacks.csv")

    story.append(Spacer(1, 34))
    add("ДОМАШНЕЕ ЗАДАНИЕ / ИНЖЕНЕРИЯ ДАННЫХ", True)
    story.append(Paragraph("Аналитика IT-вакансий<br/>в RustFS", styles["ReportTitle"]))
    story.append(Spacer(1, 20))
    table(["Автор", "Индивидуальный сегмент"], [[author, "DevOps-инженер\nФильтрация по столбцу role"]], [1, 1])
    heading("Основные результаты", True)
    table(["Показатель", "Полученный результат"], [
        ["Исходные данные", "229 388 строк; 847 619 384 байта"],
        ["Выборка своего варианта", "5 158 вакансий; 1 624 без навыков"],
        ["Полное совпадение методов", "1 401 навык, 20 454 пары, 144 024 тройки"],
        ["Зарплаты и регионы", "762 сопоставимые зарплаты; 3 региона"],
        ["Профилирование и отказ", "По 3 попытки; управляемый отказ и полный перезапуск"],
    ], [1, 1.8])
    repository_link("Репозиторий:")

    page("1. Хранилище и исходные данные")
    add("Использован ранее развёрнутый контейнер rustfs-lab2. S3 доступен на localhost:9000, консоль - localhost:9001. Программы работают в Docker и обращаются к S3 через host.docker.internal:9000.")
    table(["Объект", "Значение"], [["Бакет", "hh-lake"], ["Исходный файл", "it_vacancies_2024.csv"], ["Объём", "847 619 384 байта (около 808,35 МиБ)"], ["Роль", "DevOps-инженер"], ["Отбор", "Точное равенство role после очистки пробелов и нормализации регистра"]], [1, 2.4])
    image("rustfs-results.png", 350)
    add("Рисунок 1. Фактический список выходных объектов MapReduce в консоли RustFS: skills.csv, pairs.csv, triples.csv и manifest.json.", True)
    add("Ключи доступа берутся из окружения работающего контейнера и передаются дочернему Docker-процессу через окружение. Их значения в коде, отчёте и логах не сохраняются. Python в Windows для расчётов не требуется.")

    page("2. Собственная реализация MapReduce")
    add("Исходный CSV читается непосредственно из HTTP-ответа S3 потоково. Полный исходный файл не сохраняется на локальный диск и не загружается целиком в Python-объект. В памяти остаются счётчики уникальных сочетаний. Реализация однопроцессная, без Hadoop/Spark и распределённого планировщика.")
    for text in ["Map: для каждой подходящей строки разделить key_skills по ';', схлопнуть пробелы, применить casefold, убрать дубликаты внутри вакансии и упорядочить навыки.",
                 "Сформировать неупорядоченные сочетания размеров 1, 2 и 3 с единичным вкладом. Reduce суммирует вклады в Counter.",
                 "Проверить общее число строк и выбранных вакансий, суммы вкладов C(n,k), затем отсортировать таблицы по убыванию частоты и названию навыков.",
                 "Сохранить CSV в S3, скачать их повторно, сравнить байты и разобранные частоты; записать манифест с SHA256."]:
        add("- " + text)
    table(["Размер стека", "Уникальных сочетаний", "Сумма частот"], [[str(size), number(baseline["files"][name]["rows"]), number(baseline["files"][name]["occurrences"])] for size, name in ((1, "skills.csv"), (2, "pairs.csv"), (3, "triples.csv"))])
    add("Вакансия с n различными навыками создаёт C(n,1)=n, C(n,2)=n(n-1)/2 и C(n,3)=n(n-1)(n-2)/6 вкладов. Например, для 10 навыков это 10, 45 и 120. Поэтому пар и троек существенно больше, хотя вакансии те же.")
    heading("Наиболее частые навыки", True)
    skills = load_csv("mapreduce", "skills.csv")[:10]
    table(["Навык", "Вакансий", "Навык", "Вакансий"], [[skills[index]["skill_1"], number(skills[index]["count"]), skills[index + 5]["skill_1"], number(skills[index + 5]["count"])] for index in range(5)], [1.4, 0.8, 1.4, 0.8])
    add("Частоты считаются по исходным строкам. Повторяющиеся vacancy_id не удаляются: это сохраняет единую единицу наблюдения и контроль 5 158 строк своего варианта.", True)

    page("3. Независимый расчёт DuckDB и сверка")
    add("DuckDB 1.4.3 с расширением httpfs самостоятельно читает тот же CSV по S3 URI. В SQL явно заданы формат CSV и 23 строковых столбца: это исключает ошибки автоопределения кавычек и типов. Каждой исходной строке назначается row_id.")
    code("FROM read_csv('s3://hh-lake/it_vacancies_2024.csv', ...)\n\nCREATE TEMP TABLE selected AS\nSELECT row_id, key_skills FROM raw\nWHERE lower(trim(role)) = 'devops-инженер';\n\nSELECT first_skill.skill AS skill_1,\n       second_skill.skill AS skill_2, count(*) AS count\nFROM vacancy_skills AS first_skill\nJOIN vacancy_skills AS second_skill\n  ON first_skill.row_id = second_skill.row_id\n AND first_skill.skill < second_skill.skill\nGROUP BY first_skill.skill, second_skill.skill;")
    add("После разбиения key_skills через string_split/UNNEST удаляются дубликаты навыков в пределах row_id. Пары и тройки формируются соединениями с условиями skill_1 < skill_2 < skill_3; подсчёт выполняет GROUP BY. Полный исполняемый запрос находится в duckdb.sql.")
    table(["Таблица", "Строк", "Сумма частот", "Различий"], [[name, number(data["rows"]), number(data["occurrences"]), number(data["differences"])] for name, data in verification["tables"].items()], [1, 1, 1.2, 0.8])
    code("FULL_COMPARISON: PASS\nTOTAL: 229388\nMATCHED: 5158\nWITHOUT_SKILLS: 1624")
    add("Сверка выполняется через EXCEPT ALL в обе стороны, по всем столбцам и частотам. Эталон скачивается из S3 и проверяется по SHA256. Во всех трёх таблицах ноль различий; проверено 165 879 уникальных сочетаний, а не только топ-10.")
    add("MapReduce использует casefold, SQL - lower и нормализацию Unicode-пробелов. Для произвольных строк это не полностью одинаковые операции; совпадение подтверждено именно на данном наборе полным сравнением результатов.", True)

    page("4. Методика зарплатного анализа")
    add("Зарплаты сравниваются только внутри своего варианта DevOps. Основная подвыборка: RUR/RUB с gross=false. Зарплаты с gross=true и суммы в других валютах не смешиваются; налоговая и валютная конвертация не выполняется.")
    table(["Причина / группа", "Число строк"], [[{"missing_salary": "Зарплата отсутствует", "included": "Включены в сопоставимую выборку", "other_tax_basis": "Другой налоговый признак", "other_currency": "Другая валюта"}.get(row["salary_status"], row["salary_status"]), number(row["vacancies"])] for row in load_csv("salary", "salary-quality.csv")], [2, 1])
    add("Если указаны обе границы, оценка зарплаты равна (salary_from + salary_to)/2. Если указана одна граница, используется она. Некорректные, неположительные, бесконечные и перевёрнутые вилки исключаются. Одна граница - приближение зарплаты, а не точное предложение.")
    table(["Показатель", "Значение"], [["Сопоставимых зарплат", "762"], ["Из них вакансий с навыками", "564"], ["Общая медиана оценки зарплаты", "210 000 руб."], ["Допущено в рейтинги", "236 пар; 688 троек"]], [1.5, 1])
    heading("Как выбирается база для дополнительного навыка", True)
    add("Для пары базой служит более частый из двух навыков. Для тройки - более частая из трёх входящих пар. Частота базы определяется по всем 5 158 строкам роли; при равенстве используется лексикографический порядок. Базовая выборка включает вакансии расширенного сочетания.")
    code("difference_rub = median(combination) - median(baseline)\ndifference_pct = 100 * (median(combination) / median(baseline) - 1)")
    add("Оба размера зарплатной подвыборки должны быть не меньше 20. В таблицах сохраняются n сочетания, n базы, обе медианы, добавленный навык и разница. Порог 20 ограничивает низкую поддержку, но не является тестом статистической значимости.")

    for direction, title in (("increases", "5. Сочетания с более высокой медианой"), ("decreases", "6. Сочетания с более низкой медианой")):
        page(title)
        ranking = load_csv("salary", f"salary-{direction}.csv")
        add("Первые пять результатов отдельно для пар и троек, упорядоченные по процентной разнице медиан относительно выбранной базы. n / n базы - число сопоставимых зарплат.")
        for size, label in ((2, "Пары"), (3, "Тройки")):
            heading(label, True)
            selected = [row for row in ranking if int(row["stack_size"]) == size][:5]
            table(["Сочетание / добавлен", "База", "Медиана, руб.", "Медиана базы", "Разница, %", "n / n базы"], [[row["stack"] + "\nДобавлен: " + row["added_skill"], row["baseline_stack"], number(row["median_salary_rub"]), number(row["baseline_median_salary_rub"]), f"{float(row['difference_pct']):+.2f}".replace(".", ","), row["salary_count"] + " / " + row["baseline_salary_count"]] for row in selected], [2.2, 1.45, 0.9, 0.9, 0.75, 0.8])
        add("Интерпретация: различается состав вакансий с данным сочетанием и базой. Наблюдаемая разница не доказывает, что добавление навыка само по себе изменяет зарплату; опыт, работодатель и регион здесь не контролируются.", True)

    page("7. Региональный анализ: объёмы и зарплаты")
    add("Выбраны три наиболее представленных значения area: Москва, Санкт-Петербург и Екатеринбург. Это точные метки городов в данных, без объединения с областями. Вместе они содержат 3 881 из 5 158 строк DevOps (75,24%).")
    table(["Регион", "Вакансий", "С навыками", "Зарплат", "Покрытие", "Медиана, руб."], [[row["region"], number(row["vacancies"]), number(row["with_skills"]), number(row["salary_count"]), number(row["salary_coverage_pct"], 2) + "%", number(row["median_salary_rub"])] for row in overview], [1.5, 0.8, 0.9, 0.8, 0.9, 1.1])
    heading("Лидеры спроса каждого размера стека", True)
    leaders = [row for row in regional_top if row["ranking"] == "1"]
    leaders.sort(key=lambda row: (list(regional["regions"]).index(row["region"]), int(row["stack_size"])))
    table(["Регион", "Размер", "Сочетание", "Частота", "Доля, %", "n зарплат"], [[row["region"], row["stack_size"], row["stack"], number(row["vacancies"]), number(row["demand_pct"], 2), row["salary_count"]] for row in leaders], [1.5, 0.6, 2.6, 0.8, 0.8, 0.8])
    add("Доля считается от всех DevOps-вакансий региона, включая записи без навыков и без зарплаты. Медианы - только по сопоставимой рублёвой подвыборке с gross=false; правила совпадают с общим анализом.")
    add("У ведущей тройки Екатеринбурга только 12 зарплатных наблюдений, у пары - 18. Такие медианы сохранены с размерами выборок, но имеют признак недостаточной поддержки (<20) и не считаются устойчивым зарплатным рейтингом.", True)

    page("8. Региональные различия и проверки")
    triples = {row["region"]: float(row["demand_pct"]) for row in leaders if row["stack_size"] == "3"}
    story.append(RegionalPlot(overview, triples))
    add("Рисунок 2. Слева - медианы сопоставимых зарплат; справа - доля одного и того же сочетания во всех трёх регионах. Источник: regional-overview.csv и regional-top-stacks.csv.", True)
    for text in ["Москва имеет медиану 250 000 руб., Санкт-Петербург - 220 000 руб., Екатеринбург - 150 000 руб. Разница Москвы и Екатеринбурга составляет 100 000 руб.; зарплатные подвыборки различаются по размеру: 406 против 34.",
                 "Во всех трёх регионах лидируют linux, пара docker + kubernetes и тройка docker + kubernetes + linux. При этом относительная доля тройки максимальна в Екатеринбурге (26,06%), несмотря на меньший абсолютный объём вакансий.",
                 "Среди глобального топ-10 отдельных навыков наибольший региональный разрыв долей у docker: 42,02% в Екатеринбурге и 33,29% в Москве, разница 8,73 процентного пункта.",
                 "Навыки не указаны в 32,99% московских записей, 28,86% петербургских и 27,66% екатеринбургских. Отсутствие навыков в записи нельзя трактовать как отсутствие требований работодателя."]:
        add("- " + text)
    table(["Проверка", "Результат"], [["Независимый Python-перебор всех региональных сочетаний", "169 854 частоты; различий 0"], ["Проверка медиан регионов и их топов", "3 региональные медианы и 90 медиан топов совпали"], ["Сопоставление одинаковых сочетаний", "Глобальные топ-10 для каждого размера стека во всех регионах, включая нулевые частоты"]], [1.5, 1.5])
    add("Расчёт описательный. Структура опыта, работодатели и удалённая работа не контролируются; причинный эффект региона не установлен.", True)

    page("9. Профилирование: единый протокол")
    add("Выполнено по три попытки каждого метода в свежих дочерних Python-процессах. Порядок чередовался по раундам: MapReduce/DuckDB, DuckDB/MapReduce, MapReduce/DuckDB. Все шесть результатов совпали по SHA256 трёх полных канонических CSV.")
    table(["Условие", "Значение"], [["Среда", f"Python {configuration['python']}; DuckDB {configuration['duckdb']}; Linux/WSL2 x86_64"], ["Ресурсы контейнера", "2 CPU; 2 ГиБ RAM. DuckDB: threads=2, memory_limit=1GB; CSV parallel=false. MapReduce однопоточный."], ["Время", "perf_counter родителя: создание дочернего процесса - его завершение"], ["Память", "Пик RSS за всю жизнь дочернего процесса через Linux wait4; единица - МиБ"], ["Включено", "Импорты, подключение, чтение S3, нормализация, все три агрегации, сортировка и локальная запись CSV"], ["Не включено", "Сборка/запуск Docker, сверка эталона, загрузка выходов в S3, тест отказа и перезапуск; память RustFS и контроллера"]], [0.8, 2.8])
    heading("Исходные измерения", True)
    table(["Метод", "Попытка", "Время, с", "Пик RSS, МиБ", "Совпадение"], [[row["method"], row["attempt"], number(row["wall_seconds"], 3), number(row["peak_rss_mib"], 2), "да"] for row in runs], [1.2, 0.7, 1, 1.2, 0.9])
    add("Кеши ОС/RustFS не очищались; источник уже читался ранее. Это повторные замеры текущего окружения, без утверждения о холодном кеше. Перед, между и после запусков контролировались ETag, размер и Last-Modified источника; изменений не обнаружено. Multipart ETag не трактуется как SHA256 файла.", True)

    page("10. Время и память: результаты")
    table(["Метод", "Медиана времени, с", "Мин - макс, с", "SD времени, с", "Медиана RSS, МиБ", "Макс. RSS, МиБ"], [[row["method"], number(row["median_wall_seconds"], 3), number(row["min_wall_seconds"], 2) + " - " + number(row["max_wall_seconds"], 2), number(row["stdev_wall_seconds"], 2), number(row["median_peak_rss_mib"], 2), number(row["max_peak_rss_mib"], 2)] for row in summary], [1, 1, 1.2, 0.9, 1, 1])
    story.append(ComparisonPlot(runs))
    add("Сплошная линия и круги - MapReduce; пунктир и квадраты - DuckDB. Каждая точка соответствует одной фактической попытке; номер попытки не означает последовательный запуск только одного метода.", True)
    image("benchmark-terminal.png", 125)
    add("Рисунок 3. Предоставленный автором скриншот вывода PowerShell с шестью измерениями и проверкой all_tables_equal=True.", True)
    add("Медиана времени: 74,214 с у MapReduce и 79,729 с у DuckDB. Диапазоны перекрываются, разброс велик; устойчивое превосходство по скорости тремя попытками не доказано. Медиана пиков RSS у MapReduce примерно в 12,20 раза меньше (79,25 против 966,52 МиБ).")
    add("Разницу памяти могут объяснять CSV-буферы и SQL-операторы DuckDB по сравнению с потоковым разбором и счётчиками MapReduce. Отдельной трассировки аллокаций не было. Старый диагностический SQL-замер 39,436 с не смешивается с этой серией: его границы были другими.", True)

    page("11. Отказ маппера и восстановление")
    add("В реальную функцию mapper введён управляемый отказ на 100-й подходящей вакансии. Проверяется ошибка вычислительного этапа до публикации таблиц; отказ S3 во время загрузки здесь не моделируется.")
    code("python /app/mapreduce.py --fail-after-matches 100\n\nFAILED: RuntimeError: INJECTED_MAPPER_FAILURE")
    table(["Проверка", "Факт"], [["Код завершения при отказе", str(fault["failure"]["exit_code"])], ["Локальные CSV/manifest", "Отсутствуют"], ["S3 skills/pairs/triples/manifest в префиксе отказа", "HEAD каждого объекта вернул 404"], ["Повторный запуск без управляемой ошибки", "Код 0; все три таблицы совпали с эталоном"], ["Проверка восстановленных выходов", "Загружены и перечитаны из S3"]], [1.6, 1.4])
    table(["Запуск теста", "Время, с", "Пик RSS, МиБ"], [["Отказ", number(fault["failure"]["wall_seconds"], 3), number(fault["failure"]["peak_rss_mib"], 2)], ["Полный повторный запуск", number(fault["recovery"]["wall_seconds"], 3), number(fault["recovery"]["peak_rss_mib"], 2)]])
    add("Эти два запуска не входят в шесть сравнительных попыток. Перезапуск исполняет обычную программу с загрузкой и обратным чтением S3, поэтому его границы шире, чем у сравнительного worker.")
    code("TOTAL: 229388\nMATCHED: 5158\nWITHOUT_SKILLS: 1624\nVERIFIED: all output objects downloaded and checked\nMAPPER_FAILURE_AND_RECOVERY: PASS")
    add("Показаны корректное прерывание и явно инициированный полный перезапуск с начала. Автоматические повторы, контрольные точки и замещение узлов распределённого кластера не реализованы. Фактические логи: fault-failed.log и fault-recovered.log; измерения - fault-test.json.")

    page("12. Воспроизведение и комплект материалов")
    add("Предварительные условия: Docker Desktop, работающий rustfs-lab2, бакет hh-lake и исходный объект it_vacancies_2024.csv. В репозитории исходный CSV не дублируется. Ключи должны быть доступны в окружении контейнера RustFS.")
    code("Set-Location .\\devops-lab\n\n# Проверка столбцов и примеров исходного CSV\n& .\\run.ps1 -Inspect\n\n# MapReduce и SQL с полной сверкой\n& .\\run.ps1\n& .\\run-duckdb.ps1\n\n# Зарплаты и три региона\n& .\\run-analysis.ps1\n& .\\run-regions.ps1\n\n# По 3 замера и тест отказа/перезапуска\n& .\\run-benchmark.ps1")
    table(["Файлы", "Назначение"], [["mapreduce.py; run.ps1", "Потоковый расчёт и работа с S3"], ["duckdb.sql; duckdb_run.py", "Независимый SQL и полная сверка"], ["analysis-source.sql; salary.sql; analysis_run.py", "Фильтрация зарплат, медианы и выбор базы"], ["regions.sql; regional_analysis.py", "Частоты, доли, зарплаты и независимая проверка регионов"], ["benchmark_worker.py; benchmark_run.py", "Свежие процессы, замеры и тест отказа"], ["Dockerfile.duckdb; скрипты *.ps1", "Воспроизводимое окружение без Python в Windows"], ["build_report.py; evidence/; report/", "Генератор PDF, оригинальные скриншоты и готовый отчёт"]], [1.7, 1.8])
    repository_link("Репозиторий:")
    add("Материалы работы размещены в отдельной папке devops-lab. В комплект входят исходные программы, фактические таблицы и логи, настройки замеров и PDF.", True)

    page("13. Выводы и ограничения")
    for text in ["Построен рабочий пайплайн от S3-объекта RustFS до частот, зарплатных и региональных таблиц. Выбор варианта задан собственным условием по role и подтверждён числом 5 158.",
                 "MapReduce и DuckDB полностью совпали по навыкам, парам и тройкам. Рост числа сочетаний объясняется C(n,k), а не увеличением количества вакансий.",
                 "Самая высокая положительная разница среди поддержанных пар - devops + terraform относительно devops: +47,50%. Среди троек - bash + devops + elk относительно bash + devops: +52,17%.",
                 "Есть и отрицательные разницы: postgresql + zabbix относительно postgresql -23%; git + grafana + prometheus относительно grafana + prometheus -36%. В итоговых таблицах явно названы добавленные навыки и медианы баз.",
                 "Московская медиана сопоставимой зарплаты выше, но доли некоторых сочетаний выше в Екатеринбурге. Абсолютные частоты и относительная востребованность отвечают на разные вопросы.",
                 "Три попытки каждого метода выполнены с согласованными границами. MapReduce показал существенно меньший пик RSS; близкие медианы времени при большом разбросе не доказывают устойчивого преимущества по скорости.",
                 "Отказ маппера не создал незавершённых выходов; отдельный полный перезапуск восстановил правильный результат. Это не распределённая отказоустойчивость."]:
        add("- " + text)
    heading("Ограничения", True)
    add("Зарплата отсутствует у 4 047 из 5 158 вакансий. Анализ использует 762 сопоставимые рублёвые зарплаты с gross=false, из них 564 с навыками. Вилки с одной границей приблизительны. Опыт, работодатель, региональные различия состава вакансий и удалённая работа не контролируются. Наблюдаемые разницы не являются причинными эффектами.")
    add("Бонусная оптимизация с отдельным сравнением 'до/после' не заявляется: сравнение двух подходов не подменяет эксперимент по оптимизации одного алгоритма.")
    heading("Источники и доказательства", True)
    for text in ["Данные: s3://hh-lake/it_vacancies_2024.csv; точный размер и ETag зафиксированы в configuration.json.",
                 "Результаты S3: mapreduce/devops/20261021T135359197166Z; duckdb/devops/20261021T140217322374Z; salary/devops/20261021T140856183615Z; regions/devops/20261021T141449855514Z; benchmark/devops/20261021T142021716249Z.",
                 "Документация DuckDB: https://duckdb.org/docs/stable/data/csv/overview и https://duckdb.org/docs/stable/core_extensions/httpfs/s3api. Расчёт использует закреплённую версию 1.4.3.",
                 "Скриншоты: реальные изображения консоли RustFS и вывода PowerShell, предоставленные автором. Остальные таблицы и графики автоматически построены из сохранённых результатов."]:
        add(text, True)

    def decorate(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(44, A4[1] - 30, A4[0] - 44, A4[1] - 30)
        canvas.setFillColor(MUTED)
        canvas.setFont("Body", 7)
        canvas.drawString(44, A4[1] - 23, "DEVOPS / RUSTFS / MAPREDUCE / DUCKDB")
        canvas.drawRightString(A4[0] - 44, 24, str(document.page))
        canvas.restoreState()

    destination.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(str(destination), pagesize=A4, rightMargin=44, leftMargin=44, topMargin=47, bottomMargin=42,
                                 title="Аналитика IT-вакансий в RustFS - DevOps", author=author)
    document.build(story, onFirstPage=decorate, onLaterPages=decorate)
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--author", default="Нимеева Ангелина Борисовна")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "pdf" / "hh-devops-report.pdf")
    arguments = parser.parse_args()
    build(arguments.author, arguments.output)
