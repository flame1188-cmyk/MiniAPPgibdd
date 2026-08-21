#!/usr/bin/env python3
"""
Dry-run: проверить новую логику kart_id (17 цифр) на реальном sample.
Не пишет в БД — только симулирует. Не требует psycopg/httpx — чистая проверка kart_id_utils.

Запуск:
  python dry_run_kart_id.py
  python dry_run_kart_id.py --sample /path/to/other_sample.json
  python dry_run_kart_id.py --reg 1146
  python dry_run_kart_id.py --collision-test   # встроенный тест коллизионного кейса
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from kart_id_utils import build_kart_id  # noqa: E402


def extract_cards(api_response: dict) -> list[dict]:
    """Извлекает плоский список карточек из ответа API (как etl_archive.extract_accident_cards)."""
    cards: list[dict] = []
    results = api_response.get("results", {})
    region_list = []
    if isinstance(results, dict):
        region_list = results.get("region_list", [])
    elif isinstance(results, list):
        for r in results:
            if isinstance(r, dict) and "region_list" in r:
                region_list.extend(r["region_list"])
    for region in region_list:
        for pok in region.get("pok_list", []):
            for result in pok.get("result", []):
                cards.extend(result.get("dtpcardlist", {}).get("info_dtp", []))
    return cards


def run_on_sample(sample_path: Path, reg_code: str) -> int:
    """Симулирует kart_id на sample-файле и печатает отчёт."""
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    cards = extract_cards(sample)
    print(f"=== Dry-run: {len(cards)} карт, reg={reg_code}, sample={sample_path.name} ===\n")

    kart_ids: list[str] = []
    empt_numbers: list[str] = []
    skipped = 0
    for card in cards:
        kart_id, empt = build_kart_id(card, reg_code)
        if kart_id is None:
            skipped += 1
            continue
        kart_ids.append(kart_id)
        empt_numbers.append(empt)

    print(f"Сформировано kart_id: {len(kart_ids)}")
    print(f"Пропущено (нет empt_number/date_dtp): {skipped}")
    print()

    # 1. Примеры kart_id
    print("=== Примеры kart_id (первые 10) ===")
    print(f"{'empt_number':>14}  {'kart_id':>18}  {'date_dtp':>10}  breakdown")
    print(f"{'-'*14}  {'-'*18}  {'-'*10}  {'-'*40}")
    shown = 0
    for card in cards:
        if shown >= 10:
            break
        kart_id, empt = build_kart_id(card, reg_code)
        if kart_id is None:
            continue
        date_dtp = card.get("date_dtp", "")
        region_part = kart_id[0:2]
        year_part = kart_id[2:4]
        month_part = kart_id[4:6]
        day_part = kart_id[6:8]
        empt_part = kart_id[8:17]
        print(f"{empt:>14}  {kart_id:>18}  {date_dtp:>10}  "
              f"{region_part}|{year_part}|{month_part}|{day_part}|{empt_part}")
        shown += 1
    print()

    # 2. Коллизии внутри батча
    counter = Counter(kart_ids)
    internal_collisions = [(k, v) for k, v in counter.items() if v > 1]
    print("=== Коллизии внутри батча (разные карты → один kart_id) ===")
    if internal_collisions:
        print(f"Найдено {len(internal_collisions)} коллизий:")
        for kart, cnt in internal_collisions[:5]:
            empts = [e for k, e in zip(kart_ids, empt_numbers) if k == kart]
            print(f"  kart_id={kart} ({cnt}x): empt_numbers={empts[:3]}")
    else:
        print(f"✓ Коллизий внутри батча нет ({len(kart_ids)} карт → "
              f"{len(set(kart_ids))} уникальных kart_id)")
    print()

    # 3. Длины kart_id
    lengths = Counter(len(k) for k in kart_ids)
    print("=== Длины kart_id ===")
    for length, count in lengths.most_common():
        print(f"  длина {length}: {count} карт")
    print()

    # 4. Формат: все 17 цифр?
    non_17 = [k for k in kart_ids if len(k) != 17 or not k.isdigit()]
    if non_17:
        print(f"⚠ Найдены kart_id не 17 цифр: {non_17[:5]}")
    else:
        print("✓ Все kart_id — 17 цифр")
    print()

    # 5. Распределение по годам
    year_dist = Counter(k[2:4] for k in kart_ids)
    print("=== Распределение по годам (позиции 3-4) ===")
    for yr, cnt in year_dist.most_common():
        print(f"  год {yr}: {cnt} карт")
    print()

    # 6. Распределение по регионам
    region_dist = Counter(k[:2] for k in kart_ids)
    print("=== Распределение по регионам (позиции 1-2) ===")
    for reg, cnt in region_dist.most_common():
        print(f"  регион {reg}: {cnt} карт")
    print()

    # 7. Итог
    print("=== ИТОГ DRY-RUN ===")
    print(f"Всего карт в sample: {len(cards)}")
    print(f"Сформировано kart_id: {len(kart_ids)}")
    print(f"Пропущено: {skipped}")
    print(f"Коллизий внутри батча: {len(internal_collisions)}")
    print(f"Все kart_id 17 цифр: {'YES' if not non_17 else 'NO'}")
    print()
    print("Схема region(2)+year(2)+month(2)+day(2)+empt_number(9) корректно формирует")
    print("17-значные kart_id. Коллизии между разными (reg, dat) — попадут в")
    print("gibdd_cards_collisions через ON CONFLICT DO NOTHING.")
    return 0 if not non_17 and not internal_collisions else 1


def run_collision_test() -> int:
    """
    Встроенный тест коллизионного кейса 1103/11.2021:
      ЕЙСКИЙ и Г.СОЧИ оба получили empt_number=030000011.
    Старая 9-значная формула давала бы kolizию (11 + 21 + 00011 = "112100011" для обеих).
    Новая 17-значная с добавлением date_dtp обязана их различать.
    """
    print("=" * 78)
    print("ТЕСТ КОЛЛИЗИИ: 1103/11.2021 — kart_id должен различаться")
    print("=" * 78)
    reg_code = "1103"
    empt = "030000011"

    # две карточки с одинаковым empt_number, но разными датами
    card1 = {"empt_number": empt, "date_dtp": "28.11.2021"}
    card2 = {"empt_number": empt, "date_dtp": "01.11.2021"}

    kart1, _ = build_kart_id(card1, reg_code)
    kart2, _ = build_kart_id(card2, reg_code)
    print(f"  Карточка 1 (28.11.2021): kart_id = {kart1}")
    print(f"  Карточка 2 (01.11.2021): kart_id = {kart2}")

    assert kart1 and kart2, "FAIL: kart_id не сформирован"
    assert kart1 != kart2, "FAIL: kart_id совпадают — коллизия не устранена!"
    assert len(kart1) == 17 and len(kart2) == 17, "FAIL: kart_id не 17 цифр"
    assert kart1[:8] != kart2[:8], "FAIL: префикс (region+year+month+day) не различается"

    print(f"\n  ✓ kart_id различаются — коллизия устранена")
    print(f"  ✓ Длина: {len(kart1)} и {len(kart2)} цифр")
    print(f"  ✓ Префикс region+year+month+day различается (это и устраняет коллизию)")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Dry-run проверки kart_id (17 цифр)")
    parser.add_argument(
        "--sample",
        type=Path,
        default=SCRIPT_DIR / "gibdd_sample.json",
        help="Путь к JSON-файлу sample (по умолчанию gibdd_sample.json рядом)",
    )
    parser.add_argument("--reg", default="1146", help="Код региона (по умолчанию 1146 — МО)")
    parser.add_argument(
        "--collision-test",
        action="store_true",
        help="Запустить только встроенный тест коллизии 1103/11.2021",
    )
    args = parser.parse_args()

    if args.collision_test:
        sys.exit(run_collision_test())

    if not args.sample.is_file():
        print(f"ERROR: sample file not found: {args.sample}", file=sys.stderr)
        print("  Положите gibdd_sample.json рядом с dry_run_kart_id.py", file=sys.stderr)
        print("  или укажите --sample /path/to/sample.json", file=sys.stderr)
        # Всё равно запустим collision-тест, чтобы проверить формулу
        print("\nЗапускаю только collision-test...\n")
        sys.exit(run_collision_test())

    rc = run_on_sample(args.sample, args.reg)
    print()
    rc2 = run_collision_test()
    sys.exit(rc or rc2)


if __name__ == "__main__":
    main()
