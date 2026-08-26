"""
Tests for B5 fixes: edge_key sorted, exclude connectors, new OCR format.

B5.3 — 3 обязательных теста из плана:
1. edge_key sorted (#73)
2. exclude connectors (#56)
3. new OCR format (B5.0)

Self-contained: logic extracted directly, no PySide6 dependency.
"""


# ═══════════════════════════════════════════════════════════════════
# Logic extracted from source files (pure functions, no Qt)
# ═══════════════════════════════════════════════════════════════════

def _sorted_edge_key(ek: str) -> str:
    """Нормализовать edge_key: 'B|A' → 'A|B' (sorted).
    Source: ui/tabs/ocr_binding_tab.py line 39."""
    parts = ek.split("|", 1)
    if len(parts) == 2:
        return f"{min(parts[0], parts[1])}|{max(parts[0], parts[1])}"
    return ek


def _is_connector_excluded(class_name: str) -> bool:
    """Логика исключения connectors из auto_bind.

    Source: `ui/tabs/ocr_binding_tab.py` — `_auto_bind_diameters`, СНЯТ
    26.08.2026 вместе с авто-привязкой Ø. Тест самодостаточен (логика
    воспроизведена здесь), поэтому оставлен как замок на саму форму правила.
    """
    return class_name.lower() in ("connector", "off-page connector")


def _parse_ocr(ocr_raw):
    """Парсинг OCR результата (новый формат или legacy).
    Source: ui/tabs/ocr_binding_tab.py — _on_download_finished."""
    if isinstance(ocr_raw, dict) and "target" in ocr_raw:
        blocks = ocr_raw["target"]
        secondary = ocr_raw.get("secondary", [])
    else:
        blocks = ocr_raw if isinstance(ocr_raw, list) else []
        secondary = []
    return blocks, secondary


# ═══════════════════════════════════════════════════════════════════
# Test 1: edge_key sorted (Bug #73)
# ═══════════════════════════════════════════════════════════════════

class TestSortedEdgeKey:
    """Bug #73: edge_key must always be min|max sorted."""

    def test_reversed_order_becomes_sorted(self):
        assert _sorted_edge_key("B|A") == "A|B"

    def test_already_sorted_unchanged(self):
        assert _sorted_edge_key("A|B") == "A|B"

    def test_same_nodes(self):
        assert _sorted_edge_key("X|X") == "X|X"

    def test_numeric_node_ids(self):
        assert _sorted_edge_key("node_20|node_10") == "node_10|node_20"

    def test_no_pipe_passthrough(self):
        assert _sorted_edge_key("single") == "single"

    def test_empty_string(self):
        assert _sorted_edge_key("") == ""

    def test_long_uuid_ids(self):
        a = "zzz-node-999"
        b = "aaa-node-001"
        assert _sorted_edge_key(f"{a}|{b}") == f"{b}|{a}"

    def test_idempotent(self):
        """Applying twice gives same result."""
        for ek in ["B|A", "A|B", "Z|M", "foo|bar"]:
            once = _sorted_edge_key(ek)
            twice = _sorted_edge_key(once)
            assert once == twice


# ═══════════════════════════════════════════════════════════════════
# Test 2: exclude connectors from auto_bind (Bug #56)
# ═══════════════════════════════════════════════════════════════════

class TestExcludeConnectors:
    """Bug #56: connectors must be excluded from auto_bind nodes."""

    EXCLUDED = ["connector", "Connector", "CONNECTOR",
                "off-page connector", "Off-Page Connector", "OFF-PAGE CONNECTOR"]

    INCLUDED = ["valve", "pump", "tank", "heat-exchanger",
                "equipment", "instrument", "reducer"]

    def test_connectors_excluded(self):
        for name in self.EXCLUDED:
            assert _is_connector_excluded(name), f"'{name}' should be excluded"

    def test_equipment_not_excluded(self):
        for name in self.INCLUDED:
            assert not _is_connector_excluded(name), f"'{name}' should NOT be excluded"

    def test_partial_match_not_excluded(self):
        """'connector-foo' is NOT a connector."""
        assert not _is_connector_excluded("connector-foo")

    def test_empty_string_not_excluded(self):
        assert not _is_connector_excluded("")


# ═══════════════════════════════════════════════════════════════════
# Test 3: new OCR format (B5.0) — target/secondary dict vs legacy
# ═══════════════════════════════════════════════════════════════════

class TestNewOcrFormat:
    """B5.0: new OCR format {target: [...], secondary: [...]} vs legacy list."""

    def test_new_format_parsed(self):
        ocr_raw = {
            "target": [
                {"bbox": [0, 0, 10, 10], "text": "hello", "confidence": 0.95},
            ],
            "secondary": [
                {"bbox": [20, 20, 30, 30], "text": "world", "confidence": 0.80},
            ],
        }
        blocks, secondary = _parse_ocr(ocr_raw)
        assert len(blocks) == 1
        assert blocks[0]["text"] == "hello"
        assert len(secondary) == 1
        assert secondary[0]["text"] == "world"

    def test_legacy_list_format(self):
        ocr_raw = [
            {"bbox": [0, 0, 10, 10], "text": "hello", "confidence": 0.95},
            {"bbox": [20, 20, 30, 30], "text": "world", "confidence": 0.80},
        ]
        blocks, secondary = _parse_ocr(ocr_raw)
        assert len(blocks) == 2
        assert secondary == []

    def test_empty_secondary(self):
        ocr_raw = {"target": [{"text": "A"}], "secondary": []}
        blocks, secondary = _parse_ocr(ocr_raw)
        assert len(blocks) == 1
        assert secondary == []

    def test_missing_secondary_key(self):
        ocr_raw = {"target": [{"text": "B"}]}
        blocks, secondary = _parse_ocr(ocr_raw)
        assert len(blocks) == 1
        assert secondary == []

    def test_dict_without_target_key(self):
        """Dict without 'target' key -> treated as empty."""
        ocr_raw = {"foo": "bar"}
        blocks, secondary = _parse_ocr(ocr_raw)
        assert blocks == []
        assert secondary == []

    def test_none_input(self):
        """Non-dict, non-list -> empty."""
        blocks, secondary = _parse_ocr(None)
        assert blocks == []
        assert secondary == []

    def test_string_input(self):
        """String (invalid) -> empty."""
        blocks, secondary = _parse_ocr("invalid")
        assert blocks == []
        assert secondary == []
