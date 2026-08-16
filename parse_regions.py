"""
Скрипт для синхронизации справочника регионов с официальным API ГИБДД.

Источник: http://стат.гибдд.рф/opendataapi/v1/dictionary/rows?code=1
  (кириллический домен в punycode: xn--80a7adb.xn--90adear.xn--p1ai)

Генерирует:
  - regions_builtin.py  — Python-модуль со списком регионов
  - regions_builtin.json — то же в JSON

Исключает код 1100 (Российская Федерация целиком) — это не регион.

Запуск: python parse_regions.py
"""

import json
import urllib.request
from datetime import datetime
from pathlib import Path

# Официальный API-эндпоинт словаря регионов (code=1)
# Кириллический домен переведён в punycode — urllib.request не умеет сам.
API_URL = "http://xn--80a7adb.xn--90adear.xn--p1ai/opendataapi/v1/dictionary/rows?code=1"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Коды, которые надо исключить из справочника
EXCLUDE_CODES = {"1100"}  # Российская Федерация целиком — не регион


def fetch_regions_from_api():
    """Загружает справочник регионов из официального API ГИБДД."""
    print(f"Загружаю {API_URL} ...")
    req = urllib.request.Request(API_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    rows = data["results"][0]["dict_rows"]
    print(f"Получено {len(rows)} записей от API")
    print(f"  dict_name: {data['results'][0]['dict_name']}")
    print(f"  dict_status: {data['results'][0]['dict_status']}")
    return rows


def filter_regions(rows):
    """Фильтрует регионы: исключает 1100 (РФ) и расформированные."""
    regions = []
    today = datetime.now()

    for r in rows:
        code = r["rows_code"]
        name = r["rows_name"]
        finish_str = r.get("finish_date", "31.12.2500")

        if code in EXCLUDE_CODES:
            print(f"  Исключён {code}: {name}")
            continue

        # Если finish_date в прошлом — регион расформирован, пропускаем
        try:
            finish_dt = datetime.strptime(finish_str, "%d.%m.%Y")
            if finish_dt < today:
                print(f"  Пропущен {code}: {name} (расформирован {finish_str})")
                continue
        except ValueError:
            pass

        regions.append({"code": code, "name": name})

    # Сортировка по коду
    regions.sort(key=lambda r: r["code"])
    return regions


def generate_files(regions, output_dir):
    """Генерирует regions_builtin.py и regions_builtin.json."""
    today_str = datetime.now().strftime("%Y-%m-%d")

    # ─── regions_builtin.py ───
    py_path = output_dir / "regions_builtin.py"
    py_content = f'''"""
Встроенный (builtin) справочник регионов Российской Федерации.

Извлечён из stat.gibdd.ru: http://стат.гибдд.рф/opendataapi/v1/dictionary/rows?code=1
Дата обновления: {today_str}

Используется как fallback, когда API ГИБДД недоступен и файловый кэш пуст.

Коды в формате API: "11" + двухзначный код региона.
Исключён код 1100 (Российская Федерация целиком) — это не регион.

Всего: {len(regions)} регионов.
"""

BUILTIN_REGIONS: list[dict[str, str]] = [
'''
    for r in regions:
        py_content += f'    {{"code": "{r["code"]}", "name": "{r["name"]}"}},\n'
    py_content += "]\n"

    with open(py_path, "w", encoding="utf-8") as f:
        f.write(py_content)
    print(f"✓ Сгенерирован {py_path}: {len(regions)} регионов")

    # ─── regions_builtin.json ───
    json_path = output_dir / "regions_builtin.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(regions, f, ensure_ascii=False, indent=2)
    print(f"✓ Сохранён {json_path}")


def main():
    rows = fetch_regions_from_api()
    regions = filter_regions(rows)
    print(f"\nИтого актуальных регионов: {len(regions)}")

    # Путь к директории скрипта
    output_dir = Path(__file__).resolve().parent
    generate_files(regions, output_dir)

    # Выводим первые/последние для проверки
    print("\nПервые 5 регионов:")
    for r in regions[:5]:
        print(f"  {r['code']} — {r['name']}")
    print("Последние 5 регионов:")
    for r in regions[-5:]:
        print(f"  {r['code']} — {r['name']}")

    # Проверка наличия новых регионов (добавлены в 2022-2024)
    new_codes = ["1102", "1106", "1109", "1113", "1121", "1123", "1174", "1255"]
    print("\nНовые регионы (добавлены в 2022-2024):")
    for code in new_codes:
        found = next((r for r in regions if r["code"] == code), None)
        if found:
            print(f"  ✓ {code}: {found['name']}")
        else:
            print(f"  ✗ {code}: ОТСУТСТВУЕТ!")

    # Проверка отсутствия старых кодов
    old_codes = ["1126", "1135", "1167"]
    print("\nСтарые коды (должны быть удалены):")
    for code in old_codes:
        found = next((r for r in regions if r["code"] == code), None)
        if found:
            print(f"  ✗ {code}: {found['name']} — ДОЛЖЕН БЫТЬ УДАЛЁН!")
        else:
            print(f"  ✓ {code}: удалён")


if __name__ == "__main__":
    main()
