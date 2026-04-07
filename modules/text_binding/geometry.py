"""
DEPRECATED — используйте modules.binding.geometry напрямую.

Этот файл — шим для обратной совместимости.
Все функции делегируются в modules/binding/geometry.py.
"""

# Re-export всего API из нового модуля
from modules.binding.geometry import (  # noqa: F401
    bbox_center,
    bbox_boundary_points,
    point_to_segment_dist,
    edge_to_xy_segments,
    edge_midpoint,
    point_to_segment_dist_ex,
    bbox_to_edge_distance,
    bbox_to_edge_distance_ex,
    edge_orientation,
    edge_intersects_bbox,
    edge_length,
    bbox_to_edge_distance_weighted,
    bbox_center_to_edge_distance,
)
