"""
Async-safe Celery dispatch for API endpoints.

Analog of worker.utils.db_helpers.safe_dispatch, but for async context:
- accepts AsyncSession (not sync Session)
- wraps send_task in asyncio.to_thread (does not block event loop)
- supports queue parameter
- on failure: logs warning, returns None (does NOT change diagram status,
  so user can retry via UI)
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def async_safe_dispatch(
    task_name: str,
    args: list,
    queue: Optional[str] = None,
) -> Optional[str]:
    """
    Send a Celery task from an async API endpoint.

    On success returns task_id.
    On failure logs warning and returns None (status untouched).
    """
    from worker.celery_app import celery_app

    kwargs = {"args": args}
    if queue:
        kwargs["queue"] = queue

    try:
        result = await asyncio.to_thread(
            celery_app.send_task, task_name, **kwargs
        )
        logger.info("Dispatched %s -> %s", task_name, result.id)
        return result.id
    except Exception as exc:
        logger.warning(
            "Failed to dispatch %s: %s (user can retry via UI)",
            task_name,
            exc,
        )
        return None
