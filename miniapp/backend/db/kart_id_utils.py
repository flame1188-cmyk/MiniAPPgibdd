"""
Утилита формирования kart_id (17 цифр): region(2)+year(2)+month(2)+day(2)+empt_number(9).

Вынесена в отдельный модуль, чтобы:
- etl_archive.py использовал её при ETL-загрузке
- dry_run / тесты могли импортировать без зависимости от psycopg
- bot/access.py или miniapp/backend могли использовать для обратной совместимости

Структура empt_number (9 цифр, по данным ГИБДД):
    030000011
    ↑↑↑↑↑↑↑↑↑
    │└──────┘
    │  │
    │  └── код ОВВД + sequential (7 цифр)
    └──── код региона/ОВВД (2 цифры, фактическая структура варьируется)

ВАЖНО: empt_number НЕ гарантирует уникальность в рамках региона!
Эмпирически установлено, что ГИБДД может выдать одинаковый empt_number
разным ДТП в разных ОВВД одного региона (см. коллизию 11.2021 / 1103:
ЕЙСКИЙ и Г.СОЧИ оба получили empt_number=030000011).

kart_id (17 цифр):
    region(2) + year(2) + month(2) + day(2) + empt_number(9) = 17 цифр

    region  — последние 2 цифры reg_code (46 для "1146", 11 для "1103")
    year    — последние 2 цифры года из date_dtp (26 для 2026)
    month   — месяц ДТП из date_dtp (2 цифры, 01-12)
    day     — день ДТП из date_dtp (2 цифры, 01-31)
    empt    — оригинальный empt_number, дополненный нулями слева до 9 цифр

Пример:
    reg_code = "1103" (Краснодарский край)
    date_dtp = "28.11.2021"
    empt_number = "030000011"

    → region = "11"
    → year   = "21"
    → month  = "11"
    → day    = "28"
    → empt   = "030000011"  (уже 9 цифр)

    → kart_id = "11211128030000011"  (17 цифр)

Уникальность:
- Два разных ДТП гарантированно различаются хотя бы одним из: дата, адрес, ТС
- Если у двух карточек совпадают region+year+month+day+empt_number — это либо
  истинный дубль (пропуск), либо баг ГИБДД (пишем в gibdd_cards_collisions)
- Коллизии после добавления даты: ~0 на ~120k карт (оценка по текущему объёму)
"""
from datetime import datetime


def build_kart_id(card: dict, reg_code: str) -> tuple[str | None, str | None]:
    """
    Формирует 17-значный kart_id из карточки.

    Структура:
      region(2)       — последние 2 цифры reg_code (46 для "1146")
      year(2)         — последние 2 цифры года из date_dtp (26 для 2026)
      month(2)        — месяц ДТП (01-12)
      day(2)          — день ДТП (01-31)
      empt_number(9)  — оригинальный empt_number, padded до 9 цифр

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
        region_part = region_part.zfill(2)

    # Date: нужны year + month + day
    date_dtp_str = card.get("date_dtp") or ""
    year_part = None
    month_part = None
    day_part = None
    if date_dtp_str:
        try:
            d = datetime.strptime(date_dtp_str, "%d.%m.%Y").date()
            year_part = str(d.year)[-2:]
            month_part = f"{d.month:02d}"
            day_part = f"{d.day:02d}"
        except Exception:
            year_part = None

    if not year_part:
        # Без даты ДТП kart_id сформировать нельзя — пропустим карту
        return None, empt_number

    # empt_number: дополняем слева нулями до 9 цифр
    # Если длиннее 9 — берём последние 9 (на случай, если ГИБДД добавит разряды)
    empt_padded = empt_number.zfill(9)
    empt_part = empt_padded[-9:]

    kart_id = f"{region_part}{year_part}{month_part}{day_part}{empt_part}"
    return kart_id, empt_number
