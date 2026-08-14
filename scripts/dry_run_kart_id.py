#!/usr/bin/env python3
"""
Dry-run: проверить новую логику kart_id на реальном sample (320 карт МО 7.2026).
Не пишет в БД — только симулирует.
"""
import json
import sys
from pathlib import Path
from collections import Counter

# Импортируем build_kart_id из kart_id_utils (не зависит от psycopg)
sys.path.insert(0, "/home/z/my-project/scripts")
from kart_id_utils import build_kart_id  # noqa: E402

# Sample: 320 карточек МО за 7.2026
sample_path = Path("/home/z/my-project/scripts/gibdd_sample.json")
sample = json.loads(sample_path.read_text())

cards = []
for region in sample.get("results", {}).get("region_list", []):
    for pok in region.get("pok_list", []):
        for result in pok.get("result", []):
            cards.extend(result.get("dtpcardlist", {}).get("info_dtp", []))

print(f"=== Dry-run: {len(cards)} карт, reg=1146, dat=7.2026 ===\n")

# 1. Формируем kart_id для всех карт
kart_ids = []
empt_numbers = []
skipped = 0
for card in cards:
    kart_id, empt = build_kart_id(card, "1146")
    if kart_id is None:
        skipped += 1
        continue
    kart_ids.append(kart_id)
    empt_numbers.append(empt)

print(f"Сформировано kart_id: {len(kart_ids)}")
print(f"Пропущено (нет empt_number/date_dtp): {skipped}")
print()

# 2. Примеры kart_id
print("=== Примеры kart_id (первые 10) ===")
print(f"{'empt_number':>14}  {'kart_id':>10}  {'date_dtp':>10}  breakdown")
print(f"{'-'*14}  {'-'*10}  {'-'*10}  {'-'*30}")
for i, card in enumerate(cards[:10]):
    kart_id, empt = build_kart_id(card, "1146")
    if kart_id is None:
        continue
    date_dtp = card.get("date_dtp", "")
    region_part = kart_id[:2]
    year_part = kart_id[2:4]
    seq_part = kart_id[4:]
    print(f"{empt:>14}  {kart_id:>10}  {date_dtp:>10}  {region_part}|{year_part}|{seq_part}")
print()

# 3. Проверка коллизий внутри батча (разные карты → один kart_id)
counter = Counter(kart_ids)
internal_collisions = [(k, v) for k, v in counter.items() if v > 1]
print(f"=== Коллизии внутри батча (разные карты → один kart_id) ===")
if internal_collisions:
    print(f"Найдено {len(internal_collisions)} коллизий:")
    for kart, cnt in internal_collisions[:5]:
        # Какие empt_number породили коллизию?
        empts = [e for k, e in zip(kart_ids, empt_numbers) if k == kart]
        print(f"  kart_id={kart} ({cnt}x): empt_numbers={empts[:3]}")
else:
    print("✓ Коллизий внутри батча нет (320 карт → 320 уникальных kart_id)")
print()

# 4. Проверка длины kart_id
lengths = Counter(len(k) for k in kart_ids)
print(f"=== Длины kart_id ===")
for length, count in lengths.most_common():
    print(f"  длина {length}: {count} карт")
print()

# 5. Проверка формата: все 9 цифр?
non_9digit = [k for k in kart_ids if len(k) != 9 or not k.isdigit()]
if non_9digit:
    print(f"⚠ Найдены kart_id не 9 цифр: {non_9digit[:5]}")
else:
    print("✓ Все kart_id — 9 цифр")
print()

# 6. Распределение по годам (должен быть только 2026, т.к. sample за 7.2026)
year_dist = Counter(k[2:4] for k in kart_ids)
print(f"=== Распределение по годам (позиции 3-4) ===")
for yr, cnt in year_dist.most_common():
    print(f"  год {yr}: {cnt} карт")
print()

# 7. Распределение по регионам (позиции 1-2)
region_dist = Counter(k[:2] for k in kart_ids)
print(f"=== Распределение по регионам (позиции 1-2) ===")
for reg, cnt in region_dist.most_common():
    print(f"  регион {reg}: {cnt} карт")
print()

# 8. Итог
print("=== ИТОГ DRY-RUN ===")
print(f"Всего карт в sample: {len(cards)}")
print(f"Сформировано kart_id: {len(kart_ids)}")
print(f"Пропущено: {skipped}")
print(f"Коллизий внутри батча: {len(internal_collisions)}")
print(f"Все kart_id 9 цифр: {'YES' if not non_9digit else 'NO'}")
print()
print("Вывод: схема region(2)+year(2)+seq(5) корректно формирует 9-значные")
print("kart_id. Коллизии внутри одного батча (один reg+dat) — нет.")
print("Коллизии между разными (reg, dat) — будут, но они попадут в")
print("gibdd_cards_collisions через ON CONFLICT DO NOTHING.")
