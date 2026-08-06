"""bot.handlers.callbacks — диспетчер callback-запросов (нажатия inline-кнопок).

Содержит:
  • on_callback_query — главный диспетчер (большой if-elif по callback_data).
    В будущем может быть декомпозирован в dispatch-таблицу,
    но в рамках Phase 3-2 (100% pure) сохранён как есть.

Выделено из единого bot.py (Phase 3-2). 100% pure.
"""
from bot._state import *
from bot.access import is_user_allowed, _get_regions, _load_regions_if_needed, _fetch_cards_for_period
from bot.keyboards import build_region_keyboard, build_period_keyboard
from bot.analysis import (
    _start_fetching, _get_current_cards, _has_analytics_data, _get_card_count,
    _get_prev_cards, _build_menu_keyboard, _preload_prev_year, _offer_analysis,
    _run_analysis, _clear_analytics_data, _run_concentration_points,
)
from bot.output import _html_map_menu, _generate_and_send_dtp_map, _send_analytics_html, _send_clusters_html
from bot.point_stats import _start_point_stats, _handle_point_stats_radius, _send_point_stats_excel, _send_point_stats_html, _process_point_stats, _handle_location_message
from bot.qa import _handle_analytics_question
from bot.infra import _tg_retry, _IsDocument, _safe_edit, _send_long_message, _get_user_lock, _sanitize_error, _make_progress_bar

async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главный диспетчер callback-запросов от inline-кнопок."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.edit_message_text("У вас нет доступа к этому боту.")
        return

    data = query.data

    # Блокировка по пользователю — предотвращает гонку user_data
    # при concurrent_updates=True (быстрые двойные нажатия кнопок)
    lock = _get_user_lock(user_id)
    if lock.locked():
        # Другой callback уже обрабатывается — игнорируем
        logger.debug(f"Callback от user_id={user_id} пропущен (locked): {data}")
        return

    async with lock:
        try:
            # --- Навигация по страницам регионов ---
            if data.startswith("rp:"):
                parts = data.split(":")
                if parts[1] != "noop":
                    try:
                        page = int(parts[1])
                    except (ValueError, IndexError):
                        return
                    regions = _get_regions(context)
                    keyboard = build_region_keyboard(regions, page)
                    await _safe_edit(query, "Выберите регион:", reply_markup=keyboard,
                                     description="регионы (страница)")
                return

            # --- Выбор региона ---
            if data.startswith("r:"):
                reg_code = data[2:]
                regions = _get_regions(context)
                reg_name = "Регион " + reg_code
                for r in regions:
                    if r["code"] == reg_code:
                        reg_name = r["name"]
                        break

                # Очищаем данные предыдущего региона/аналитики,
                # чтобы не накопить тысячи карточек и OSM-полигонов в RAM.
                _old_reg = context.user_data.get("reg_code")
                _clear_analytics_data(context.user_data)
                if _old_reg and _old_reg != reg_code:
                    # Удаляем ВСЕ записи старого региона из data_cache
                    # (используем invalidate_by_region_async вместо selective
                    #  invalidate — надёжнее, не зависит от формата дат.
                    #  Чистит и БД, и in-memory LRU.)
                    removed = await data_cache_invalidate_region_async(_old_reg)
                    _freed = gc.collect()
                    logger.info(
                        f"Смена региона: {_old_reg} -> {reg_code}, "
                        f"кэш: {removed} записей удалено, gc: {_freed} объектов"
                    )

                context.user_data["reg_code"] = reg_code
                context.user_data["reg_name"] = reg_name

                # Показываем клавиатуру выбора периода
                current_year = datetime.now().year
                context.user_data["sel_year"] = current_year
                keyboard = build_period_keyboard(current_year)

                await _safe_edit(query,
                    f"Регион: {reg_name}\n\n"
                    f"Выберите период:",
                    reply_markup=keyboard,
                    description="период (выбор региона)")
                return

            # --- Выбор периода: Весь год ---
            if data.startswith("py:"):
                year = int(data[3:])
                period = ParsedPeriod(
                    months=list(range(1, 13)),
                    year=year,
                    label=f"Весь {year} год",
                )
                await _start_fetching(query, context, period)
                return

            # --- Выбор периода: Квартал ---
            if data.startswith("pq:"):
                parts = data.split(":")
                try:
                    q = int(parts[1])
                    year = int(parts[2])
                except (ValueError, IndexError):
                    return
                start = (q - 1) * 3 + 1
                end = start + 2
                period = ParsedPeriod(
                    months=list(range(start, end + 1)),
                    year=year,
                    label=f"{['I','II','III','IV'][q-1]} квартал {year} "
                          f"({MONTH_SHORT[start]}-{MONTH_SHORT[end]})",
                )
                await _start_fetching(query, context, period)
                return

            # --- Выбор периода: Полугодие ---
            if data.startswith("ph:"):
                parts = data.split(":")
                try:
                    half = int(parts[1])
                    year = int(parts[2])
                except (ValueError, IndexError):
                    return
                if half == 1:
                    months = list(range(1, 7))
                    label = f"Полугодие 1 {year} (Янв-Июн)"
                else:
                    months = list(range(7, 13))
                    label = f"Полугодие 2 {year} (Июл-Дек)"
                period = ParsedPeriod(months=months, year=year, label=label)
                await _start_fetching(query, context, period)
                return

            # --- Выбор периода: 9 месяцев ---
            if data.startswith("p9:"):
                year = int(data[3:])
                period = ParsedPeriod(
                    months=list(range(1, 10)),
                    year=year,
                    label=f"9 месяцев {year} (Янв-Сен)",
                )
                await _start_fetching(query, context, period)
                return

            # --- Выбор периода: Произвольное количество месяцев ---
            if data.startswith("pn:"):
                parts = data.split(":")
                try:
                    n = int(parts[1])
                    year = int(parts[2])
                except (ValueError, IndexError):
                    return
                months = list(range(1, n + 1))
                label = f"За {n} мес. {year} ({MONTH_SHORT[1]}-{MONTH_SHORT[n]})"
                period = ParsedPeriod(months=months, year=year, label=label)
                await _start_fetching(query, context, period)
                return

            # --- Выбор периода: Конкретный месяц ---
            if data.startswith("pm:"):
                parts = data.split(":")
                try:
                    month = int(parts[1])
                    year = int(parts[2])
                except (ValueError, IndexError):
                    return
                period = ParsedPeriod(
                    months=[month],
                    year=year,
                    label=f"{MONTH_FULL.get(month, '')} {year}",
                )
                await _start_fetching(query, context, period)
                return

            # --- Навигация по годам ---
            if data.startswith("yy:"):
                parts = data.split(":")
                if parts[1] != "noop":
                    try:
                        year = int(parts[1])
                    except (ValueError, IndexError):
                        return
                    context.user_data["sel_year"] = year
                    keyboard = build_period_keyboard(year)
                    reg_name = context.user_data.get("reg_name", "")
                    await _safe_edit(query,
                        f"Регион: {reg_name}\n\n"
                        f"Выберите период:",
                        reply_markup=keyboard,
                        description="период (навигация по годам)")
                return

            # --- Назад к регионам ---
            if data == "back":
                context.user_data.pop("reg_code", None)
                context.user_data.pop("reg_name", None)
                regions = _get_regions(context)
                keyboard = build_region_keyboard(regions, page=0)
                await _safe_edit(query, "Выберите регион:",
                                 reply_markup=keyboard, description="назад к регионам)")
                return

            # --- Запрос аналитики (без ИИ) ---
            if data == "do_analytics":
                await _run_analysis(update, context, use_llm=False)
                return

            # --- Запрос аналитики (с ИИ) — прямой вызов для бесплатного ---
            if data == "do_analytics_ai":
                await _run_analysis(update, context, use_llm=True, llm_provider="free")
                return

            # --- Выбор метода ИИ (бесплатный vs платный) ---
            if data == "choose_ai_method":
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "\U0001F490 Бесплатный (GLM, ограниченный)",
                        callback_data="do_analytics_ai",
                    )],
                    [InlineKeyboardButton(
                        "\U0001F3AF Полный (DeepSeek, 1M контекст)",
                        callback_data="do_analytics_ai_paid",
                    )],
                    [InlineKeyboardButton(
                        "\u21A9\uFE0F Назад",
                        callback_data="back_to_menu",
                    )],
                ])
                await _safe_edit(
                    query,
                    "Выберите метод анализа:\n\n"
                    "\U0001F490 <b>Бесплатный</b> — GLM-4.7-Flash (200K контекст)\n"
                    "Агрегированные данные + очаги + новости\n\n"
                    "\U0001F3AF <b>Полный</b> — DeepSeek V4 Flash (1M контекст)\n"
                    "Полные данные участников, нарушений, погодных условий\n"
                    "и дорожной обстановки по каждому ДТП",
                    reply_markup=keyboard,
                    description="выбор метода ИИ",
                )
                return

            # --- Запрос аналитики (с ИИ, платный метод) ---
            if data == "do_analytics_ai_paid":
                await _run_analysis(update, context, use_llm=True, llm_provider="paid")
                return

            # --- Расчёт очагов ДТП ---
            if data == "do_concentration":
                # Проверяем, есть ли камеры в кэше для этого региона
                reg_code = (
                    context.user_data.get("concentration_reg_code", "")
                    or context.user_data.get("reg_code", "")
                    or context.user_data.get("analytics_reg_code", "")
                )
                # Запоминаем код региона для последующей загрузки файла камер
                if reg_code:
                    context.user_data["concentration_reg_code"] = reg_code
                from camera_cache import has_cached_cameras, load_cameras_from_cache

                cached_cameras = None
                if reg_code and has_cached_cameras(reg_code):
                    cached_cameras = load_cameras_from_cache(reg_code)

                if cached_cameras:
                    # Камеры в кэше — предлагаем использовать их или загрузить новые
                    with_pk = sum(1 for c in cached_cameras if c["has_piket"])
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                f"\U0001F4F7 Использовать сохранённые ({len(cached_cameras)} камер)",
                                callback_data="cam_use_cached",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "\U0001F4E4 Загрузить новый файл",
                                callback_data="cam_ask_upload",
                            ),
                            InlineKeyboardButton(
                                "\u27A1 Без камер",
                                callback_data="cam_skip",
                            ),
                        ],
                    ])
                    await _safe_edit(query,
                        "\U0001F525 <b>Очаги ДТП</b>\n\n"
                        f"Для региона <b>{reg_code}</b> найден сохранённый файл камер:\n"
                        f"  \u2022 Всего: {len(cached_cameras)}\n"
                        f"  \u2022 С пикетажем: {with_pk}\n\n"
                        "Использовать его или загрузить новый?",
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        description="очаги (камеры в кэше)")
                else:
                    # Камер в кэше нет — просим загрузить
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "\U0001F4F7 Загрузить камеры",
                                callback_data="cam_ask_upload",
                            ),
                            InlineKeyboardButton(
                                "\u27A1 Без камер",
                                callback_data="cam_skip",
                            ),
                        ],
                    ])
                    await _safe_edit(query,
                        "\U0001F525 <b>Очаги ДТП</b>\n\n"
                        "Загрузите файл с камерами фотовидеофиксации\n"
                        "(gibddrf_cameras_change_*.xls)\n"
                        "или продолжите без камер.",
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        description="очаги (без камер)")
                return

            # --- Камеры: пропустить ---
            if data == "cam_skip":
                context.user_data.pop("cameras_data", None)
                await _safe_edit(query,
                    "\U0001F525 Запуск расчёта очагов (без камер)...",
                    description="очаги (старт без камер)")
                await _run_concentration_points(update, context)
                return

            # --- Камеры: использовать сохранённые из кэша ---
            if data == "cam_use_cached":
                from camera_cache import load_cameras_from_cache
                reg_code = (
                    context.user_data.get("concentration_reg_code", "")
                    or context.user_data.get("reg_code", "")
                    or context.user_data.get("analytics_reg_code", "")
                )
                cameras = load_cameras_from_cache(reg_code) if reg_code else None
                if cameras:
                    context.user_data["cameras_data"] = cameras
                    await _safe_edit(query,
                        f"\U0001F525 Запуск расчёта очагов "
                        f"(с сохранёнными камерами: {len(cameras)})...",
                        description="очаги (старт с камерами)")
                    await _run_concentration_points(update, context)
                else:
                    await _safe_edit(query,
                        "\u26A0\uFE0F Файл камер не найден. Загрузите заново.",
                        description="очаги (камеры не найдены)")
                return

            # --- Камеры: запрос загрузки ---
            if data == "cam_ask_upload":
                context.user_data["waiting_camera_file"] = True
                await _safe_edit(query,
                    "\U0001F4F7 <b>Загрузка камер</b>\n\n"
                    "Отправьте Excel-файл с камерами\n"
                    "(gibddrf_cameras_change_*.xlsx)\n\n"
                    "Или нажмите \u274C чтобы пропустить.",
                    parse_mode="HTML",
                    description="камеры (просьба загрузить)")
                return

            # --- Статистика по точке ---
            if data == "do_point_stats":
                await _start_point_stats(update, context)
                return

            # --- HTML-карта ДТП ---
            if data == "do_html_map":
                await _html_map_menu(update, context)
                return

            if data == "html_map_dtp_only":
                await _generate_and_send_dtp_map(update, context, cameras=None)
                return

            if data == "html_map_ask_cameras":
                context.user_data["waiting_camera_for_map"] = True
                await _safe_edit(query,
                    "\U0001F5FA <b>Карта ДТП + камеры</b>\n\n"
                    "Отправьте Excel-файл с реестром камер\n"
                    "(gibddrf_cameras_change_*.xlsx)\n\n"
                    "Или нажмите \u274C чтобы пропустить.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("\u274C Без камер", callback_data="html_map_dtp_only"),
                    ]]),
                    description="карта (просьба загрузить камеры)")
                return

            # --- Смена радиуса статистики по точке ---
            if data.startswith("ps_radius:"):
                try:
                    radius_m = int(data.split(":")[1])
                except (ValueError, IndexError):
                    return
                await _handle_point_stats_radius(update, context, radius_m)
                return

            # --- Выгрузка ДТП по точке в Excel ---
            if data == "ps_excel":
                await _send_point_stats_excel(update, context)
                return

            # --- HTML-карта по точке ---
            if data == "ps_html_map":
                await _send_point_stats_html(update, context)
                return

            # --- Сменить данные (очистка + /dtp) ---
            if data == "change_data":
                # Полная очистка памяти при смене региона.
                # 1) Удаляем карточки старого региона из глобального data_cache
                #    (это критично — analytics_cards и data_cache ссылаются
                #    на одни и те же dict-объекты, поэтому очистка user_data
                #    без очистки кэша НЕ освобождает память).
                _old_reg_for_cache = context.user_data.get("reg_code")
                _mem_before = _log_memory("Смена данных: ПАМЯТЬ ДО очистки")

                if _old_reg_for_cache:
                    removed = await data_cache_invalidate_region_async(_old_reg_for_cache)
                    logger.info(
                        f"Смена данных: из кэша удалено {removed} записей региона {_old_reg_for_cache}"
                    )
                else:
                    # reg_code отсутствует — очищаем весь in-memory кэш на всякий случай.
                    # (БД не трогаем — там могут быть валидные записи других пользователей.)
                    data_cache.clear()
                    logger.info("Смена данных: reg_code отсутствует, кэш полностью очищен")

                # 2) Очищаем user_data (карточки, полигоны, очаги, etc.)
                _clear_analytics_data(context.user_data)

                # 3) Логируем память после очистки
                _log_memory("Смена данных: ПАМЯТЬ ПОСЛЕ очистки")
                regions = _get_regions(context)
                keyboard = build_region_keyboard(regions, page=0)
                await _safe_edit(query, "Выберите регион:",
                                 reply_markup=keyboard, description="смена данных)")
                return

            # --- Возврат в главное меню ---
            if data == "back_to_menu":
                # Сбрасываем временные флаги режимов, но НЕ удаляем данные
                for key in [
                    "qa_mode", "qa_llm_provider", "qa_history",
                    "point_stats_mode",
                    "waiting_camera_file", "waiting_camera_for_map",
                ]:
                    context.user_data.pop(key, None)

                menu_text, menu_kb = _build_menu_keyboard(context)
                if menu_text and menu_kb:
                    try:
                        await _safe_edit(query, menu_text,
                                     reply_markup=menu_kb, parse_mode="HTML",
                                     description="главное меню)")
                    except Exception:
                        # Если не удалось отредактировать — отправляем новым сообщением
                        await context.bot.send_message(
                            chat_id=query.from_user.id,
                            text=menu_text, reply_markup=menu_kb, parse_mode="HTML",
                        )
                else:
                    await _safe_edit(query,
                        "Данные не найдены. Отправьте /dtp для новой выгрузки.",
                        description="меню (нет данных)")
                return

            # --- Завершить режим вопросов ---
            if data == "end_qa":
                _clear_analytics_data(context.user_data)
                await _safe_edit(query,
                    "Режим вопросов завершён.\n\nОтправьте /dtp для новой выгрузки.",
                    description="завершение QA)")
                return

            # --- Отмена ---
            if data == "cancel":
                context.user_data.clear()
                await _safe_edit(query,
                    "Отменено. Отправьте /dtp чтобы начать заново.",
                    description="отмена)")
            return

        except Exception as e:
            logger.exception(f"Ошибка в callback handler: {e}")
            try:
                await _safe_edit(query,
                    f"\u26A0\uFE0F Ошибка: {_sanitize_error(e)}",
                    description="callback ошибка)")
            except Exception:
                pass




