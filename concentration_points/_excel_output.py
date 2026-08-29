"""Excel-формatters, обогащение камерами, колонки для выгрузки."""
import logging

from ._constants import (
    CONCENTRATION_COLUMNS, DETAIL_COLUMNS, PRECLUSTER_COLUMNS,
    DYNAMICS_COLUMNS, DYNAMICS_DETAIL_COLUMNS,
)
from ._card_accessors import _get_road_name, _get_date, _get_dtp_type, _get_km_m
from analytics import _safe_int

logger = logging.getLogger(__name__)


def get_precluster_column_names() -> list[str]:
    """Названия колонок для Excel-файла предочагов."""
    return list(PRECLUSTER_COLUMNS)


def get_dynamics_column_names() -> list[str]:
    """Названия колонок для Excel-файла очагов с динамикой."""
    return list(DYNAMICS_COLUMNS)


def get_dynamics_detail_column_names() -> list[str]:
    """Названия колонок для листа детализации с динамикой."""
    return list(DYNAMICS_DETAIL_COLUMNS)


def _format_piketazh(pos: float | None) -> str:
    """Форматирует пикетаж из км.ddd в строку «КК+МММ»."""
    if pos is None:
        return ""
    km = int(pos)
    m = round((pos - km) * 1000)
    return f"{km}+{m:03d}"


def _first_last_piketazh(cards: list[dict]) -> tuple[float | None, float | None]:
    """
    Возвращает (пикетаж_первого_ДТП, пикетаж_последнего_ДТП)
    по минимальному и максимальному пикетажу среди карточек.
    """
    positions = []
    for card in cards:
        pos = _get_km_m(card)
        if pos is not None:
            positions.append(pos)
    if not positions:
        return None, None
    return min(positions), max(positions)


def get_concentration_column_names() -> list[str]:
    """Названия колонок для Excel-файла очагов."""
    return list(CONCENTRATION_COLUMNS)


def get_detail_column_names() -> list[str]:
    """Названия колонок для листа детализации ДТП в очагах."""
    return list(DETAIL_COLUMNS)


def _camera_row_fields(cluster: dict) -> dict[str, str]:
    """Формирует словарь с полями камер для строки Excel.

    Ожидает в cluster ключ "camera_match" — результат
    camera_loader.find_cameras_for_cluster().
    """
    match = cluster.get("camera_match") or {}

    # Статус покрытия
    status = match.get("status", "открыт")
    if status == "закрыт":
        status_display = "Закрыт"
    elif match.get("nearest"):
        status_display = "Открыт (есть ближайшая)"
    else:
        status_display = "Открыт"

    # Камера в очаге
    cam_in = match.get("in_cluster")
    if cam_in:
        cam_num = cam_in.get("number", "")
        cam_addr = cam_in.get("address", "")
        cam_coords = (
            f"{cam_in['lat']:.6f}, {cam_in['lon']:.6f}"
        )
    else:
        cam_num = ""
        cam_addr = ""
        cam_coords = ""

    # Ближайшая камера
    near = match.get("nearest")
    if near:
        near_num = near.get("number", "")
        near_addr = near.get("address", "")
        near_coords = f"{near['lat']:.6f}, {near['lon']:.6f}"
        near_dist = str(match.get("nearest_dist_m", ""))
    else:
        near_num = ""
        near_addr = ""
        near_coords = ""
        near_dist = ""

    return {
        "Статус покрытия камерой": status_display,
        "Камера: номер": cam_num,
        "Камера: адрес": cam_addr,
        "Камера: координаты": cam_coords,
        "Ближайшая камера: номер": near_num,
        "Ближайшая камера: адрес": near_addr,
        "Ближайшая камера: координаты": near_coords,
        "Расстояние до камеры (м)": near_dist,
    }


def enrich_clusters_with_cameras(
    clusters: list[dict],
    cameras: list[dict],
) -> None:
    """
    Обогащает кластеры результатами поиска камер.

    Модифицирует каждый кластер in-place, добавляя ключ
    "camera_match" с результатом camera_loader.find_cameras_for_cluster().
    """
    if not cameras:
        for c in clusters:
            c["camera_match"] = None
        return

    from camera_loader import find_cameras_for_cluster

    for cluster in clusters:
        cluster["camera_match"] = find_cameras_for_cluster(
            cluster, cameras,
        )

    # Статистика
    closed = sum(
        1 for c in clusters
        if (c.get("camera_match") or {}).get("status") == "закрыт"
    )
    logger.info(
        f"Камеры: {closed}/{len(clusters)} очагов закрыты "
        f"({len(cameras)} камер проверено)"
    )


def build_concentration_excel_data(
    clusters: list[dict],
) -> list[dict[str, str]]:
    """Строит данные для Excel-файла очагов концентрации ДТП."""
    rows = []

    for i, cluster in enumerate(clusters, start=1):
        # Виды ДТП
        types_parts = [
            f"{t}: {c}" for t, c in cluster["type_counter"].items()
        ]
        types_str = "; ".join(types_parts)

        # Координаты
        fc = cluster.get("first_coords")
        lc = cluster.get("last_coords")
        first_lat = f"{fc[0]:.6f}" if fc else ""
        first_lon = f"{fc[1]:.6f}" if fc else ""
        last_lat = f"{lc[0]:.6f}" if lc else ""
        last_lon = f"{lc[1]:.6f}" if lc else ""

        # Пикетаж: первое и последнее ДТП в очаге
        start_pos, end_pos = _first_last_piketazh(cluster["cards"])
        start_str = _format_piketazh(start_pos)
        end_str = _format_piketazh(end_pos)

        # Даты: первое и последнее ДТП
        dates = cluster["dates"]
        first_date = dates[0] if dates else ""
        last_date = dates[-1] if dates else ""

        zone_label = ZONE_TYPE_LABELS.get(
            cluster["zone_type"], cluster["zone_type"],
        )

        rows.append({
            "№ очага": str(i),
            "Тип зоны": zone_label,
            "Дорога/Улица": cluster["road"],
            "Пикетаж начало": start_str,
            "Пикетаж конец": end_str,
            "Широта первого ДТП": first_lat,
            "Долгота первого ДТП": first_lon,
            "Широта последнего ДТП": last_lat,
            "Долгота последнего ДТП": last_lon,
            "Кол-во ДТП": str(cluster["total_accidents"]),
            "Виды ДТП (детализация)": types_str,
            "Доминирующий вид": cluster.get("dominant_type") or "",
            "Погибло": str(cluster["deaths"]),
            "Ранено": str(cluster["injured"]),
            "Дата первого ДТП": first_date,
            "Дата последнего ДТП": last_date,
            # --- Камеры фотовидеофиксации ---
            **_camera_row_fields(cluster),
        })

    return rows


def build_precluster_excel_data(
    preclusters: list[dict],
) -> list[dict[str, str]]:
    """Строит данные для Excel-файла предочагов."""
    rows = []

    for i, pc in enumerate(preclusters, start=1):
        # Виды ДТП
        types_parts = [
            f"{t}: {c}" for t, c in pc["type_counter"].items()
        ]
        types_str = "; ".join(types_parts)

        # Координаты
        fc = pc.get("first_coords")
        lc = pc.get("last_coords")
        first_lat = f"{fc[0]:.6f}" if fc else ""
        first_lon = f"{fc[1]:.6f}" if fc else ""
        last_lat = f"{lc[0]:.6f}" if lc else ""
        last_lon = f"{lc[1]:.6f}" if lc else ""

        # Пикетаж
        start_pos, end_pos = _first_last_piketazh(pc["cards"])
        start_str = _format_piketazh(start_pos)
        end_str = _format_piketazh(end_pos)

        # Даты
        dates = pc["dates"]
        first_date = dates[0] if dates else ""
        last_date = dates[-1] if dates else ""

        zone_label = ZONE_TYPE_LABELS.get(
            pc["zone_type"], pc["zone_type"],
        )

        rows.append({
            "№ предочага": str(i),
            "Тип зоны": zone_label,
            "Дорога/Улица": pc["road"],
            "Пикетаж начало": start_str,
            "Пикетаж конец": end_str,
            "Широта первого ДТП": first_lat,
            "Долгота первого ДТП": first_lon,
            "Широта последнего ДТП": last_lat,
            "Долгота последнего ДТП": last_lon,
            "Кол-во ДТП": str(pc["total_accidents"]),
            "Виды ДТП (детализация)": types_str,
            "Доминирующий вид": pc.get("dominant_type") or "",
            "Погибло": str(pc["deaths"]),
            "Ранено": str(pc["injured"]),
            "Дата первого ДТП": first_date,
            "Дата последнего ДТП": last_date,
            "Критерий предочага": pc.get("precluster_criterion", ""),
            # --- Камеры фотовидеофиксации ---
            **_camera_row_fields(pc),
        })

    return rows


def build_concentration_detail_data(
    clusters: list[dict],
) -> list[dict[str, str]]:
    """
    Строит данные для листа детализации:
    все ДТП, попавшие в очаги, с указанием номера очага.
    """
    rows = []

    for i, cluster in enumerate(clusters, start=1):
        for card in cluster["cards"]:
            coords = _parse_coords(card)
            pos = _get_km_m(card)
            piketazh_str = _format_piketazh(pos)

            lat_str = f"{coords[0]:.6f}" if coords else ""
            lon_str = f"{coords[1]:.6f}" if coords else ""

            rows.append({
                "№ очага": str(i),
                "Дата ДТП": _get_date(card),
                "Вид ДТП": _get_dtp_type(card),
                "Дорога/Улица": _get_road_name(card),
                "Пикетаж": piketazh_str,
                "Широта": lat_str,
                "Долгота": lon_str,
                "Погибло": str(_safe_int(card.get("pog"))),
                "Ранено": str(_safe_int(card.get("ran"))),
            })

    return rows


# ========================
# Точка входа
# ========================

def _format_prev_year_field(dyn: dict) -> str:
    """
    Формирует текст для столбца «Очаг в прошлом году».

    Возвращает:
    - «Да, №5» — для повторного очага с одним прошлогодним
    - «Да, №3, №4» — для слияния 2+ прошлогодних
    - «Нет» — для новых (с соседом и без)
    - «» (пусто) — для исчезнувших и prev_matched
      (это и есть прошлогодний очаг, нечего на него ссылаться)
    """
    status = dyn.get("status", "")
    if status in ("lost", "prev_matched"):
        return ""

    matched_numbers = dyn.get("matched_prev_numbers") or []
    if not matched_numbers:
        return "Нет"

    # Фильтруем None (на случай если номер не нашёлся)
    valid_numbers = [str(n) for n in matched_numbers if n is not None]
    if not valid_numbers:
        return "Да"  # сматчилось, но номера потерялись — редкий случай

    return "Да, №" + ", №".join(valid_numbers)


def _format_matched_curr_field(dyn: dict) -> str:
    """
    Формирует текст для столбца «Повторён в текущем» (только prev_matched).

    Возвращает:
    - «№5» — один текущий
    - «№3, №4» — несколько текущих (если один АППГ-очаг разнесён
      по нескольким текущим)
    - «» — для всех остальных статусов
    """
    status = dyn.get("status", "")
    if status != "prev_matched":
        return ""

    matched_curr_numbers = dyn.get("matched_curr_numbers") or []
    valid = [str(n) for n in matched_curr_numbers if n is not None]
    if not valid:
        return ""
    return "№" + ", №".join(valid)


def _format_neighbors_field(dyn: dict) -> str:
    """
    Формирует текст для столбца «Соседние очаги (пр. период)».

    Возвращает строку вида «№3 (340м), №7 (890м)» — до 3 ближайших.
    Пустая строка для статусов, где соседей нет.
    """
    neighbors = dyn.get("neighbors") or []
    if not neighbors:
        return ""

    parts = []
    for n in neighbors:
        num = n.get("prev_number")
        dist = n.get("distance_m")
        if num is None or dist is None:
            continue
        parts.append(f"№{num} ({dist:.0f}м)")

    return ", ".join(parts)


def build_dynamics_excel_data(
    clusters: list[dict],
) -> list[dict[str, str]]:
    """
    Строит данные для Excel-файла очагов с исторической динамикой.

    Включает колонки:
    - Статус (повторный/новый/исчезнувший с подстатусами)
    - ДТП (пр. период), Изменение ДТП
    - Погибло/Ранено за прошлый период
    - Очаг в прошлом году (Да, №N / Нет)
    - Соседние очаги (пр. период) — для «новых с соседом»

    Для исчезнувших очагов показывает данные прошлого периода.
    """
    rows = []

    for i, cluster in enumerate(clusters, start=1):
        dyn = cluster.get("dynamics", {})
        raw_status = dyn.get("status", "new")
        is_lost = cluster.get("_is_lost", False)
        is_prev_matched = cluster.get("_is_prev_matched", False)
        # lost и prev_matched — это «прошлогодние» очаги, у них нет ДТП текущего периода
        is_prev_period = is_lost or is_prev_matched

        # Для слияния добавляем в метку номера слитых очагов
        if raw_status == "repeated_merged":
            nums = dyn.get("matched_prev_numbers") or []
            valid_nums = [str(n) for n in nums if n is not None]
            if valid_nums:
                status = f"Повторный (слияние №" + ", №".join(valid_nums) + ")"
            else:
                status = "Повторный (слияние)"
        elif raw_status == "prev_matched":
            # «АППГ (повторён в текущем №X)» — пользователь сразу видит,
            # в каком текущем очаге «продолжился» этот АППГ-очаг.
            curr_nums = dyn.get("matched_curr_numbers") or []
            valid_curr = [str(n) for n in curr_nums if n is not None]
            if valid_curr:
                status = "АППГ (повторён в текущем №" + ", №".join(valid_curr) + ")"
            else:
                status = "АППГ (повторён)"
        else:
            status = DYNAMICS_STATUS_LABELS.get(raw_status, "?")

        # Виды ДТП
        types_parts = [
            f"{t}: {c}" for t, c in cluster["type_counter"].items()
        ]
        types_str = "; ".join(types_parts)

        # Координаты: для lost/prev_matched показываем центр прошлого очага
        if is_prev_period:
            c = cluster.get("center")
            lat_str = f"{c[0]:.6f}" if c else ""
            lon_str = f"{c[1]:.6f}" if c else ""
        else:
            fc = cluster.get("first_coords")
            lat_str = f"{fc[0]:.6f}" if fc else ""
            lon_str = f"{fc[1]:.6f}" if fc else ""

        # Пикетаж
        start_pos, end_pos = _first_last_piketazh(cluster["cards"])
        start_str = _format_piketazh(start_pos)
        end_str = _format_piketazh(end_pos)

        # ДТП
        # Для prev_matched: текущих ДТП нет (это прошлогодний очаг),
        # показываем только prev_total.
        current_total = 0 if is_prev_period else cluster["total_accidents"]
        prev_total = dyn.get("prev_total")
        if prev_total is not None and not is_prev_period:
            delta = current_total - prev_total
            delta_str = f"{delta:+d}"
        elif is_lost and prev_total is not None:
            delta_str = f"-{prev_total}"
        elif is_prev_matched:
            # Для prev_matched изменения нет (есть текущий counterpart,
            # изменение показано в его строке).
            delta_str = ""
        else:
            delta_str = ""

        prev_total_str = str(prev_total) if prev_total is not None else ""

        # Даты
        dates = cluster.get("dates", [])
        first_date = dates[0] if dates else ""
        last_date = dates[-1] if dates else ""

        zone_label = ZONE_TYPE_LABELS.get(
            cluster["zone_type"], cluster["zone_type"],
        )

        # Новые столбцы
        prev_year_field = _format_prev_year_field(dyn)
        neighbors_field = _format_neighbors_field(dyn)
        matched_curr_field = _format_matched_curr_field(dyn)

        rows.append({
            "№ очага": str(i),
            "Статус": status,
            "Тип зоны": zone_label,
            "Дорога/Улица": cluster["road"],
            "Пикетаж начало": start_str,
            "Пикетаж конец": end_str,
            "Широта": lat_str,
            "Долгота": lon_str,
            "Кол-во ДТП": str(current_total),
            "ДТП (пр. период)": prev_total_str,
            "Изменение ДТП": delta_str,
            "Очаг в прошлом году": prev_year_field,
            "Соседние очаги (пр. период)": neighbors_field,
            "Повторён в текущем": matched_curr_field,
            "Виды ДТП (детализация)": types_str,
            "Доминирующий вид": cluster.get("dominant_type") or "",
            "Погибло": str(0 if is_prev_period else cluster["deaths"]),
            "Ранено": str(0 if is_prev_period else cluster["injured"]),
            "Погибло (пр. период)": str(dyn["prev_deaths"]) if dyn.get("prev_deaths") is not None else "",
            "Ранено (пр. период)": str(dyn["prev_injured"]) if dyn.get("prev_injured") is not None else "",
            "Дата первого ДТП": first_date,
            "Дата последнего ДТП": last_date,
        })

    return rows


def _get_card_cause_field(card: dict, field: str) -> str:
    """Извлекает одно значение причины из карточки для листа детализации.

    Для списков (npdd, ndu, factor) — объединяет через '; ' с фильтром мусора.
    Для строк (s_pch, t_n) — возвращает как есть.
    Для sop_npdd — с фильтром по ключевым словам.
    """
    dor_usl = card.get("dor_usl") or {}
    if not isinstance(dor_usl, dict):
        return ""

    if field == "npdd":
        parts = []
        for ts in (card.get("ts_info") or []):
            for uch in (ts.get("ts_uch") or []):
                for v in (uch.get("npdd") or []):
                    s = str(v).strip()
                    if s and s.lower() not in _CAUSE_SKIP_VALUES:
                        parts.append(s)
        for uch in (card.get("uch_info") or []):
            for v in (uch.get("npdd") or []):
                s = str(v).strip()
                if s and s.lower() not in _CAUSE_SKIP_VALUES:
                    parts.append(s)
        return "; ".join(parts)

    if field == "sop_npdd":
        parts = []
        for ts in (card.get("ts_info") or []):
            for uch in (ts.get("ts_uch") or []):
                for v in (uch.get("sop_npdd") or []):
                    s = str(v).strip()
                    if s and _matches_sop_filter(s):
                        parts.append(s)
        for uch in (card.get("uch_info") or []):
            for v in (uch.get("sop_npdd") or []):
                s = str(v).strip()
                if s and _matches_sop_filter(s):
                    parts.append(s)
        return "; ".join(parts)

    if field == "ndu":
        parts = []
        for item in (dor_usl.get("ndu") or []):
            s = str(item).strip()
            if s and s.lower() not in _CAUSE_SKIP_VALUES:
                parts.append(s)
        return "; ".join(parts)

    if field == "spch":
        return str(dor_usl.get("s_pch", "")).strip()

    if field == "factor":
        parts = []
        for item in (dor_usl.get("factor") or []):
            s = str(item).strip()
            if s and s.lower() not in _CAUSE_SKIP_VALUES:
                parts.append(s)
        return "; ".join(parts)

    if field == "tn":
        parts = []
        for ts in (card.get("ts_info") or []):
            s = str(ts.get("t_n", "")).strip()
            if s and s.lower() not in _CAUSE_SKIP_VALUES:
                parts.append(s)
        return "; ".join(parts)

    if field == "osv":
        return str(dor_usl.get("osv", "")).strip()

    return ""


def build_dynamics_detail_data(
    clusters: list[dict],
    current_label: str = "",
    prev_label: str = "",
) -> list[dict[str, str]]:
    """
    Строит данные для листа детализации с указанием периода и статуса.

    Для текущих очагов показывает ДТП текущего периода.
    Для исчезнувших очагов показывает ДТП прошлого периода
    с пометкой периода.
    """
    rows = []

    for i, cluster in enumerate(clusters, start=1):
        dyn = cluster.get("dynamics", {})
        status = DYNAMICS_STATUS_LABELS.get(dyn.get("status", "new"), "?")
        is_lost = cluster.get("_is_lost", False)
        is_prev_matched = cluster.get("_is_prev_matched", False)
        # lost и prev_matched — оба из прошлого периода
        period = prev_label if (is_lost or is_prev_matched) else current_label

        for card in cluster.get("cards", []):
            coords = _parse_coords(card)
            pos = _get_km_m(card)
            piketazh_str = _format_piketazh(pos)

            lat_str = f"{coords[0]:.6f}" if coords else ""
            lon_str = f"{coords[1]:.6f}" if coords else ""

            rows.append({
                "№ очага": str(i),
                "Статус": status,
                "Период": period,
                "Дата ДТП": _get_date(card),
                "Вид ДТП": _get_dtp_type(card),
                "Дорога/Улица": _get_road_name(card),
                "Пикетаж": piketazh_str,
                "Широта": lat_str,
                "Долгота": lon_str,
                "Погибло": str(_safe_int(card.get("pog"))),
                "Ранено": str(_safe_int(card.get("ran"))),
                # --- Причины ДТП ---
                "Непосредственные нарушения ПДД": _get_card_cause_field(card, "npdd"),
                "Сопутствующие нарушения (опьянение/лишение)": _get_card_cause_field(card, "sop_npdd"),
                "Недостатки ТЭС": _get_card_cause_field(card, "ndu"),
                "Состояние проезжей части": _get_card_cause_field(card, "spch"),
                "Фактор режима движения": _get_card_cause_field(card, "factor"),
                "Технические неисправности": _get_card_cause_field(card, "tn"),
                "Освещение": _get_card_cause_field(card, "osv"),
            })

    return rows


def build_dynamics_summary(clusters: list[dict]) -> dict:
    """
    Считает сводную статистику по динамике очагов.

    Returns:
        {
            "total": int,
            "new": int,
            "lost": int,
            "growing": int,
            "shrinking": int,
            "stable": int,
            "current_total_dtp": int,
            "prev_total_dtp": int,
        }
    """
    stats = {
        "total": len(clusters),
        "new": 0,
        "lost": 0,
        "growing": 0,
        "shrinking": 0,
        "stable": 0,
        "current_total_dtp": 0,
        "prev_total_dtp": 0,
    }

    for cluster in clusters:
        dyn = cluster.get("dynamics", {})
        status = dyn.get("status", "new")
        stats[status] = stats.get(status, 0) + 1

        # current_total_dtp — только для текущих очагов
        # (lost и prev_matched — это очаги прошлого периода, их ДТП
        # уже учтены в prev_total_dtp).
        if not cluster.get("_is_lost", False) and not cluster.get("_is_prev_matched", False):
            stats["current_total_dtp"] += cluster["total_accidents"]

        prev_total = dyn.get("prev_total")
        if prev_total is not None:
            stats["prev_total_dtp"] += prev_total

    return stats
