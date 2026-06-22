"""
ЭТАП 1: Извлечение узлов из маски.

Функции для нумерации узлов и вычисления их параметров.

ИЗМЕНЕНИЯ v2:
- Добавлена функция identify_node_by_point() для YOLO lookup
- Классификация узлов теперь через centroid lookup в YOLO
- bbox берётся из YOLO аннотации, не из слившейся маски
"""
import numpy as np
from scipy import ndimage
from typing import List, Dict, Tuple, Optional
from pathlib import Path
import json


# Имена классов из YOLO разметки (индекс = class_id)
CLASS_NAMES = {
    0: 'armatura_ruchn',
    1: 'klapan_obratn',
    2: 'regulator_ruchn',
    3: 'armatura_electro',
    4: 'regulator_electro',
    5: 'drossel',
    6: 'perehod',
    7: 'klapan_obratn_seroprivod',
    8: 'armatura_seroprivod',
    9: 'regulator_seroprivod',
    10: 'armatura_membr_electro',
    11: 'nasos',
    12: 'ventilaytor',
    13: 'predohran',
    14: 'condensatootvod',
    15: 'rashodomernaya_shaiba',
    16: 'vodostruiniy_nasos',
    17: 'teploobmen',
    18: 'zaglushka',
    19: 'gidrozatvor',
    20: 'bak',
    21: 'voronka',
    22: 'filtr_meh',
    23: 'separator',
    24: 'kapleulov',
    25: 'celindr_turb',
    26: 'redukcion_ustr',
    27: 'bistro_redukc_ustr',
    28: 'separator_paro',
    29: 'dearator',
    30: 'silfonnii_kompensator',
    31: 'electronagrevat',
    32: 'smotrowoe_steclo',
    33: 'datchik',
    34: 'annotation',
    35: 'output',
    36: 'truba',
    37: 'unknow',
    38: 'strelka',
    39: 'background',
    40: 'napravlenie',
    41: 'vozdushnik',
}

# Специальные классы
CONNECTOR_CLASS_ID = -1
CONNECTOR_CLASS_NAME = 'connector'
UNKNOWN_CLASS_ID = 37
UNKNOWN_CLASS_NAME = 'unknow'

# Классы которые исключаем из поиска (annotation, truba, strelka, background).
# napravlenie (40) и vozdushnik (41) — узлы графа, НЕ исключаются.
EXCLUDED_CLASS_IDS = {34, 36, 38, 39}


def point_in_polygon(point: Tuple[int, int], polygon: List[float]) -> bool:
    """
    Проверка принадлежности точки полигону (ray casting algorithm).

    NOTE: currently unused -- identify_node_by_point uses bbox-only check.
    Kept for future use when segmentation quality justifies polygon matching.
    Args:
        point: (x, y) координаты точки
        polygon: Список координат [x1, y1, x2, y2, ...] (плоский список)
        
    Returns:
        True если точка внутри полигона
    """
    x, y = point
    n = len(polygon) // 2
    inside = False
    
    # Преобразовать плоский список в пары точек
    j = n - 1
    for i in range(n):
        xi = polygon[i * 2]
        yi = polygon[i * 2 + 1]
        xj = polygon[j * 2]
        yj = polygon[j * 2 + 1]
        
        # Ray casting: луч из точки вправо пересекает ребро?
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        
        j = i
    
    return inside


def load_coco_annotations(coco_path: str, image_filename: str, image_shape: Tuple[int, int]) -> List[Dict]:
    """
    Загрузить аннотации из COCO JSON для конкретного изображения.
    
    Args:
        coco_path: Путь к COCO JSON файлу
        image_filename: Имя файла изображения
        image_shape: (height, width) изображения
        
    Returns:
        Список словарей с bbox и class_id
    """
    labels = []
    
    coco_path = Path(coco_path)
    if not coco_path.exists():
        print(f"  Предупреждение: COCO файл не найден: {coco_path}")
        return labels
    
    with open(coco_path, 'r', encoding='utf-8') as f:
        coco = json.load(f)
    
    image_id = None
    for img in coco.get('images', []):
        if img.get('file_name') == image_filename:
            image_id = img['id']
            break
    
    if image_id is None:
        print(f"  Предупреждение: Изображение '{image_filename}' не найдено в COCO")
        return labels
    
    for idx, ann in enumerate(coco.get('annotations', [])):
        if ann.get('image_id') != image_id:
            continue
        
        bbox_coco = ann.get('bbox', [])
        if len(bbox_coco) < 4:
            continue
        
        x, y, w, h = bbox_coco
        x_min = int(x)
        y_min = int(y)
        x_max = int(x + w)
        y_max = int(y + h)
        
        category_id = ann.get('category_id', 0)
        class_id = category_id - 1
        
        # Загружаем segmentation если есть
        segmentation = ann.get('segmentation', [])
        # Проверяем что это список полигонов (не RLE)
        if segmentation and isinstance(segmentation, list) and len(segmentation) > 0:
            # Берём первый полигон если их несколько
            polygon = segmentation[0] if isinstance(segmentation[0], list) else None
        else:
            polygon = None
        
        labels.append({
            'idx': ann['id'],  # Используем id из COCO, не enumerate idx
            'class_id': class_id,
            'class_name': CLASS_NAMES.get(class_id, UNKNOWN_CLASS_NAME),
            'bbox': (x_min, y_min, x_max, y_max),
            'center': (int(x + w/2), int(y + h/2)),
            'segmentation': polygon,  # Добавляем полигон
            'attributes': ann.get('attributes', {}),  # direction и пр. (для direction-узлов)
        })
    
    return labels


def _find_nearest_mask_point(y: int, x: int, equipment_mask: np.ndarray, 
                              connection_mask: np.ndarray, radius: int = 3) -> Optional[Tuple[int, int]]:
    """Найти ближайший пиксель маски в радиусе."""
    for r in range(1, radius + 1):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if abs(dy) != r and abs(dx) != r:
                    continue  # Только граница квадрата
                ny, nx = y + dy, x + dx
                if 0 <= ny < equipment_mask.shape[0] and 0 <= nx < equipment_mask.shape[1]:
                    if equipment_mask[ny, nx] or connection_mask[ny, nx]:
                        return (ny, nx)
    return None


def identify_node_by_point(
    x: int, 
    y: int, 
    equipment_mask: np.ndarray,
    connection_mask: np.ndarray,
    annotations: List[Dict],
    labeled_equipment: np.ndarray = None,
    labeled_connectors: np.ndarray = None,
    exclude_classes: set = None
) -> Optional[Dict]:
    """
    Идентифицировать узел по координатам точки контакта.
    
    Логика:
    1. Если точка вне масок — ищем ближайший пиксель маски
    2. Если точка на connection_mask → возвращаем connector с label_id из labeled_connectors
    3. Если точка на equipment_mask → ищем в annotations какому bbox принадлежит
    4. Иначе → None
    
    ВАЖНО: Приоритет connector над equipment!
    Если точка попадает на обе маски, connector имеет приоритет.
    
    Args:
        x, y: Координаты точки (x, y) - НЕ (y, x)!
        equipment_mask: Бинарная маска оборудования
        connection_mask: Бинарная маска junction/connector
        annotations: Список COCO аннотаций
        labeled_equipment: Нумерованная маска equipment (отдельный слой)
        labeled_connectors: Нумерованная маска connectors (отдельный слой)
        exclude_classes: Классы для исключения (по умолчанию {34, 36, 38, 39})
        
    Returns:
        Словарь с информацией об узле или None
    """
    if exclude_classes is None:
        exclude_classes = EXCLUDED_CLASS_IDS
    
    check_x, check_y = x, y
    
    # Если точка вне масок — ищем ближайший пиксель
    if not equipment_mask[y, x] and not connection_mask[y, x]:
        nearest = _find_nearest_mask_point(y, x, equipment_mask, connection_mask)
        if nearest:
            check_y, check_x = nearest
        else:
            return None
    
    # ПРИОРИТЕТ: Проверка connector ПЕРВЫМ!
    # Это критически важно для случаев когда connector касается/перекрывается с equipment
    if connection_mask[check_y, check_x]:
        label_id = None
        if labeled_connectors is not None and labeled_connectors[check_y, check_x] > 0:
            label_id = int(labeled_connectors[check_y, check_x])

        return {
            'type': 'connector',
            'class_id': CONNECTOR_CLASS_ID,
            'class_name': CONNECTOR_CLASS_NAME,
            'bbox': None,
            'ann_idx': None,
            'label_id': label_id,
            'segmentation': None  # Connector не имеет полигона
        }
    
    # Проверка equipment
    if equipment_mask[check_y, check_x]:
        matches = []
        excluded_matches = []  # Боксы из exclude_classes
        
        for label in annotations:
            # Проверка принадлежности точки аннотации
            is_inside = False
            x_min, y_min, x_max, y_max = label['bbox']
            in_bbox = (x_min <= check_x <= x_max and y_min <= check_y <= y_max)

            if not in_bbox:
                # Точка вне bbox — точно не принадлежит этой аннотации
                continue

            # bbox check is sufficient (polygon check was always
            # overridden back to True, so effectively dead code)
            is_inside = True  # in_bbox guaranteed True here (continue above)

            if is_inside:
                if label['class_id'] in exclude_classes:
                    excluded_matches.append(label)
                else:
                    matches.append(label)

        # Если есть только excluded боксы (например, только background), то фильтруем
        if not matches and excluded_matches:
            return None

        if matches:
            if len(matches) > 1:
                # Приоритет меньшему bbox
                matches.sort(key=lambda l: (l['bbox'][2]-l['bbox'][0]) * (l['bbox'][3]-l['bbox'][1]))

            best_match = matches[0]
            # label_id из labeled_equipment
            eq_label_id = None
            if labeled_equipment is not None and labeled_equipment[check_y, check_x] > 0:
                eq_label_id = int(labeled_equipment[check_y, check_x])
            
            return {
                'type': 'equipment',
                'class_id': best_match['class_id'],
                'class_name': best_match['class_name'],
                'bbox': best_match['bbox'],
                'ann_idx': best_match['idx'],
                'label_id': eq_label_id,
                'segmentation': best_match.get('segmentation'),  # Полигон из COCO
                # Направление от direction-классификатора (up/right/down/left).
                # Используется в FXML для разворота скина (nasos/shaiba).
                'direction': (best_match.get('attributes') or {}).get('direction'),
            }
        else:
            # Полигон — unknown, bbox из маски компоненты
            comp_bbox = None
            eq_label_id = None
            if labeled_equipment is not None:
                eq_label_id = labeled_equipment[check_y, check_x]
                if eq_label_id > 0:
                    coords = np.argwhere(labeled_equipment == eq_label_id)
                    y_min, x_min = coords.min(axis=0)
                    y_max, x_max = coords.max(axis=0)
                    comp_bbox = (int(x_min), int(y_min), int(x_max), int(y_max))
                    eq_label_id = int(eq_label_id)
                else:
                    eq_label_id = None

            return {
                'type': 'equipment',
                'class_id': UNKNOWN_CLASS_ID,
                'class_name': UNKNOWN_CLASS_NAME,
                'bbox': comp_bbox,
                'ann_idx': None,
                'label_id': eq_label_id,
                'segmentation': None  # Unknown не имеет полигона
            }

    return None


def assign_node_classes(
    nodes: List[Dict],
    labeled_nodes: np.ndarray,
    connection_mask: np.ndarray,
    image_shape: Tuple[int, int],
    iou_threshold: float = 0.7,
    labels_format: str = "yolo",
    annotations_path: Optional[str] = None,
    coco_path: Optional[str] = None,
    image_filename: Optional[str] = None,
    debug: bool = False
) -> None:
    """
    Присвоить классы узлам на основе разметки (YOLO или COCO).

    НОВАЯ ЛОГИКА (v2):
    - Используем centroid узла для lookup в YOLO
    - bbox берём из YOLO, не из маски

    Args:
        nodes: Список узлов (модифицируется in-place)
        labeled_nodes: Массив с метками узлов
        connection_mask: Маска коннекторов
        image_shape: (height, width)
        iou_threshold: Не используется в v2
        labels_format: "yolo" или "coco"
        annotations_path: Путь к YOLO .txt файлу
        coco_path: Путь к COCO JSON файлу
        image_filename: Имя файла изображения (для COCO)
        debug: Выводить отладочную информацию
    """
    # Загружаем разметку
    if labels_format == "coco":
        if not coco_path or not image_filename:
            print(f"  Ошибка: для COCO формата нужны coco_path и image_filename")
            return
        labels = load_coco_annotations(coco_path, image_filename, image_shape)
        if debug:
            print(f"  Загружено COCO меток: {len(labels)}")
    else:
        if not annotations_path:
            print(f"  Ошибка: для YOLO формата нужен annotations_path")
            return
        labels = load_annotations(annotations_path, image_shape)
        if debug:
            print(f"  Загружено YOLO меток: {len(labels)}")

    stats = {'connector': 0, 'matched': 0, 'unknow': 0}

    for node in nodes:
        label_id = node['label_id']
        node_mask = (labeled_nodes == label_id)

        # Проверка 1: Пересечение с connection_mask
        overlap_with_connection = np.sum(node_mask & connection_mask)
        node_area = np.sum(node_mask)

        if node_area > 0 and (overlap_with_connection / node_area) > 0.5:
            node['class_id'] = CONNECTOR_CLASS_ID
            node['class_name'] = CONNECTOR_CLASS_NAME
            stats['connector'] += 1
            continue

        # Проверка 2: YOLO lookup по центроиду
        cy, cx = node['centroid']

        matches = []
        for label in labels:
            if label['class_id'] in EXCLUDED_CLASS_IDS:
                continue

            x_min, y_min, x_max, y_max = label['bbox']
            if x_min <= cx <= x_max and y_min <= cy <= y_max:
                matches.append(label)

        if matches:
            if len(matches) > 1:
                matches.sort(key=lambda l: (l['bbox'][2]-l['bbox'][0]) * (l['bbox'][3]-l['bbox'][1]))

            best_match = matches[0]
            node['class_id'] = best_match['class_id']
            node['class_name'] = best_match['class_name']
            node['bbox'] = list(best_match['bbox'])
            node['ann_idx'] = best_match['idx']
            stats['matched'] += 1
        else:
            node['class_id'] = UNKNOWN_CLASS_ID
            node['class_name'] = UNKNOWN_CLASS_NAME
            node['ann_idx'] = None
            stats['unknow'] += 1

    if debug:
        print(f"  Классификация узлов:")
        print(f"    Connector: {stats['connector']}")
        print(f"    Matched ({labels_format}): {stats['matched']}")
        print(f"    Unknown: {stats['unknow']}")


def _bbox_intersects(bbox1, bbox2):
    """
    Быстрая проверка пересечения двух bbox.
    bbox: (x_min, y_min, x_max, y_max)
    """
    return not (bbox1[2] < bbox2[0] or bbox2[2] < bbox1[0] or
                bbox1[3] < bbox2[1] or bbox2[3] < bbox1[1])


def _get_intersection_bbox(bbox1, bbox2):
    """Получить bbox пересечения двух bbox."""
    x_min = max(bbox1[0], bbox2[0])
    y_min = max(bbox1[1], bbox2[1])
    x_max = min(bbox1[2], bbox2[2])
    y_max = min(bbox1[3], bbox2[3])
    return (x_min, y_min, x_max, y_max)


def split_merged_nodes(
    nodes_mask: np.ndarray,
    annotations: List[Dict],
    min_overlap: float = 0.3,
    debug: bool = False
) -> np.ndarray:
    """
    Разбить слившиеся компоненты маски на отдельные узлы по YOLO bbox.

    ОПТИМИЗИРОВАННАЯ ВЕРСИЯ:
    - Использует find_objects() для быстрого получения bbox компонент
    - Проверяет пересечение bbox'ов без создания полноразмерных масок
    - Пиксельные операции только на локальных crop'ах

    Args:
        nodes_mask: Бинарная маска узлов
        annotations: Список YOLO аннотаций
        min_overlap: Минимальная доля пересечения bbox с маской (0-1)
        debug: Режим отладки

    Returns:
        Новая маска с разделёнными узлами
    """
    # Исключаем annotation (34), truba (36), output (35), strelka (38)
    excluded_classes = {34, 36, 38, 39}

    # Фильтруем YOLO labels
    filtered_labels = [l for l in annotations if l['class_id'] not in excluded_classes]

    if not filtered_labels:
        return nodes_mask

    h, w = nodes_mask.shape

    # Находим компоненты и их bbox через find_objects (быстро!)
    labeled, num_components = ndimage.label(nodes_mask)
    component_slices = ndimage.find_objects(labeled)

    if debug:
        print(f"  Разбиение слившихся узлов:")
        print(f"    Исходных компонент: {num_components}")
        print(f"    YOLO labels (filtered): {len(filtered_labels)}")

    # Создаём новую маску
    new_mask = np.zeros_like(nodes_mask)

    split_count = 0
    kept_count = 0

    for comp_id, slices in enumerate(component_slices, start=1):
        if slices is None:
            continue

        # Извлекаем bbox компоненты из slices
        y_slice, x_slice = slices
        comp_bbox = (x_slice.start, y_slice.start, x_slice.stop, y_slice.stop)

        # Находим какие YOLO bbox пересекаются с bbox компоненты
        overlapping_labels = []

        for label in filtered_labels:
            x_min, y_min, x_max, y_max = label['bbox']

            # Ограничиваем координаты размерами изображения
            label_bbox = (
                max(0, x_min),
                max(0, y_min),
                min(w, x_max),
                min(h, y_max)
            )

            # Быстрая проверка пересечения bbox'ов
            if not _bbox_intersects(comp_bbox, label_bbox):
                continue

            # Получаем область пересечения
            inter_bbox = _get_intersection_bbox(comp_bbox, label_bbox)
            ix_min, iy_min, ix_max, iy_max = inter_bbox

            if ix_max <= ix_min or iy_max <= iy_min:
                continue

            # Считаем пиксели только в области пересечения (локальный crop)
            intersection = np.sum(labeled[iy_min:iy_max, ix_min:ix_max] == comp_id)

            bbox_area = (label_bbox[2] - label_bbox[0]) * (label_bbox[3] - label_bbox[1])

            if bbox_area > 0:
                overlap_ratio = intersection / bbox_area
                if overlap_ratio >= min_overlap:
                    overlapping_labels.append((label, label_bbox, intersection, overlap_ratio))

        if len(overlapping_labels) <= 1:
            # Только один bbox или ни одного — копируем компоненту напрямую через slice
            new_mask[slices] |= (labeled[slices] == comp_id)
            kept_count += 1
        else:
            # Несколько bbox — разбиваем по bbox
            if debug:
                print(f"    Компонента {comp_id}: {len(overlapping_labels)} YOLO bbox")
                for lbl, _, inter, ratio in overlapping_labels:
                    print(f"      - {lbl['class_name']} (class {lbl['class_id']}): overlap={ratio:.2f}")

            split_count += 1

            # Для каждого bbox берём только пиксели компоненты внутри bbox
            for label, label_bbox, _, _ in overlapping_labels:
                lx_min, ly_min, lx_max, ly_max = label_bbox

                # Работаем только с локальным crop
                local_comp = (labeled[ly_min:ly_max, lx_min:lx_max] == comp_id)
                new_mask[ly_min:ly_max, lx_min:lx_max] |= local_comp

    # Перенумеруем компоненты
    new_labeled, new_count = ndimage.label(new_mask)

    if debug:
        print(f"    Компонент после разбиения: {new_count}")
        print(f"    Разбито компонент: {split_count}")

    return new_mask


def extract_nodes(nodes_mask: np.ndarray,
                 invalid_bridges_mask: np.ndarray = None,
                 annotations: List[Dict] = None,
                 split_merged: bool = True,
                 debug: bool = False) -> Tuple[np.ndarray, List[Dict]]:
    """
    Извлечь узлы из бинарной маски.

    Args:
        nodes_mask: Бинарная маска узлов
        invalid_bridges_mask: Маска невалидных мостов
        annotations: YOLO аннотации для разбиения слившихся узлов
        split_merged: Разбивать слившиеся узлы по YOLO bbox
        debug: Режим отладки
    """
    # Разбиваем слившиеся узлы если есть YOLO
    if split_merged and annotations:
        nodes_mask = split_merged_nodes(nodes_mask, annotations, debug=debug)

    labeled_nodes, num_nodes = ndimage.label(nodes_mask)

    if debug:
        print(f"Найдено узлов: {num_nodes}")

    nodes = []

    for label_id in range(1, num_nodes + 1):
        node_mask = (labeled_nodes == label_id)

        coords = np.argwhere(node_mask)
        centroid_y = int(np.mean(coords[:, 0]))
        centroid_x = int(np.mean(coords[:, 1]))
        centroid_yx = [centroid_y, centroid_x]

        area = np.sum(node_mask)

        y_min, x_min = coords.min(axis=0)
        y_max, x_max = coords.max(axis=0)
        bbox = (int(x_min), int(y_min), int(x_max), int(y_max))

        if invalid_bridges_mask is not None and np.any(node_mask & invalid_bridges_mask):
            node_type = 'invalid_bridge'
        else:
            node_type = 'junction'

        degree = 0

        node_dict = {
            'id': f'node_{label_id - 1}',
            'label_id': label_id,
            'type': node_type,
            'centroid': centroid_yx,
            'area': int(area),
            'bbox': bbox,
            'degree': degree
        }

        nodes.append(node_dict)

    return labeled_nodes, nodes


def prepare_tracing_data(skeleton: np.ndarray,
                        labeled_equipment: np.ndarray,
                        labeled_connectors: np.ndarray,
                        connector_offset: int,
                        valid_bridges_mask: np.ndarray,
                        bridge_routing: Dict,
                        dilation: int = 1,
                        direction_axis: Dict = None,
                        debug: bool = False) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Подготовить данные для трассировки рёбер.

    Args:
        skeleton: Бинарная маска скелета
        labeled_equipment: Нумерованная маска equipment (label_id: 1..N)
        labeled_connectors: Нумерованная маска connectors (label_id: 1..M)
        connector_offset: Сдвиг для label_id connectors (= N)
        valid_bridges_mask: Маска валидных мостов
        bridge_routing: Словарь routing для мостов
        dilation: Дилатация для поиска контактов
        direction_axis: {label_id: 'V'/'H'} — для боксов napravlenie ось трубы,
            которую бокс ИМЕЕТ ПРАВО цеплять. Осевые контакты → к боксу; сквозной
            перпендикуляр (обе стороны) → телепорт мимо бокса; перпендикуляр-поворот
            (одна сторона) → не цепляется (повиснет у грани → cap повесит connector).
        debug: Режим отладки
    
    Returns:
        (skeleton_cleaned, contact_map, bridge_contact_map)
        contact_map содержит label_id: 1..N для equipment, N+1..N+M для connectors
    """
    import cv2

    height, width = skeleton.shape

    # Объединённая маска узлов для очистки скелета
    all_nodes_mask = ((labeled_equipment > 0) | (labeled_connectors > 0)).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    contact_map = np.zeros((height, width), dtype=np.int32)
    total_contacts = 0

    direction_axis = direction_axis or {}
    # синтетические телепорты для сквозных перпендикуляров через бокс napravlenie
    # (как мост: труба проходит насквозь, бокса не касаясь)
    _synth_bid = (max(bridge_routing.keys()) if bridge_routing else 0) + 1000
    _synth_through = 0
    _perp_dropped = 0

    # Контакты для equipment (label_id: 1..num_equipment)
    num_equipment = labeled_equipment.max()
    for label_id in range(1, num_equipment + 1):
        node_mask = (labeled_equipment == label_id).astype(np.uint8) * 255
        dilated_node = cv2.dilate(node_mask, kernel, iterations=dilation)
        contours, _ = cv2.findContours(dilated_node, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        boundary_mask = np.zeros((height, width), dtype=np.uint8)
        cv2.drawContours(boundary_mask, contours, -1, 255, 1)
        skeleton_uint8 = (skeleton.astype(np.uint8)) * 255
        contact_mask = cv2.bitwise_and(skeleton_uint8, boundary_mask)
        contact_coords = np.argwhere(contact_mask > 0)

        axis = direction_axis.get(label_id)
        if axis is None:
            # обычное оборудование: все контакты → на узел
            for y, x in contact_coords:
                contact_map[y, x] = label_id  # Equipment: 1..N
                total_contacts += 1
            continue

        # ===== БОКС NAPRAVLENIE: цепляем только осевые трубы =====
        ys, xs = np.where(labeled_equipment == label_id)
        bx1, by1, bx2, by2 = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
        bcy, bcx = ys.mean(), xs.mean()

        def _pipe_dir(cy, cx, steps=14):
            """Направление трубы у контакта: идём по скелету наружу, держа КУРС от
            центра бокса и предпочитая прямое продолжение. Устойчиво к мостам и
            пересечениям, где жадный ход свернул бы на поперечный рукав (из-за чего
            осевая труба бокса на мосту терялась)."""
            hy, hx = cy - bcy, cx - bcx
            n = (hy * hy + hx * hx) ** 0.5 or 1.0
            hy, hx = hy / n, hx / n
            seen = {(cy, cx)}
            y0, x0 = cy, cx
            yc, xc = cy, cx
            for _ in range(steps):
                best, bscore = None, -9.0
                for dyy in (-1, 0, 1):
                    for dxx in (-1, 0, 1):
                        if dyy == 0 and dxx == 0:
                            continue
                        ny, nx = yc + dyy, xc + dxx
                        if not (0 <= ny < height and 0 <= nx < width):
                            continue
                        if not skeleton[ny, nx] or (ny, nx) in seen:
                            continue
                        seg = (dyy * dyy + dxx * dxx) ** 0.5
                        score = (dyy * hy + dxx * hx) / seg  # косинус с текущим курсом
                        if not (bx1 <= nx <= bx2 and by1 <= ny <= by2):
                            score += 0.5  # бонус за выход наружу из бокса
                        if score > bscore:
                            bscore, best = score, (ny, nx, dyy, dxx)
                if best is None or bscore < -0.5:  # резкий разворот → стоп
                    break
                ny, nx, dyy, dxx = best
                seg = (dyy * dyy + dxx * dxx) ** 0.5
                hy, hx = 0.5 * hy + 0.5 * dyy / seg, 0.5 * hx + 0.5 * dxx / seg
                nn = (hy * hy + hx * hx) ** 0.5 or 1.0
                hy, hx = hy / nn, hx / nn
                seen.add((ny, nx))
                yc, xc = ny, nx
            return ('V' if abs(yc - y0) >= abs(xc - x0) else 'H'), (yc - y0), (xc - x0)

        perp = []
        for y, x in contact_coords:
            orient, ody, odx = _pipe_dir(int(y), int(x))
            if orient == axis:
                contact_map[y, x] = label_id  # осевая труба → контакт бокса
                total_contacts += 1
            else:
                # точка СНАРУЖИ бокса на этой трубе (примыкает к выжившему скелету)
                perp.append((int(y) + ody, int(x) + odx, ody, odx))
        # перпендикуляр: группируем по поперечной координате (одна труба), и для
        # сквозной (есть выход в обе стороны) ставим ОДИН телепорт между крайними
        # точками снаружи бокса. Одиночная сторона (поворот-выход) → не трогаем →
        # труба повиснет у грани → cap_dangling_ends повесит connector снаружи.
        if perp:
            kidx = 0 if axis == 'V' else 1   # вдоль какой коорд группировать трубы
            perp.sort(key=lambda t: t[kidx])
            clusters = []
            for t in perp:
                if clusters and abs(t[kidx] - clusters[-1][-1][kidx]) <= 12:
                    clusters[-1].append(t)
                else:
                    clusters.append([t])
            for cl in clusters:
                if axis == 'V':   # перпендикуляр горизонтальный: влево(odx<0)/вправо(odx>0)
                    neg = [(py, px) for (py, px, ody, odx) in cl if odx < 0]
                    pos = [(py, px) for (py, px, ody, odx) in cl if odx > 0]
                    a = min(neg, key=lambda p: p[1]) if neg else None   # крайний левый
                    b = max(pos, key=lambda p: p[1]) if pos else None   # крайний правый
                else:             # перпендикуляр вертикальный: вверх(ody<0)/вниз(ody>0)
                    neg = [(py, px) for (py, px, ody, odx) in cl if ody < 0]
                    pos = [(py, px) for (py, px, ody, odx) in cl if ody > 0]
                    a = min(neg, key=lambda p: p[0]) if neg else None
                    b = max(pos, key=lambda p: p[0]) if pos else None
                if a and b and a != b:
                    bridge_routing[_synth_bid] = {tuple(a): 'pipe1', tuple(b): 'pipe1'}
                    _synth_bid += 1
                    _synth_through += 1
                else:
                    _perp_dropped += 1

    # Контакты для connectors (label_id: N+1..N+M)
    num_connectors = labeled_connectors.max()
    for label_id in range(1, num_connectors + 1):
        node_mask = (labeled_connectors == label_id).astype(np.uint8) * 255
        dilated_node = cv2.dilate(node_mask, kernel, iterations=dilation)
        contours, _ = cv2.findContours(dilated_node, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        boundary_mask = np.zeros((height, width), dtype=np.uint8)
        cv2.drawContours(boundary_mask, contours, -1, 255, 1)
        skeleton_uint8 = (skeleton.astype(np.uint8)) * 255
        contact_mask = cv2.bitwise_and(skeleton_uint8, boundary_mask)
        contact_coords = np.argwhere(contact_mask > 0)
        for y, x in contact_coords:
            # Connector: N+1..N+M (со сдвигом)
            contact_map[y, x] = label_id + connector_offset
            total_contacts += 1

    # ===== Мост под узлом: перенос «проглоченных» routing-точек на контакт узла =====
    # Если routing-точка моста попала ВНУТРЬ узла (напр. бокс napravlenie сидит на
    # пересечении труб), её скелет затирается и контакт узла оказывается отрезанным
    # от телепорта → труба-насквозь теряется. Переносим такую точку на ближайший
    # контакт оборудования (в радиусе), чтобы трасса от контакта телепортировалась
    # через мост — ровно как у обычного оборудования, стоящего над мостом.
    _RELOC_R = 25
    if direction_axis:
        # контакты по боксам НАПРАВЛЕНИЯ (label → его осевые контакты)
        box_contacts = {}
        for y, x in np.argwhere(contact_map > 0):
            lab = int(contact_map[y, x])
            if lab in direction_axis:
                box_contacts.setdefault(lab, []).append((int(y), int(x)))
        relocated = 0
        for bridge_id, routing in list(bridge_routing.items()):
            for p, ptype in list(routing.items()):
                py, px = p
                if not (0 <= py < height and 0 <= px < width):
                    continue
                lab_here = int(labeled_equipment[py, px])
                if lab_here not in direction_axis:
                    continue  # точка моста НЕ под боксом направления — не трогаем
                # переносим только на контакт ТОГО ЖЕ бокса (не чужого оборудования)
                best, best_d = None, _RELOC_R + 1.0
                for (cy, cx) in box_contacts.get(lab_here, []):
                    d = ((cy - py) ** 2 + (cx - px) ** 2) ** 0.5
                    if d < best_d:
                        best_d, best = d, (cy, cx)
                if best is None or best == p or best in routing:
                    continue
                del routing[p]
                routing[best] = ptype
                relocated += 1
        if debug and relocated:
            print(f"  Мост→бокс napravlenie: перенесено проглоченных точек: {relocated}")

    bridge_contact_map = np.zeros((height, width), dtype=np.int32)
    for bridge_id, routing in bridge_routing.items():
        for point, pipe_type in routing.items():
            y, x = point
            bridge_contact_map[y, x] = bridge_id

    bridge_contacts = np.sum(bridge_contact_map > 0)

    skeleton_cleaned = skeleton.copy()
    dilated_all_mask = cv2.dilate(all_nodes_mask, kernel, iterations=dilation)
    skeleton_cleaned[dilated_all_mask > 0] = False
    skeleton_cleaned[valid_bridges_mask] = False

    node_contacts_restored = 0
    contact_points_coords = np.argwhere(contact_map > 0)
    for y, x in contact_points_coords:
        skeleton_cleaned[y, x] = True
        node_contacts_restored += 1

    bridge_contacts_restored = 0
    for bridge_id, routing in bridge_routing.items():
        for point, pipe_type in routing.items():
            y, x = point
            skeleton_cleaned[y, x] = True
            bridge_contacts_restored += 1

    if debug:
        print(f"Подготовка данных для трассировки:")
        print(f"  Dilation: {dilation} iteration(s)")
        print(f"  Контакты equipment: {sum(1 for y, x in np.argwhere(contact_map > 0) if contact_map[y, x] <= connector_offset)}")
        print(f"  Контакты connectors: {sum(1 for y, x in np.argwhere(contact_map > 0) if contact_map[y, x] > connector_offset)}")
        print(f"  Контакты валидных мостов: {bridge_contacts}")
        print(f"  Контактные точки восстановлены: {node_contacts_restored}")
        print(f"  Контактные точки мостов восстановлены: {bridge_contacts_restored}")
        print(f"  Боксы napravlenie: сквозных перпендикуляров (телепорт) {_synth_through}, "
              f"поворотов-выходов (→cap) {_perp_dropped}")

    return skeleton_cleaned, contact_map, bridge_contact_map


def update_node_degrees(nodes: List[Dict], edges: List[Dict], debug: bool = False):
    """Обновить степень узлов после трассировки."""
    node_idx = {node['id']: i for i, node in enumerate(nodes)}

    for node in nodes:
        node['degree'] = 0

    for edge in edges:
        from_id = edge['from']
        to_id = edge.get('to')

        if from_id in node_idx:
            nodes[node_idx[from_id]]['degree'] += 1

        if to_id and to_id in node_idx:
            nodes[node_idx[to_id]]['degree'] += 1


def filter_isolated_connectors(
    nodes: List[Dict],
    edges: List[Dict],
    debug: bool = False
) -> Tuple[List[Dict], List[Dict], Dict]:
    """Фильтрация изолированных connector'ов."""
    adjacency = {node['id']: set() for node in nodes}

    for edge in edges:
        from_id = edge['from']
        to_id = edge.get('to')

        if to_id is None:
            continue

        if from_id in adjacency and to_id in adjacency:
            adjacency[from_id].add(to_id)
            adjacency[to_id].add(from_id)

    node_by_id = {node['id']: node for node in nodes}

    def is_connector(node_id: str) -> bool:
        node = node_by_id.get(node_id)
        return node and node.get('class_name') == 'connector'

    def is_real_node(node_id: str) -> bool:
        node = node_by_id.get(node_id)
        if not node:
            return False
        class_name = node.get('class_name')
        return class_name and class_name != 'connector'

    def has_path_to_real_node(start_id: str) -> bool:
        """Iterative BFS: проверяет есть ли путь от connector до real node."""
        from collections import deque
        visited = {start_id}
        queue = deque([start_id])
        while queue:
            current = queue.popleft()
            for neighbor_id in adjacency.get(current, set()):
                if neighbor_id in visited:
                    continue
                if is_real_node(neighbor_id):
                    return True
                if is_connector(neighbor_id):
                    visited.add(neighbor_id)
                    queue.append(neighbor_id)
        return False

    connectors_to_remove = set()

    for node in nodes:
        node_id = node['id']
        if not is_connector(node_id):
            continue

        if not has_path_to_real_node(node_id):
            connectors_to_remove.add(node_id)

    filtered_nodes = [n for n in nodes if n['id'] not in connectors_to_remove]

    filtered_edges = []
    removed_edges = 0

    for edge in edges:
        from_id = edge['from']
        to_id = edge.get('to')

        if from_id in connectors_to_remove:
            removed_edges += 1
            continue
        if to_id is not None and to_id in connectors_to_remove:
            removed_edges += 1
            continue

        filtered_edges.append(edge)

    stats = {
        'removed_connectors': len(connectors_to_remove),
        'removed_edges': removed_edges
    }

    return filtered_nodes, filtered_edges, stats
