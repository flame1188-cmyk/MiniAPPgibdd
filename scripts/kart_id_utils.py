"""
Утилита формирования kart_id (9 цифр): region(2) + year(2) + seq(5).

Вынесена в отдельный модуль, чтобы:
- etl_archive.py использовал её при ETL-загрузке
- dry_run / тесты могли импортировать без зависимости от psycopg
- bot/access.py или miniapp/backend могли использовать для обратной совместимости
"""
from datetime import datetime


def build_kart_id(card: dict, reg_code: str) -> tuple[str | None, str | None]:
    """
    Формирует 9-значный kart_id из карточки.

    Структура:
      region(2) — последние 2 цифры reg_code (46 для "1146")
      year(2)   — последние 2 цифры года из date_dtp (26 для 2026)
      seq(5)    — последние 5 цифр empt_number

    Args:
        card: словарь карточки ДТП из API ГИБДД
        reg_code: код региона ("1146" для Московской обл.)

    Returns:
        (kart_id, empt_number) — оба как строки.
        Если empt_number или date_dtp отсутствуют — (None, empt_number или None).
    """
    empt_number = str(card.get("empt_number") or card.get("kart_id") or "")
    if not empt_number:
        return None, None

    # Region: последние 2 цифры reg_code
    region_part = (reg_code or "")[-2:]
    if len(region_part) < 2:
        # Для кодов региона короче 2 символов (нет таких в РФ) — паддим
        region_part = region_part.zfill(2)

    # Year: последние 2 цифры года из date_dtp
    date_dtp_str = card.get("date_dtp") or ""
    year_part = None
    if date_dtp_str:
        try:
            d = datetime.strptime(date_dtp_str, "%d.%m.%Y").date()
            year_part = str(d.year)[-2:]
        except Exception:
            year_part = None

    if not year_part:
        # Без даты ДТП kart_id сформировать нельзя — пропустим карту
        return None, empt_number

    # Seq: последние 5 цифр empt_number, padded до 5
    digits = empt_number.zfill(5)
    seq_part = digits[-5:]

    kart_id = f"{region_part}{year_part}{seq_part}"
    return kart_id, empt_number
