"""
LLM-аналитика: summary + Q&A.

- start_llm_summary() — асинхронная генерация LLM-резюме с timeout 5 мин
- _run_llm_summary_inner() — внутренняя логика summary (промпт + вызов LLM)
- ask_llm_question() — синхронный (но длительный) ответ на вопрос
- get_llm_providers_status() — статус доступности провайдеров (free/paid)

Провайдеры:
- "free" — ZhipuAI/GLM (LLM_API_KEY, дефолт)
- "paid" — DeepSeek (LLM_PAID_API_KEY/LLM_PAID_API_URL)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict

from . import _imports
from .analytics_ops import _get_cross_tables, ensure_comparison
from .models import AnalysisState, AnalysisStatus, Task
from .pipeline import ensure_prev_cards

logger = logging.getLogger(__name__)


async def start_llm_summary(task: Task, provider: str = "free") -> None:
    """
    Асинхронная генерация LLM-резюме.

    provider: "free" (ZhipuAI/GLM) или "paid" (DeepSeek).

    Внутри использует asyncio.wait_for с max duration (5 минут), чтобы
    при зависании LLM (бесконечные 5xx-ретраи, потеря соединения)
    операция гарантированно завершилась с понятной ошибкой, а не висела
    в статусе RUNNING вечно.
    """
    state = task.llm_summary_state
    state.status = AnalysisStatus.RUNNING
    state.progress = 5
    state.stage = "Подготовка данных..."
    state.started_at = datetime.now(timezone.utc)
    state.error = None
    state.result = None

    # Защита от зависания: максимум 5 минут на всю операцию.
    # Если LLM не ответил за 5 мин — что-то не так (сервис недоступен,
    # бесконечные ретраи,超大 промпт) — лучше упасть с понятной ошибкой.
    MAX_LLM_DURATION_SEC = 300

    try:
        # Запускаем реальную работу в task и ограничиваем по времени.
        # Используем shield, чтобы wait_for cancel не отменил сам task
        # (он продолжит работать в фоне, но результат уже не запишется).
        try:
            await asyncio.wait_for(
                _run_llm_summary_inner(task, provider, state),
                timeout=MAX_LLM_DURATION_SEC,
            )
        except asyncio.TimeoutError:
            elapsed = int(
                (datetime.now(timezone.utc) - state.started_at).total_seconds()
            )
            err_msg = (
                f"LLM-анализ превысил максимально допустимое время "
                f"({MAX_LLM_DURATION_SEC} сек, прошло {elapsed} сек). "
                f"Возможно, сервис нейросети перегружен или промпт слишком большой. "
                f"Попробуйте ещё раз через несколько минут или используйте "
                f"другой провайдер."
            )
            logger.error(
                f"Task {task.id}: LLM summary timeout after {elapsed}s"
            )
            state.status = AnalysisStatus.FAILED
            state.error = err_msg
            state.stage = "Превышено время ожидания"
            state.finished_at = datetime.now(timezone.utc)

    except Exception as exc:
        logger.exception(f"Task {task.id}: LLM summary failed")
        state.status = AnalysisStatus.FAILED
        state.error = str(exc)
        state.stage = "Ошибка"
        state.finished_at = datetime.now(timezone.utc)


async def _run_llm_summary_inner(
    task: Task, provider: str, state: AnalysisState,
) -> None:
    """Внутренняя логика LLM-саммари — вынесена, чтобы можно было
    обернуть в asyncio.wait_for для max duration."""
    config = _imports._import_module("config")

    # Проверяем доступность провайдера
    if provider == "paid":
        if not (config.LLM_PAID_API_KEY and config.LLM_PAID_API_URL):
            raise RuntimeError(
                "Платный LLM-провайдер не настроен "
                "(LLM_PAID_API_KEY/LLM_PAID_API_URL)"
            )
    else:
        if not config.LLM_API_KEY:
            raise RuntimeError(
                "Бесплатный LLM-провайдер не настроен (LLM_API_KEY)"
            )

    state.progress = 10
    state.stage = "Загрузка данных за прошлый год..."
    if not task.prev_cards_loaded:
        await ensure_prev_cards(task)

    state.progress = 20
    state.stage = "Расчёт сравнительных метрик..."
    comp_result = await ensure_comparison(task)
    if not comp_result.get("ok"):
        raise RuntimeError(comp_result.get("error", "Не удалось рассчитать comparison"))
    comparison = comp_result["comparison"]

    state.progress = 35
    state.stage = "Расчёт очагов ДТП для контекста..."

    # Используем готовые очаги, если уже рассчитаны
    clusters_ctx = ""
    if task.clusters_state.status == AnalysisStatus.DONE and task.clusters_state.result:
        llm_module = _imports._import_module("llm_analyzer")
        # Передаём ВСЕ очаги (а не только топ-10), чтобы format_clusters_for_prompt
        # могла разделить их по категориям (повторные/новые/исчезнувшие).
        # Раньше брали [:10] и LLM видел «солянку» из текущих и прошлых очагов.
        # Теперь метод сам сортирует и режет по max_clusters в каждой категории.
        fake_clusters = [
            {
                "road": c.get("road", ""),
                "zone_type": c.get("zone_type", ""),
                "total_accidents": c.get("total_accidents", 0),
                "deaths": c.get("deaths", 0),
                "injured": c.get("injured", 0),
                # None (смешанный тип) -> пустая строка для UI
                "dominant_type": c.get("dominant_type") or "",
                "type_counter": c.get("type_counter", {}),
                "start_pos": c.get("start_pos"),
                "end_pos": c.get("end_pos"),
                "dates": c.get("dates", []),
                # Передаём dynamics (status, prev_total, matched_prev_numbers, neighbors)
                # и флаги is_lost/is_prev_matched — по ним LLM поймёт, к какой
                # категории относится очаг.
                "dynamics": c.get("dynamics", {}),
                "_is_lost": c.get("is_lost", False),
                "_is_prev_matched": c.get("is_prev_matched", False),
            }
            for c in task.clusters_state.result.get("clusters", [])
        ]
        clusters_ctx = llm_module.format_clusters_for_prompt(
            fake_clusters, max_clusters=10,
        )

    state.progress = 50
    state.stage = "Формирование промпта..."

    llm_module = _imports._import_module("llm_analyzer")
    analytics_module = _imports._import_module("analytics")

    # Кросс-таблицы (только для бесплатного метода)
    # Phase 3.1: используем _get_cross_tables(task) — он кэширует результат
    # в task.cross_tables по id(task.cards). При повторных LLM-запросах
    # или Q&A по той же задаче — cache hit, ~0 ms вместо ~38 ms.
    cross_tables_ctx = ""
    if provider == "free":
        try:
            current_cross = _get_cross_tables(task, prev=False)
            prev_cross = _get_cross_tables(task, prev=True) if task.prev_cards else None
            cross_tables_ctx = llm_module.format_cross_tables_for_prompt(
                current_cross, prev_cross,
                task.period_label,
                task.prev_label or "",
            )
            # Этап 2: статистические метрики (severity rates, Z-score, χ²)
            stats = analytics_module.calculate_statistical_metrics(current_cross)
            stats_text = llm_module.format_statistical_metrics_for_prompt(stats)
            if stats_text and not stats_text.endswith("(недостаточно данных для статистического анализа)"):
                cross_tables_ctx += "\n\n" + stats_text
        except Exception as exc:
            logger.warning(f"Cross-tables failed: {exc}")

    state.progress = 60
    state.stage = (
        "Запрос к нейросети (15-60 сек)... "
        "Не закрывайте вкладку."
    )

    # Диагностическое логирование: размер промпта и кросс-таблиц.
    # После добавления 7 новых кросс-таблиц (БДД-факторы + профиль ТС)
    # промпт может вырасти до ~50k+ символов, что вызывает 500-е ошибки
    # у GLM-4.7-Flash. Логируем состав, чтобы видеть, какие таблицы
    # раздули промпт.
    logger.info(
        f"Task {task.id}: LLM prompt sizes — "
        f"clusters_ctx={len(clusters_ctx)} симв., "
        f"cross_tables_ctx={len(cross_tables_ctx)} симв., "
        f"provider={provider}"
    )

    # Вызываем LLM с уменьшенным числом ретраев (3 вместо 5) — для summary
    # долгие ретраи (7.5 мин) плохой UX, лучше быстро упасть и дать
    # пользователю кнопку «Повторить».
    summary = await llm_module.get_ai_summary(
        comparison=comparison,
        reg_name=task.region_name,
        current_label=task.period_label,
        prev_label=task.prev_label or "прошлый период",
        raw_supplement="",
        news_context="",
        clusters_context=clusters_ctx,
        cross_tables_context=cross_tables_ctx,
        provider=provider,
        current_cards=task.cards if provider == "paid" else None,
        prev_cards=task.prev_cards if provider == "paid" else None,
        max_retries=3,
    )

    state.result = {
        "text": summary,
        "provider": provider,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    state.status = AnalysisStatus.DONE
    state.progress = 100
    state.stage = "Готово"
    state.finished_at = datetime.now(timezone.utc)

    logger.info(f"Task {task.id}: LLM summary done ({provider})")


async def ask_llm_question(
    task: Task,
    question: str,
    provider: str = "free",
) -> Dict[str, Any]:
    """
    Синхронный (но длительный) ответ на вопрос пользователя.

    Не использует state-машину — просто вызывает LLM и возвращает ответ.
    """
    if not question or len(question.strip()) < 3:
        return {"ok": False, "error": "Слишком короткий вопрос"}

    try:
        config = _imports._import_module("config")
        if provider == "paid":
            if not (config.LLM_PAID_API_KEY and config.LLM_PAID_API_URL):
                return {"ok": False, "error": "Платный LLM не настроен"}
        else:
            if not config.LLM_API_KEY:
                return {"ok": False, "error": "Бесплатный LLM не настроен"}

        # Гарантируем comparison
        comp_result = await ensure_comparison(task)
        if not comp_result.get("ok"):
            return {"ok": False, "error": comp_result.get("error")}
        comparison = comp_result["comparison"]

        llm_module = _imports._import_module("llm_analyzer")
        analytics_module = _imports._import_module("analytics")

        # Кросс-таблицы (только для бесплатного)
        # Phase 3.1: используем кэш через _get_cross_tables — при повторных
        # Q&A по той же задаче cross_tables уже посчитаны, ~0 ms вместо ~38 ms.
        cross_tables_ctx = ""
        if provider == "free":
            try:
                current_cross = _get_cross_tables(task, prev=False)
                cross_tables_ctx = llm_module.format_cross_tables_for_prompt(
                    current_cross, None,
                    task.period_label,
                    task.prev_label or "",
                )
                # Этап 2: статистические метрики (severity rates, Z-score, χ²)
                stats = analytics_module.calculate_statistical_metrics(current_cross)
                stats_text = llm_module.format_statistical_metrics_for_prompt(stats)
                if stats_text and not stats_text.endswith("(недостаточно данных для статистического анализа)"):
                    cross_tables_ctx += "\n\n" + stats_text
            except Exception as exc:
                # Не валить весь Q&A, если кросс-таблицы упали —
                # LLM ответит на основе comparison + clusters_context.
                # Но залогировать нужно, иначе ошибка будет невидимой.
                logger.warning(
                    f"Task {task.id}: Q&A cross-tables failed: {exc}"
                )

        # Очаги (если есть)
        clusters_ctx = ""
        if task.clusters_state.status == AnalysisStatus.DONE and task.clusters_state.result:
            # Передаём ВСЕ очаги с dynamics — format_clusters_for_prompt
            # сама разделит по категориям (повторные/новые/исчезнувшие).
            fake_clusters = [
                {
                    "road": c.get("road", ""),
                    "zone_type": c.get("zone_type", ""),
                    "total_accidents": c.get("total_accidents", 0),
                    "deaths": c.get("deaths", 0),
                    "injured": c.get("injured", 0),
                    # None (смешанный тип) -> пустая строка для UI
                    "dominant_type": c.get("dominant_type") or "",
                    "type_counter": c.get("type_counter", {}),
                    "start_pos": c.get("start_pos"),
                    "end_pos": c.get("end_pos"),
                    "dates": c.get("dates", []),
                    "dynamics": c.get("dynamics", {}),
                    "_is_lost": c.get("is_lost", False),
                    "_is_prev_matched": c.get("is_prev_matched", False),
                }
                for c in task.clusters_state.result.get("clusters", [])
            ]
            clusters_ctx = llm_module.format_clusters_for_prompt(
                fake_clusters, max_clusters=10,
            )

        # Преобразуем сохранённую историю Q&A (для UI) в формат OpenAI
        # и передаём в LLM — чтобы модель понимала follow-up-вопросы.
        # Берём последние 12 сообщений (6 пар Q&A), чтобы не раздувать промпт.
        history_for_llm: list[dict[str, str]] = []
        for h in task.llm_qa_history:
            q = h.get("question", "")
            a = h.get("answer", "")
            if q:
                history_for_llm.append({"role": "user", "content": q})
            if a:
                history_for_llm.append({"role": "assistant", "content": a})
        if len(history_for_llm) > 12:
            history_for_llm = history_for_llm[-12:]

        # Диагностическое логирование: видно, доходит ли история до LLM
        hist_total_chars = sum(len(m.get("content", "")) for m in history_for_llm)
        logger.info(
            f"Task {task.id}: LLM ask — "
            f"qa_history={len(task.llm_qa_history)} records, "
            f"history_for_llm={len(history_for_llm)} msgs, "
            f"history_chars={hist_total_chars}, "
            f"provider={provider}"
        )

        answer = await llm_module.get_ai_answer(
            question=question,
            comparison=comparison,
            reg_name=task.region_name,
            current_label=task.period_label,
            prev_label=task.prev_label or "прошлый период",
            raw_supplement="",
            news_context="",
            clusters_context=clusters_ctx,
            cross_tables_context=cross_tables_ctx,
            provider=provider,
            history=history_for_llm,
        )

        # Сохраняем в историю
        task.llm_qa_history.append({
            "question": question,
            "answer": answer,
            "provider": provider,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        # Ограничиваем историю 10 записями
        if len(task.llm_qa_history) > 10:
            task.llm_qa_history = task.llm_qa_history[-10:]

        return {"ok": True, "answer": answer, "provider": provider}

    except Exception as exc:
        logger.exception(f"Task {task.id}: LLM ask failed")
        return {"ok": False, "error": str(exc)}


def get_llm_providers_status() -> Dict[str, bool]:
    """Возвращает статус доступности LLM-провайдеров."""
    try:
        config = _imports._import_module("config")
        return {
            "free": bool(config.LLM_API_KEY),
            "paid": bool(
                getattr(config, "LLM_PAID_API_KEY", None)
                and getattr(config, "LLM_PAID_API_URL", None)
            ),
            "free_model": getattr(config, "LLM_MODEL", "glm-4-flash"),
            "paid_model": getattr(config, "LLM_PAID_MODEL", "deepseek-chat"),
        }
    except Exception:
        return {"free": False, "paid": False,
                "free_model": "", "paid_model": ""}
