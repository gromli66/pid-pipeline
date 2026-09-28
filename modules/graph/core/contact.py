# -*- coding: utf-8 -*-
"""contact.py — точки прикрепления труб к скиновым узлам (одна модель на систему).

Здесь живут таблицы скинов и правило «куда входит труба», общие для посадки
концов (сервер: сборка холста и раскладка; клиент: «Ручная правка» и
«Авто-выравнивание») и для выгрузки FXML. Раньше таблицы жили в
`modules/graph_to_fxml.py`, и ядро посадки не могло их взять без тяжёлого
экспортёра; тот реэкспортирует их отсюда — объекты те же.

Модель (решение заказчика 2026-09-22):
  * клапан (`ValveControl`) — ВЫВЕРЕННЫЙ порт: на двух гранях вдоль оси трубы
    труба входит в «талию бабочки» (`SKIN_CONTACT_OFFSET`; у симметричных
    клапанов сдвиг 0 — ровно центр грани). Порт жёсткий: ради прямой трубы
    не уступает;
  * прочие рамочные узлы и неосевые грани клапана — середина грани; ради
    прямой трубы уступает не больше `MID_SNAP`;
  * полигон — как прежде (посадка на контур), здесь не описывается.

Метка модели (Р1, блок 3): холст, собранный сервером по модели, несёт
`graph.canvas_transform.attach_model` == `ATTACH_MODEL` (пишет
`canvas_state.stamp`). Холст без метки — СТАРЫЙ: живёт по правилам 238fe6b,
пока его не пересоберёт сервер (открытие — канон без слотов, порты в
середине граней; выгрузка — старая поправка контрола по
`SKIN_CONTACT_OFFSET_LEGACY`). До `face_refs` признак доходит производным
ключом узла `LEGACY_KEY` (`apply_attach_mode`); в файл он не пишется
(`strip_attach_mode`).

Чистый python (stdlib), без Qt и shapely.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# class_name -> (ControlClass, SkinType, [EquipmentType])
#
# Проверено по:
#   - ControlSkinType.class  (ValveControl)
#   - PumpSkinType.class     (PumpControl)
#   - HeaterControlSkinType  (HeaterControl)
#   - DetectorSkinType       (DetectorControl)
#   - FunctionControlSkinType(FunctionControl)
#   - ButtonSkinType         (ButtonControl)
#   - IndicationSkinType     (FunctionControl с indication)
#
# 3-й элемент tuple — equipmentType (дефолт по типу оборудования):
#   MOV — электрическая задвижка
#   VLV — ручная задвижка / ручной регулятор
#   PMP — насос / вентилятор
#   CNT — регулятор (с приводом)
#   XMA — датчик
# Для классов без явного указания equipmentType не ставится.
# ---------------------------------------------------------------------------
CLASS_NAME_TO_SKIN = {
    # --- Арматура / задвижки ---
    'armatura_ruchn':       ('ValveControl', 'HANDLE_VLV', 'VLV'),
    'armatura_electro':     ('ValveControl', 'ELECTRIC_VLV', 'MOV'),
    'armatura_seroprivod':  ('ValveControl', 'AOC_VLV'),
    'armatura_membr_electro': ('ValveControl', 'ELECTROMAGNETIC_VLV'),

    # --- Клапаны ---
    'klapan_obratn':        ('ValveControl', 'CHECK_VLV'),
    'klapan_obratn_seroprivod': ('ValveControl', 'CHECK_HYDRO_VLV'),

    # --- Регуляторы ---
    'regulator_electro':    ('ValveControl', 'CTRL_VLV_ELEC', 'CNT'),
    'regulator_ruchn':      ('ValveControl', 'CTRL_VLV_HANDLE', 'VLV'),
    'regulator_seroprivod': ('ValveControl', 'AOC_CTRL_VLV', 'CNT'),

    # drossel — НЕ маппим на скин, нет аналога в библиотеке

    # --- Предохранительный клапан ---
    'predohran':            ('ValveControl', 'RELIEF_VLV'),

    # --- Насос ---
    'nasos':                ('PumpControl', 'PUMP', 'PMP'),
    'vodostruiniy_nasos':   ('PumpControl', 'PUMP', 'PMP'),

    # --- Вентилятор ---
    'ventilaytor':          ('PumpControl', 'FAN', 'PMP'),

    # --- Теплообмен / нагрев ---
    'teploobmen':           ('HeaterControl', 'HEATER_VOL_TUBE'),  # теплообменник (кожухотрубный)
    'electronagrevat':      ('HeaterControl', 'HEATER'),           # электронагреватель
    # dearator — НЕ маппим на скин, используется Polygon из segmentation

    # --- Датчики ---
    # Param/Unit НЕ ставим: тип датчика (T/P/F/L) из графа не определить,
    # остаются библиотечные дефолты ("T:" / "°C")
    'datchik':              ('DetectorControl', 'MINSK', 'XMA'),

    # --- Расходомерная шайба ---
    'rashodomernaya_shaiba': ('FunctionControl', 'FLOWMETER'),

    # --- Фильтр ---
    'filtr_meh':            ('FunctionControl', 'FILTER'),

    # --- Переход (стрелка-портал) ---
    'perehod':              ('FunctionControl', 'ARROW'),

    # --- Стрелка ---
    'strelka':              ('FunctionControl', 'ARROW'),

    # --- Выход схемы (портал-переход) ---
    'output':               ('ButtonControl', 'TEXT_BUTTON', 'TRANSITION_BUTTON'),
}

# class_name'ы которые НЕ должны маппиться на скины
SKIP_CLASS_NAMES = {
    'connector', 'voronka', 'annotation', 'truba', 'background',
}

# Контролы, которые всегда рисуются HORIZONTAL,
# независимо от ориентации трубы (датчики)
FORCE_HORIZONTAL_CONTROLS = {'DetectorControl'}

# ---------------------------------------------------------------------------
# Направление потока (от direction-классификатора) → orientation скина.
#
# `orientation` у контролов библиотеки — это BlockOrientation с 4 значениями:
#   HORIZONTAL, HORIZONTAL_REVERSE, VERTICAL, VERTICAL_REVERSE.
# Дефолт отрисовки скинов в библиотеке: вертикаль = «вверх», горизонталь =
# «вправо». Отсюда соответствие 4 направлений классификатора:
#   up    → VERTICAL          (скин как нарисован по умолчанию)
#   down  → VERTICAL_REVERSE  (разворот на 180°)
#   right → HORIZONTAL        (скин как нарисован по умолчанию)
#   left  → HORIZONTAL_REVERSE
# Если при визуальной проверке REVERSE окажется перепутан — правится здесь,
# в одном месте, после чего FXML перегенерируется.
# ---------------------------------------------------------------------------
DIRECTION_TO_ORIENTATION = {
    'up':    'VERTICAL',
    'down':  'VERTICAL_REVERSE',
    'right': 'HORIZONTAL',
    'left':  'HORIZONTAL_REVERSE',
}

# Базовая ось направления — нужна для геометрии (swap width/height и пересчёт
# layout выполняется так же, как для обычной VERTICAL-ориентации).
_DIRECTION_TO_AXIS = {
    'up': 'VERTICAL', 'down': 'VERTICAL',
    'left': 'HORIZONTAL', 'right': 'HORIZONTAL',
}

# Классы оборудования, чей скин разворачиваем по направлению классификатора.
# (Совпадает с direction_classification.classes минус napravlenie, который
#  рисуется треугольником, и strelka, исключённой из графа.)
DIRECTION_ORIENT_CLASSES = {
    'nasos', 'rashodomernaya_shaiba',
}

# Классы арматуры/регуляторов/клапанов, которые в вертикальном положении рисуем
# развёрнутыми на 180° (VERTICAL_REVERSE вместо VERTICAL). Библиотечный дефолт
# вертикали для них — «вниз», а на схемах нужен разворот. Ось (geometry.orientation)
# и swap/layout не меняются: REVERSE лишь зеркалит графику, из-за чего талия
# уезжает на 1-waist (учитывается в fxml_standardize) и знак contact-поправки
# инвертируется (apply_contact_offset). predohran (RELIEF_VLV) намеренно НЕ входит.
REVERSE_VERTICAL_CLASSES = {
    'armatura_ruchn', 'klapan_obratn', 'armatura_membr_electro',
    'regulator_ruchn', 'regulator_electro', 'armatura_electro',
    'klapan_obratn_seroprivod', 'regulator_seroprivod', 'armatura_seroprivod',
}

# ---------------------------------------------------------------------------
# Поправка оси контакта скинов (талия «бабочки»).
#
# У части скинов визуальная точка контакта (талия «бабочки») не совпадает
# с центром бокса контрола, т.к. сверху в канве скина нарисован привод
# (мотор/мембрана).
#
# Источник (Р6, 2026-09-28): замер JavaFX-рендером скинов jar 1.6.0 на
# РАБОЧЕМ размере рамки (`pretransform.FIXED_SIZES`) — общая вершина
# #leftPoly/#rightPoly относительно центра рамки:
#     python -X utf8 tools/skin_waist_probe.py [--check]
# (`--check` сверяет эту таблицу с замером; числа — MEASUREMENTS §130.2).
# Доля НЕЛИНЕЙНА по размеру рамки: верна только для рабочего размера,
# на рамке «Размеров» или детекции растра промахивается (MEASUREMENTS).
# Поля contact/waist в tools/skin_geometry.json — легаси-копии, ни с чем не
# синхронизированы; источник истины — эта таблица.
#
# Формат: skin -> (h_frac, v_frac) — ЗНАКОВАЯ доля ПОПЕРЕЧНОГО размера рамки:
#   h_frac: HORIZONTAL — талия НИЖЕ центра на h_frac*height
#           (h_frac < 0 — выше: CHECK_HYDRO_VLV);
#   v_frac: VERTICAL — после rotate(90) талия ЛЕВЕЕ центра на v_frac*width;
#           VERTICAL_REVERSE — ПРАВЕЕ (разворот на 180°); v_frac < 0 — наоборот.
# Скин клапана вне таблицы симметричен: талия в центре (сдвиг 0).
# ---------------------------------------------------------------------------
SKIN_CONTACT_OFFSET = {
    'ELECTRIC_VLV':         (0.222,  0.222),
    'ELECTROMAGNETIC_VLV':  (0.149,  0.149),
    'CTRL_VLV_ELEC':        (0.314,  0.314),
    'CTRL_VLV_HANDLE':      (0.131,  0.131),
    'CHECK_HYDRO_VLV':      (-0.041, -0.041),
    'AOC_CTRL_VLV':         (0.256,  0.256),
    'AOC_VLV':              (0.341,  0.341),
    'RELIEF_VLV':           (0.167,  0.167),
}

# Старая таблица (побайтово deploy 238fe6b, graph_to_fxml.py:223-232):
# поправка контрола при выгрузке растрового `graph_to_fxml` (рамка детекции,
# доля рабочего размера к ней неприложима) и старых холстов без метки
# модели (Р1, блок 3). НЕ МЕНЯТЬ.
# Значения откалиброваны вручную по skin_calibration_v3_ladder.fxml
SKIN_CONTACT_OFFSET_LEGACY = {
    'ELECTRIC_VLV':     (0.222, 0.20),
    'AOC_VLV':          (0.336, 0.336),
    'AOC_VLV_V2_NPP':   (0.20,  0.20),
    'CHECK_HYDRO_VLV':  (0.15,  0.15),
    'CTRL_VLV_ELEC':    (0.15,  0.15),
    'RELIEF_VLV':       (0.15,  0.15),
    'CTRL_VLV_HANDLE':  (0.10,  0.111),
    'AOC_CTRL_VLV':     (0.15,  0.111),
}

# Метка модели точек прикрепления на холсте (Р1). Правка SKIN_CONTACT_OFFSET
# меняет геометрию помеченных холстов, а is_stale её не видит: любая правка
# таблицы обязана поднять метку и `ATTACH_MODEL_TABLE_SHA` (сторож —
# tests/test_attach_mark.py).
ATTACH_MODEL = 'waist-1'
ATTACH_MODEL_TABLE_SHA = \
    '36b8889dd4c197ea94d908f325338abff58959cd547b25372c30b8e2afd6dd98'
# Производный ключ узла старого холста (метки нет): face_refs -> None.
# Ставится при каждой загрузке из метки, в файл не пишется.
LEGACY_KEY = '_attach_legacy'

# px: на сколько конец трубы на середине грани (НЕ выверенный порт) может
# уйти от середины ради прямой трубы. Решение заказчика 2026-09-22
# («допуск, если скина нет»): 0 давал на корпусе +41 мелкое колено 0.75-3 px.
MID_SNAP = 3.0

_H_SIDES = {'LEFT', 'RIGHT', 'L', 'R'}


def skin_info(node):
    """Скин по class_name: (ControlClass, SkinType[, EquipmentType]) или None.

    Без легаси-fallback по class_id: на холсте class_name есть у всех узлов.
    """
    cn = node.get('class_name', '')
    if cn in SKIP_CLASS_NAMES:
        return None
    return CLASS_NAME_TO_SKIN.get(cn)


def is_valve(node) -> bool:
    """Клапан — узел с выверенным портом (скин `ValveControl`)."""
    info = skin_info(node)
    return bool(info) and info[0] == 'ValveControl'


def orientation(node, sides=None):
    """Ось ('H'|'V') и orientation эмита — одно правило для посадки и выгрузки.

    Приоритет: датчик всегда горизонтален > направление классификатора
    (nasos/шайба) > `_axis` холста > стороны подключения рёбер (`sides` —
    'LEFT'/'RIGHT'/'TOP'/'BOTTOM' или 'L'/'R'/'T'/'B') > пропорции bbox.
    REVERSE-разворот вертикальной арматуры — поверх (только графика, ось не
    меняет).
    """
    info = skin_info(node)
    if info and info[0] in FORCE_HORIZONTAL_CONTROLS:
        return 'H', 'HORIZONTAL'
    cn = node.get('class_name')
    d = node.get('direction') or node.get('flow_direction')
    if cn in DIRECTION_ORIENT_CLASSES and d in DIRECTION_TO_ORIENTATION:
        axis = 'V' if _DIRECTION_TO_AXIS[d] == 'VERTICAL' else 'H'
        return axis, DIRECTION_TO_ORIENTATION[d]

    axis = node.get('_axis')
    if axis not in ('H', 'V'):
        sides = set(sides or ())
        if sides:
            axis = 'H' if (_H_SIDES & sides) else 'V'
        else:
            bb = node.get('bbox')
            axis = 'H' if (not bb or len(bb) != 4
                           or bb[2] - bb[0] >= bb[3] - bb[1]) else 'V'

    emit = 'HORIZONTAL' if axis == 'H' else 'VERTICAL'
    if axis == 'V' and cn in REVERSE_VERTICAL_CLASSES:
        emit = 'VERTICAL_REVERSE'
    return axis, emit


def canvas_model(graph):
    """Метка модели холста (`attach_model` в canvas_transform) или None —
    старый холст (Р1)."""
    tr = (graph.get('graph') or {}).get('canvas_transform') or {}
    return tr.get('attach_model')


def apply_attach_mode(graph) -> bool:
    """Разметить узлы по метке холста; вернуть True, если холст старый.

    Метки нет — всем узлам `LEGACY_KEY` = True (face_refs -> None: порты в
    середине граней, как в 238fe6b); метка есть — ключ снимается.
    Идемпотентна: истина — метка.
    """
    legacy = canvas_model(graph) is None
    for n in graph.get('nodes') or ():
        if legacy:
            n[LEGACY_KEY] = True
        else:
            n.pop(LEGACY_KEY, None)
    return legacy


def strip_attach_mode(nodes):
    """Снять производный ключ старого холста перед записью файла."""
    for n in nodes or ():
        n.pop(LEGACY_KEY, None)


def face_refs(node, rect, sides=None):
    """Выверенные порты клапана на гранях рамки `rect`: {side: coord} | None.

    side из {'L','R','T','B'}; coord — координата вдоль грани (y для L/R,
    x для T/B). Порт есть только у клапана и только на двух гранях вдоль оси
    трубы; остальные грани и прочие узлы — ключа нет (= середина грани).
    Доля — `SKIN_CONTACT_OFFSET` (замер на рабочем размере, Р6), знаковая.
    Соглашение о знаке то же, что у сдвига растровой выгрузки: порт минус
    центр рамки == минус сдвиг `graph_to_fxml.apply_contact_offset` при
    той же таблице (контрол едет, чтобы талия легла на трубу; здесь
    наоборот — труба входит в талию). Растровая выгрузка по умолчанию
    берёт `SKIN_CONTACT_OFFSET_LEGACY`.

    Только узел ХОЛСТА: клапан с `_axis` (его ставит `apply_fixed_sizes`,
    бокс — габарит графики скина). В координатах растра («Проверка схемы»)
    бокс — детекция, и доля талии к нему не приложима: там портов нет.

    Узел старого холста (`LEGACY_KEY`, метки модели нет) — портов нет:
    середина грани, как в 238fe6b (Р1).
    """
    if node.get(LEGACY_KEY):
        return None
    if rect is None or not is_valve(node) \
            or node.get('_axis') not in ('H', 'V'):
        return None
    x1, y1, x2, y2 = rect
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return None
    axis, emit = orientation(node, sides)
    h_frac, v_frac = SKIN_CONTACT_OFFSET.get(skin_info(node)[1], (0.0, 0.0))
    if axis == 'H':
        y = (y1 + y2) / 2.0 + h_frac * h
        return {'L': y, 'R': y}
    if emit == 'VERTICAL_REVERSE':
        x = (x1 + x2) / 2.0 + v_frac * w
    else:
        x = (x1 + x2) / 2.0 - v_frac * w
    return {'T': x, 'B': x}


def face_ref(node, rect, side, sides=None):
    """Опорная точка грани side: (coord, hard).

    hard=True — выверенный порт клапана (ради прямой не уступает);
    hard=False — середина грани (уступает не больше `MID_SNAP`).
    """
    x1, y1, x2, y2 = rect
    refs = face_refs(node, rect, sides)
    if refs and side in refs:
        return refs[side], True
    if side in ('L', 'R'):
        return (y1 + y2) / 2.0, False
    return (x1 + x2) / 2.0, False


def waist_offset(node, axis, w, h):
    """Смещение выверенного порта клапана от центра рамки w×h поперёк
    трубы (px): +вниз для H, +вправо для V; 0 — не клапан/симметричный.

    Ось задаётся явно (`axis`), узел не меняется: так её зовут и сборка
    холста (`pretransform.apply_fixed_sizes`, ось ещё не записана), и
    редактор (рамка другого размера / повёрнутая).
    """
    probe = dict(node)
    probe["_axis"] = axis
    refs = face_refs(probe, (0.0, 0.0, w, h))
    if not refs:
        return 0.0
    return (refs["L"] - h / 2.0) if axis == "H" else (refs["T"] - w / 2.0)


def waist_centroid(node, bbox):
    """Рамка задана -> центроид узла [y, x].

    Центр рамки; у клапана холста (`face_refs` не None: клапан с `_axis`)
    поперечная координата — точка талии: центроид клапана холста — талия,
    а не центр рамки (модель `apply_fixed_sizes`). Прочие узлы и растр —
    центр рамки.
    """
    x1, y1, x2, y2 = bbox
    c = [(y1 + y2) / 2, (x1 + x2) / 2]
    refs = face_refs(node, tuple(bbox))
    if refs:
        if 'L' in refs:
            c[0] = refs['L']
        else:
            c[1] = refs['T']
    return c


def box_around_waist(node, centroid, w, h):
    """Центроид = талия -> рамка w×h [x1, y1, x2, y2] (обратное к
    `waist_centroid`).

    Вдоль оси центр рамки = центроид, поперёк — центроид минус
    `waist_offset`. Не клапан или нет `_axis` — рамка вокруг центроида.
    """
    cy, cx = centroid
    by, bx = cy, cx
    axis = node.get('_axis')
    if is_valve(node) and axis in ('H', 'V'):
        off = waist_offset(node, axis, w, h)
        if axis == 'H':
            by = cy - off
        else:
            bx = cx - off
    return [bx - w / 2.0, by - h / 2.0, bx + w / 2.0, by + h / 2.0]
