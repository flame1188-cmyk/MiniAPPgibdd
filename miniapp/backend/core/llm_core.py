"""
core/llm_core.py — синхронные LLM-операции (Sprint 7, Фаза C.2).

Две публичные функции:
- run_llm_summary_sync(...) → dict {text, provider, generated_at}
- ask_llm_question_sync(...) → dict {ok, text, provider, generated_at, error}

Назначение:
  llm_analyzer.get_ai_summary / get_ai_answer_stream — async-функции.
  Celery worker (Фаза C.3) — sync, не имеет event loop.

  В core/ мы предоставляем sync-обёртки, которые:
  1. Принимают ПОДГОТОВЛЕННЫЕ данные (comparison, clusters_context, ...)
     — НЕ Task, НЕ AnalysisState.
  2. Вызывают LLM через asyncio.run().
  3. Возвращают результат как dict — Celery-таск сохранит его в Redis (C.4).

  Подготовка данных (ensure_cards, ensure_comparison, format_clusters_for_prompt,
  format_cross_tables_for_prompt) остаётся responsibility Celery-таска (C.3)
  или async-path (llm_ops.start_llm_summary).

Возвращает:
  dict — структурированный результат, сериализуемый в JSON для Redis.

Исключения:
  Любые исключения от llm_analyzer пробрасываются наверх.
  Celery-таск должен их ловить и помечать задачу как FAILED.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..services._imports import _import_module

logger = logging.getLogger(__name__)


def run_llm_summary_sync(
    comparison: Dict[str, Any],
    reg_name: str,
    current_label: str,
    prev_label: str,
    clusters_context: str = "",
    cross_tables_context: str = "",
    provider: str = "free",
    current_cards: Optional[List[Dict[str, Any]]] = None,
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    max_retries: int = 3,
    log_prefix: str = "Celery[llm-summary]",
) -> Dict[str, Any]:
    """Синхронно генерирует LLM-резюме по подготовленным данным.

    Sync-обёртка над llm_analyzer.get_ai_summary (async).

    Args:
        comparison: dict с метриками comparison (от analytics_ops.ensure_comparison).
        reg_name: Название региона (например "Республика Башкортостан").
        current_label: Метка текущего периода (например "2025 год").
        prev_label: Метка прошлого периода (например "2024 год").
        clusters_context: Готовый clusters_context (от format_clusters_for_prompt).
                          Пустая строка — без очагов.
        cross_tables_context: Готовый cross_tables_context
                              (от format_cross_tables_for_prompt).
                              Пустая строка — без кросс-таблиц.
        provider: "free" (ZhipuAI/GLM) или "paid" (DeepSeek).
        current_cards: Карточки текущего периода (только для paid-провайдера).
        prev_cards: Карточки прошлого периода (только для paid-провайдера).
        max_retries: Количество ретраев при ошибке LLM (по умолчанию 3).
        log_prefix: Префикс для логов.

    Returns:
        dict:
        {
            "text": str,             # LLM-резюме
            "provider": str,         # "free" | "paid"
            "generated_at": str,     # ISO timestamp
        }

    Raises:
        RuntimeError: Если модуль llm_analyzer не найден или если вызвана
                      из running event loop.
        Любые исключения от llm_analyzer.get_ai_summary.

    Пример (Celery task):
        from miniapp.backend.core import run_llm_summary_sync

        @app.task(queue="llm")
        def llm_summary_task(comparison, reg_name, ...):
            result = run_llm_summary_sync(
                comparison=comparison,
                reg_name=reg_name,
                current_label="2025 год",
                prev_label="2024 год",
                clusters_context=clusters_ctx,
                cross_tables_context=cross_tables_ctx,
                provider="free",
            )
            return result  # {"text": "...", "provider": "free", ...}
    """
    llm_module = _import_module("llm_analyzer")

    try:
        summary = asyncio.run(
            llm_module.get_ai_summary(
                comparison=comparison,
                reg_name=reg_name,
                current_label=current_label,
                prev_label=prev_label,
                raw_supplement="",
                news_context="",
                clusters_context=clusters_context,
                cross_tables_context=cross_tables_context,
                provider=provider,
                current_cards=current_cards,
                prev_cards=prev_cards,
                max_retries=max_retries,
            )
        )
    except RuntimeError as exc:
        if "asyncio.run() cannot be called from a running event loop" in str(exc):
            raise RuntimeError(
                "run_llm_summary_sync() вызван из running event loop. "
                "Используйте llm_analyzer.get_ai_summary напрямую (await) "
                "или вызывайте из sync-контекста (Celery worker)."
            ) from exc
        raise

    result = {
        "text": summary,
        "provider": provider,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        f"{log_prefix}: LLM summary done — provider={provider}, "
        f"text={len(summary)} симв."
    )
    return result


def ask_llm_question_sync(
    question: str,
    comparison: Dict[str, Any],
    reg_name: str,
    current_label: str,
    prev_label: str,
    qa_history: Optional[List[Dict[str, str]]] = None,
    clusters_context: str = "",
    cross_tables_context: str = "",
    provider: str = "free",
    current_cards: Optional[List[Dict[str, Any]]] = None,
    prev_cards: Optional[List[Dict[str, Any]]] = None,
    log_prefix: str = "Celery[llm-qa]",
) -> Dict[str, Any]:
    """Синхронно отвечает на вопрос пользователя по подготовленным данным.

    Sync-обёртка над llm_analyzer.get_ai_answer_stream (async).
    Стриминг НЕ используется — собираем полный ответ.
    Celery-таск возвращает готовый текст, frontend показывает его сразу.

    Args:
        question: Текст вопроса пользователя.
        comparison: dict с метриками comparison.
        reg_name: Название региона.
        current_label: Метка текущего периода.
        prev_label: Метка прошлого периода.
        qa_history: История предыдущих Q&A (последние 10 пар).
                    [{"q": "...", "a": "..."}, ...]
        clusters_context: Готовый clusters_context.
        cross_tables_context: Готовый cross_tables_context.
        provider: "free" или "paid".
        current_cards: Карточки текущего периода (для paid).
        prev_cards: Карточки прошлого периода (для paid).
        log_prefix: Префикс для логов.

    Returns:
        dict:
        {
            "ok": bool,
            "text": str | None,      # ответ LLM (если ok=True)
            "provider": str,         # "free" | "paid"
            "generated_at": str,     # ISO timestamp
            "error": str | None,     # сообщение об ошибке (если ok=False)
        }

    Raises:
        RuntimeError: Если модуль llm_analyzer не найден или если вызвана
                      из running event loop.

    Пример (Celery task):
        from miniapp.backend.core import ask_llm_question_sync

        @app.task(queue="llm")
        def llm_qa_task(question, comparison, qa_history, ...):
            return ask_llm_question_sync(
                question=question,
                comparison=comparison,
                ...
            )
    """
    if not question or len(question.strip()) < 3:
        return {
            "ok": False,
            "text": None,
            "provider": provider,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": "Слишком короткий вопрос",
        }

    llm_module = _import_module("llm_analyzer")

    async def _collect_stream() -> str:
        """Собирает полный ответ из стрима."""
        chunks: List[str] = []
        async for delta in llm_module.get_ai_answer_stream(
            question=question,
            comparison=comparison,
            reg_name=reg_name,
            current_label=current_label,
            prev_label=prev_label,
            qa_history=qa_history or [],
            clusters_context=clusters_context,
            cross_tables_context=cross_tables_context,
            provider=provider,
            current_cards=current_cards,
            prev_cards=prev_cards,
        ):
            chunks.append(delta)
        return "".join(chunks)

    try:
        answer_text = asyncio.run(_collect_stream())
    except RuntimeError as exc:
        if "asyncio.run() cannot be called from a running event loop" in str(exc):
            raise RuntimeError(
                "ask_llm_question_sync() вызван из running event loop. "
                "Используйте llm_analyzer.get_ai_answer_stream напрямую (async for) "
                "или вызывайте из sync-контекста (Celery worker)."
            ) from exc
        raise
    except Exception as exc:
        logger.exception(f"{log_prefix}: ask_llm_question_sync failed")
        return {
            "ok": False,
            "text": None,
            "provider": provider,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }

    logger.info(
        f"{log_prefix}: LLM Q&A done — provider={provider}, "
        f"question={len(question)} симв., answer={len(answer_text)} симв."
    )
    return {
        "ok": True,
        "text": answer_text,
        "provider": provider,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }
