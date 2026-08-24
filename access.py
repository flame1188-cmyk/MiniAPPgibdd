"""bot.access — контроль доступа и загрузка регионов.

Содержит:
  • is_user_allowed
  • _get_regions / _load_regions_if_needed
  • _fetch_cards_for_period — основная функция загрузки карточек ДТП
    (API ГИБДД + web_fallback + кэш)

Выделено из единого bot.py (Phase 3-2). 100% pure.
"""
from bot._state import *
from bot.infra import _is_api_down, _mark_api_down

def is_user_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def _get_regions(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, str]]:
    """Возвращает список регионов из кэша в user_data."""
    return context.bot_data.get("regions", [])


async def _load_regions_if_needed(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, str]]:
    """Загружает справочник регионов, если ещё не загружен."""
    regions = _get_regions(context)
    if not regions:
        regions = await ensure_regions_loaded()
        context.bot_data["regions"] = regions
    return regions


async def _fetch_cards_for_period(
    dat_list: list[str],
    reg_code: str,
    log_prefix: str,
    progress_callback=None,
    notify_callback=None,
    cache_result: bool = True,
    force_refresh: bool = False,
) -> tuple[list[dict], list[str]]:
    """Загружает карточки ДТП за список месяцев с GIBDD API.

    При получении 5xx от API автоматически переключается на запасной
    метод через сайт stat.gibdd.ru (web_fallback).

    Общая функция для аналитики, очагов и точечной статистики —
    устраняет дублирование одного и того же цикла в 3 местах.

    Args:
        dat_list: Список строк в формате "m.YYYY"
        reg_code: Код региона
        log_prefix: Префикс для логов (например "Аналитика", "Очаги")
        progress_callback: Опциональная async-функция(i, total, month_name, year)
                           для обновления статуса
        notify_callback: Опциональная async-функция(str) для одноразовых
                         уведомлений пользователю (например, о переключении
                         на запасной метод)

    Returns:
        (cards, errors) — список карточек ДТП и список строк-ошибок
    """
    import httpx as _httpx

    # --- Кэш (PostgreSQL dtp_cards_cache) ---
    if not force_refresh:
        cached = await data_cache_get_async(reg_code, dat_list)
        if cached is not None:
            cards, errors = cached
            logger.info(
                f"  {log_prefix}: из кэша БД "
                f"({len(cards)} ДТП)"
            )
            return cards, errors

    # --- 🆕 L2.5: Постоянный архив (gibdd_cards) ---
    # Если запрошенные месяцы уже загружены в архив — отдаём мгновенно,
    # без обращения к stat.gibdd.ru.
    try:
        from miniapp.backend.db.archive import get_cards_from_archive
        archived = await get_cards_from_archive(reg_code, dat_list)
        if archived is not None:
            logger.info(
                f"  {log_prefix}: из архива gibdd_cards "
                f"({len(archived)} ДТП, {len(dat_list)} мес)"
            )
            # Сохраняем в кэш для следующих запросов (TTL 7 дней)
            if cache_result and archived:
                await data_cache_put_async(reg_code, dat_list, archived, [])
            return archived, []
    except Exception as e:
        logger.warning(f"  {log_prefix}: archive lookup failed: {e}, продолжаем через API")

    cards: list[dict] = []
    errors: list[str] = []

    # Если API уже помечен как недоступный — сразу на web_fallback
    if _is_api_down():
        logger.info(
            f"  {log_prefix}: API ГИБДД помечен как недоступный, "
            f"сразу на сайт ({len(dat_list)} мес)"
        )
        from web_fallback import fetch_dtp_via_web_period
        fb_cards, fb_errors = await fetch_dtp_via_web_period(
            dat_list, reg_code,
            log_prefix=f"{log_prefix} [сайт]",
            progress_callback=progress_callback,
        )
        cards.extend(fb_cards)
        errors.extend(fb_errors)
    else:
        import httpx as _httpx
        use_web_fallback = False

        for i, dat in enumerate(dat_list, start=1):
            month_num = int(dat.split(".")[0])
            month_name = MONTH_FULL.get(month_num, dat)
            year = dat.split(".")[1]

            if progress_callback:
                await progress_callback(i, len(dat_list), month_name, year)

            if not use_web_fallback:
                # --- Основной метод: API ГИБДД ---
                try:
                    api_response = await fetch_dtp_data(dat=dat, reg=reg_code, pok="1")
                    # Передаём reg_code, чтобы extract_accident_cards вычислил kart_id
                    # для каждой карточки (нужно для столбцов «Номер»/«Номер ДТП» в Excel).
                    extracted = extract_accident_cards(api_response, reg_code=reg_code)
                    cards.extend(extracted)
                    logger.info(f"  {log_prefix}: {dat} -> {len(extracted)} ДТП")
                except _httpx.HTTPStatusError as e:
                    status = e.response.status_code
                    if status >= 500:
                        # Серверная ошибка — помечаем API как недоступный,
                        # все оставшиеся месяцы через web_fallback.
                        _mark_api_down()
                        use_web_fallback = True
                        logger.warning(
                            f"  {log_prefix}: {dat} -> HTTP {status}, "
                            f"переключаюсь на запасной метод (сайт ГИБДД)"
                        )
                        if notify_callback:
                            try:
                                await notify_callback(
                                    "\u26A0\uFE0F API ГИБДД недоступен (HTTP "
                                    f"{status}).\n"
                                    "Переключаюсь на запасной метод (сайт)..."
                                )
                            except Exception:
                                pass
                        remaining_dats = [dat] + dat_list[i:]
                        from web_fallback import fetch_dtp_via_web_period
                        fb_cards, fb_errors = await fetch_dtp_via_web_period(
                            remaining_dats, reg_code,
                            log_prefix=f"{log_prefix} [сайт]",
                            progress_callback=progress_callback,
                        )
                        cards.extend(fb_cards)
                        errors.extend(fb_errors)
                        break  # fallback обработал все оставшиеся месяцы
                    elif status == 404:
                        # 404 — данные могут быть на сайте, но отсутствуют в API.
                        # Наблюдается при rate-limiting ГИБДД (API возвращает 404
                        # вместо 429 для каждого второго запроса на keep-alive).
                        # Пробуем получить этот месяц через web_fallback.
                        logger.warning(
                            f"  {log_prefix}: {dat} -> HTTP 404 от API, "
                            f"пробую через сайт ГИБДД"
                        )
                        try:
                            from web_fallback import fetch_dtp_via_web_period
                            fb_cards, fb_errors = await fetch_dtp_via_web_period(
                                [dat], reg_code,
                                log_prefix=f"{log_prefix} [сайт]",
                            )
                            cards.extend(fb_cards)
                            if fb_errors:
                                errors.extend(fb_errors)
                            else:
                                # Успешно получили через сайт — убираем ошибку
                                logger.info(
                                    f"  {log_prefix}: {dat} -> {len(fb_cards)} ДТП (через сайт)"
                                )
                        except Exception as fb_exc:  # noqa: BLE001
                            err_msg = f"{month_name} {year}: HTTP 404 (API + сайт: {fb_exc})"
                            errors.append(err_msg)
                            logger.error(
                                f"  {log_prefix}: {dat} -> ОШИБКА "
                                f"[HTTPStatusError] HTTP 404 (fallback тоже failed)"
                            )
                    elif status == 400 and "не найден" in (e.response.text or ""):
                        # 400 «reg = XXX не найден» — код региона не распознан
                        # API (например, 1167 для Севастополя). Сайт ГИБДД
                        # использует 2-значные коды и может иметь данные.
                        logger.warning(
                            f"  {log_prefix}: {dat} -> HTTP 400 (reg не найден), "
                            f"пробую через сайт ГИБДД"
                        )
                        try:
                            from web_fallback import fetch_dtp_via_web_period
                            fb_cards, fb_errors = await fetch_dtp_via_web_period(
                                [dat], reg_code,
                                log_prefix=f"{log_prefix} [сайт]",
                            )
                            cards.extend(fb_cards)
                            if fb_errors:
                                errors.extend(fb_errors)
                            else:
                                logger.info(
                                    f"  {log_prefix}: {dat} -> {len(fb_cards)} ДТП (через сайт)"
                                )
                        except Exception as fb_exc:  # noqa: BLE001
                            err_msg = f"{month_name} {year}: HTTP 400 (API + сайт: {fb_exc})"
                            errors.append(err_msg)
                            logger.error(
                                f"  {log_prefix}: {dat} -> ОШИБКА "
                                f"[HTTPStatusError] HTTP 400 (fallback тоже failed)"
                            )
                    else:
                        # Прочие 4xx — не ретраим, не переключаемся
                        err_msg = f"{month_name} {year}: {error_brief(e)}"
                        errors.append(err_msg)
                        logger.error(
                            f"  {log_prefix}: {dat} -> ОШИБКА "
                            f"[{type(e).__name__}] {error_brief(e)}"
                        )
                except ConnectionError as e:
                    # Сетевая ошибка / таймаут — переключаемся на fallback
                    _mark_api_down()
                    use_web_fallback = True
                    logger.warning(
                        f"  {log_prefix}: {dat} -> {error_brief(e)}, "
                        f"переключаюсь на запасной метод (сайт ГИБДД)"
                    )
                    if notify_callback:
                        try:
                            await notify_callback(
                                "\u26A0\uFE0F API ГИБДД недоступен "
                                f"({error_brief(e)}).\n"
                                "Переключаюсь на запасной метод (сайт)..."
                            )
                        except Exception:
                            pass
                    remaining_dats = [dat] + dat_list[i:]
                    from web_fallback import fetch_dtp_via_web_period
                    fb_cards, fb_errors = await fetch_dtp_via_web_period(
                        remaining_dats, reg_code,
                        log_prefix=f"{log_prefix} [сайт]",
                        progress_callback=progress_callback,
                    )
                    cards.extend(fb_cards)
                    errors.extend(fb_errors)
                    break  # fallback обработал все оставшиеся месяцы
                except Exception as e:
                    err_msg = f"{month_name} {year}: {error_brief(e)}"
                    errors.append(err_msg)
                    logger.error(
                        f"  {log_prefix}: {dat} -> ОШИБКА "
                        f"[{type(e).__name__}] {error_brief(e)}"
                    )

    # --- Сохраняем результат в кэш БД ---
    if cache_result and cards:
        await data_cache_put_async(reg_code, dat_list, cards, errors)

    return cards, errors


