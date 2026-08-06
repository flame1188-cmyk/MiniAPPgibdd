"""
bot — модульный пакет Telegram-бота для выгрузки данных ДТП с stat.gibdd.ru.

Структура (Phase 3-2, 100% pure refactoring из единого bot.py):
  bot._state           — shared state (imports, logger, globals, constants)
  bot.infra            — утилиты Telegram API (retry, safe_edit, send_long_message)
  bot.access           — контроль доступа + загрузка регионов
  bot.keyboards        — inline-клавиатуры
  bot.analysis         — конвейер аналитики и очагов (~1300 строк)
  bot.output           — HTML-вывод и карты
  bot.point_stats      — статистика по точке (геолокация)
  bot.qa               — Q&A-режим с LLM
  bot.handlers.commands     — /start /help /dtp /regions /miniapp /precache
  bot.handlers.callbacks    — on_callback_query
  bot.handlers.messages     — handle_message + _handle_document
  bot.app              — точка входа (main, _build_app, error_handler)

Совместимость: thin `bot.py` рядом с пакетом делает
    from bot.app import main; main()
— это позволяет запускать `python bot.py` как раньше, а также
`python -m bot.app`.

Все тесты (445) продолжают проходить без изменений — импорты из
модуля `bot` разрешаются через этот __init__.py.
"""
