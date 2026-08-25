# -*- coding: utf-8 -*-
"""Гейт пересборки графа: пока граф собирается, работа фазы B заперта.

Блок 5 «точечных болей» (2026-08-25), решение Максима: **на время сборки фаза B
блокируется**. Причина не вкусовая: `id` узлов между сборками не выживают — они
порядковые `node_N` от порядка компонент маски (`modules/graph/core/nodes.py`),
— поэтому всё, что фаза B пишет во время пересборки, через минуту повисает на
чужих узлах или исчезает вместе со старым `graph_validated.json`.

⛔ **Форма гейта — WHITELIST, а не «BUILDING и раньше».** Чёрный список
пропускает `ERROR`, а упавшая сборка уходит именно в него (замечание редтима):
после `set_diagram_error(..., "building_graph")` статус диаграммы — `error`, и
блэклист по конвейерному порядку такую диаграмму считает «поздней».

`ERROR` разбирается отдельно по `error_stage` (решение №3 редтима): упавшая
СБОРКА — 400, упавший OCR фазу B не запирает. Оператор после падения
распознавания обязан сохранить контуры, которые считал руками.

⚠ Сосед со СВОЕЙ политикой: `/api/ocr/{uid}/binding/save` (`_BINDING_SAVE_
STATUSES`, пункт 3.1в) `ERROR` не пускает вовсе — и `/binding/apply`, которому
этот же список передан, теперь тоже не пускает. Раньше передача списка ничего
не меняла (см. `graph_is_ready`), и два соседа расходились знаком.

Кто зовёт (перечень снят грепом `require_graph_ready` по `app/api/`):
`contours.py` — `/extract`, PUT `/validated`, `/auto-accept`, `/complete`,
PUT `/training`; `ocr.py` — PUT `/result`, `/validation/save`, `/recognize`,
`/binding/apply`. Отдельно `ocr.py:/start` — у него свой белый список, из
которого блок 5 убрал `BUILDING_GRAPH`.
"""
import logging

from fastapi import HTTPException

from app.models import DiagramStatus

logger = logging.getLogger(__name__)

#: Машинный признак отказа, вшитый В САМ ТЕКСТ `detail`.
#:
#: Клиент по нему отличает «граф пересобирается» от прочих 400 — и замораживает
#: буфер вкладки только на нём (`ui/tabs/save_mode.py`). Опознавать отказ по
#: русской прозе нельзя: её перепишут, и заслон ослепнет молча. Сведение
#: клиентской копии с этой держит `tests/test_graph_rebuild_gate.py`.
REBUILD_REFUSAL = "graph_rebuilding"

#: Этап, которым воркер подписывает упавшую сборку графа
#: (`worker/tasks/graph.py`; литерал заперт `tests/test_graph_build_status_gate.py`).
GRAPH_BUILD_STAGE = "building_graph"

#: Статусы, при которых граф УЖЕ СОБРАН и фазе B есть с чем работать.
#:
#: Начинается с `BUILT`, а не с `VALIDATED_GRAPH`: гейт стережёт пересборку, а
#: не право входа во вкладку (его считает клиент, `_BEAD_DEFS`). Всё, что
#: раньше, — фаза A и сама сборка; `VALIDATED_JUNCTIONS` тоже отвергается, и это
#: не перестраховка: `complete_junction_validation` ставит задачу сборки, ОСТАВЛЯЯ
#: статус `validated_junctions`, а `BUILDING_GRAPH` появляется только когда
#: воркер снимет задачу с очереди — на CPU-only сервере это минуты ожидания.
GRAPH_READY_STATUSES = (
    DiagramStatus.BUILT,
    DiagramStatus.VALIDATING_GRAPH,
    DiagramStatus.VALIDATED_GRAPH,
    DiagramStatus.EXTRACTING_CONTOURS,
    DiagramStatus.CONTOURS_EXTRACTED,
    DiagramStatus.CONTOURS_VALIDATED,
    DiagramStatus.OCR_PROCESSING,
    DiagramStatus.OCR_COMPLETED,
    DiagramStatus.OCR_BOUND,
    DiagramStatus.GENERATING_FXML,
    DiagramStatus.COMPLETED,
    # ⛔ `ERROR` здесь ЯВНО (возврат ревизии связки 3+5). Раньше ветка `ERROR`
    # отвечала ДО проверки `allowed`, и передача своего списка вырождалась
    # в no-op: `/binding/apply` при `error/ocr` отвечал 200 и писал KKS, а его
    # сосед `/binding/save` — 400, то есть ровно «применить можно, сохранить
    # нельзя», против чего его список и передавали. Теперь `allowed` решает и
    # здесь: у кого `ERROR` в списке — тот пускает (решение №3: упавший OCR
    # фазу B не запирает), у кого нет — отвергает вместе со всеми.
    DiagramStatus.ERROR,
)


def graph_is_ready(diagram, allowed=GRAPH_READY_STATUSES) -> bool:
    """Можно ли сейчас писать в граф из фазы B.

    ⛔ `ERROR` проверяется ПО ТОМУ ЖЕ `allowed`, что и всё остальное, и только
    ПОТОМ уточняется по `error_stage`. Порядок был обратным, и вызов со своим
    списком вырождался в no-op — см. комментарий у `GRAPH_READY_STATUSES`.
    """
    if diagram.status not in allowed:
        return False
    if diagram.status is DiagramStatus.ERROR:
        # Упавшая СБОРКА — 400 (графа нет); любой другой упавший этап фазу B
        # не запирает (решение №3 редтима).
        return diagram.error_stage != GRAPH_BUILD_STAGE
    return True


def require_graph_ready(diagram, allowed=GRAPH_READY_STATUSES) -> None:
    """Отбить работу фазы B, пока граф пересобирается. 400 либо ничего."""
    if graph_is_ready(diagram, allowed):
        return

    where = diagram.status.value
    if diagram.status is DiagramStatus.ERROR:
        where = f"{where}/{diagram.error_stage}"
    logger.warning(
        "гейт пересборки: отказ, статус %s", where,
        extra={"uid": str(getattr(diagram, "uid", "?")),
               "event": "rebuild_gate_refused"},
    )
    raise HTTPException(
        status_code=400,
        detail=(
            f"[{REBUILD_REFUSAL}] Граф пересобирается (статус '{where}') — "
            f"правки этой вкладки лягут на узлы, которых после сборки не будет. "
            f"Дождитесь конца сборки и откройте вкладку заново."
        ),
    )
