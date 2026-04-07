"""
domain_profile.py — Базовый класс доменного профиля для P&ID OCR-пайплайна.

Гибридный подход:
  - YAML: паттерны, пороги, units, noise-регулярки
  - Python-класс: classify, is_noise, is_target, группировка, пост-обработка

Для нового домена:
  1. Создать YAML (паттерны, units, пороги)
  2. Наследовать BaseDomainProfile, реализовать абстрактные методы
  3. Передать профиль в pipeline: --profile profiles/my_domain.py:MyProfile
"""

import re
import yaml
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Dataclasses ──────────────────────────────────

@dataclass
class CompiledPattern:
    """Скомпилированный regex-паттерн с метаданными."""
    name: str
    regex: re.Pattern
    role: str          # full, head, tail, standalone, fragment
    min_length: int = 0


@dataclass
class CodeMatch:
    """Результат разбора кода оборудования на компоненты.

    Универсальный: покрывает KKS (block+system+fn+unit+num+suffix),
    кириллический (block-system-number+suffix) и любые другие форматы.
    """
    full: str                       # Полный нормализованный код
    block: str = ""                 # Блок (10, Т1, ...)
    system: str = ""                # Система (LCS, ПД, ...)
    fn: str = ""                    # Функциональный номер
    num: str = ""                   # Номер
    unit: str = ""                  # Unit code (AA, ПД, ...)
    unit_num: str = ""              # Номер unit (001, 585, ...)
    suffix: str = ""                # Суффикс (A, К, СК, ...)
    confidence: float = 1.0
    span: tuple[int, int] = (0, 0) # Позиция в исходном тексте
    code_type: str = ""             # Имя code_type из конфига
    corrected_text: str = ""        # Текст после OCR-коррекции


@dataclass
class DiamMatch:
    """Результат разбора диаметра."""
    prefix: str                     # Dy, DN, Ø, ⌀
    diameter: int                   # 50, 200, ...
    suffix: str = ""                # c, cs, *4, ...
    text: str = ""                  # Полный текст "Dy50"
    confidence: float = 1.0


@dataclass
class GroupingPass:
    """Один проход пространственной группировки."""
    name: str
    source: list          # категории-источники
    action: str           # emit, merge_and_emit, attach_to_existing, merge_text_and_reclassify
    target: list = None   # категории-цели (для поиска соседей)
    max_distance: float = 3.0
    direction: str = "any"    # below_only, above_only, any
    x_overlap_min: float = 0.25
    chain: bool = False
    chain_max_distance: float = 2.0
    chain_head_between_check: bool = False


# ── Базовый профиль ──────────────────────────────

class BaseDomainProfile(ABC):
    """
    Абстрактный базовый класс доменного профиля.

    Загружает YAML-конфиг (паттерны, units, пороги, noise regex).
    Абстрактные методы реализуются в конкретных профилях.
    """

    def __init__(self, yaml_path: Optional[str] = None,
                 config: Optional[dict] = None):
        """
        Args:
            yaml_path: путь к YAML-файлу с конфигурацией
            config: dict-конфиг (альтернатива yaml_path)
        """
        if yaml_path:
            with open(yaml_path, encoding='utf-8') as f:
                self._config = yaml.safe_load(f)
        elif config:
            self._config = config
        else:
            self._config = {}

        self.name = self._config.get('name', self.__class__.__name__)
        self.version = self._config.get('version', '1.0')
        self.languages = self._config.get('languages', ['en'])

        # Скомпилировать units → regex-подстановка
        self.units = self._config.get('units', [])
        self.unit_re = '|'.join(re.escape(u) for u in self.units) if self.units else ''

        # Скомпилировать паттерны из YAML
        self.patterns: dict[str, CompiledPattern] = {}
        for pname, pdef in self._config.get('patterns', {}).items():
            raw = pdef['regex'].replace('%%UNITS%%', self.unit_re)
            flags = 0
            if 'IGNORECASE' in pdef.get('flags', ''):
                flags |= re.IGNORECASE
            self.patterns[pname] = CompiledPattern(
                name=pname,
                regex=re.compile(raw, flags),
                role=pdef.get('role', 'full'),
                min_length=pdef.get('min_length', 0),
            )

        # Группировки по ролям
        self.head_patterns = [p for p in self.patterns.values()
                              if p.role == 'head']
        self.tail_patterns = [p for p in self.patterns.values()
                              if p.role == 'tail']
        self.full_patterns = [p for p in self.patterns.values()
                              if p.role == 'full']
        self.standalone_patterns = [p for p in self.patterns.values()
                                    if p.role == 'standalone']
        self.fragment_patterns = [p for p in self.patterns.values()
                                  if p.role == 'fragment']

        # Noise regex из YAML (простые regex-правила)
        self._noise_regexes: list[tuple[str, re.Pattern]] = []
        for nr in self._config.get('noise', {}).get('rules', []):
            if 'regex' in nr:
                flags = 0
                if 'IGNORECASE' in nr.get('flags', ''):
                    flags |= re.IGNORECASE
                self._noise_regexes.append(
                    (nr['name'], re.compile(nr['regex'], flags)))

        # Нормализация символов из YAML
        norm_cfg = self._config.get('char_normalization', {})
        self.normalize_enabled = norm_cfg.get('enabled', False)
        self._normalize_table = str.maketrans(
            norm_cfg.get('table', {}))

        # Grouping passes из YAML (может быть переопределено в Python)
        self._yaml_grouping_passes = []
        for gp in self._config.get('grouping', {}).get('passes', []):
            src = gp['source']
            if isinstance(src, str):
                src = [src]
            tgt = gp.get('target')
            if isinstance(tgt, str):
                tgt = [tgt]
            self._yaml_grouping_passes.append(GroupingPass(
                name=gp['name'],
                source=src,
                target=tgt,
                max_distance=gp.get('max_distance', 3.0),
                direction=gp.get('direction', 'any'),
                x_overlap_min=gp.get('x_overlap_min', 0.25),
                chain=gp.get('chain', False),
                chain_max_distance=gp.get('chain_max_distance', 2.0),
                chain_head_between_check=gp.get(
                    'chain_head_between_check', False),
                action=gp.get('action', 'emit'),
            ))

    # ── Утилиты (не абстрактные) ─────────────────

    def normalize_text(self, text: str) -> str:
        """Нормализация символов (кириллица→латиница и т.п.)."""
        if not self.normalize_enabled:
            return text
        return text.translate(self._normalize_table)

    def pattern(self, name: str) -> Optional[CompiledPattern]:
        """Получить паттерн по имени."""
        return self.patterns.get(name)

    def match_pattern(self, name: str, text: str) -> Optional[re.Match]:
        """Проверить паттерн по имени."""
        p = self.patterns.get(name)
        if p:
            return p.regex.search(text)
        return None

    def _check_noise_regexes(self, text: str) -> bool:
        """Проверка noise-regex из YAML."""
        for name, rx in self._noise_regexes:
            if rx.search(text):
                return True
        return False

    # ── Абстрактные методы (реализуются в профиле) ─

    @abstractmethod
    def classify(self, text: str) -> str:
        """
        Классифицировать текст → категория.

        Возвращает строку: 'FULL', 'HEAD', 'TAIL', 'DN',
        'MULTI', 'FRAG', 'OTHER' и т.д.
        Конкретные категории зависят от домена.
        """
        ...

    @abstractmethod
    def is_noise(self, text_raw: str) -> bool:
        """
        Является ли текст шумом?

        Вызывается ДО классификации. Шумовые блоки отбрасываются.
        Блоки с целевыми паттернами НЕ должны быть шумом.
        """
        ...

    @abstractmethod
    def is_target(self, text: str) -> bool:
        """
        Финальное решение: блок — целевой (зелёный)?

        Вызывается ПОСЛЕ группировки, на собранном тексте.
        """
        ...

    @abstractmethod
    def has_any_target_pattern(self, text: str) -> bool:
        """
        Есть ли в тексте хотя бы один целевой паттерн?

        Используется для защиты от noise-фильтра и как
        fallback в title block detection.
        """
        ...

    @abstractmethod
    def split_inline(self, text: str) -> list[str]:
        """
        Разбить строку на токены по паттернам домена.

        Пример KKS: "AA001 10LCS68" → ["AA001", "10LCS68"]
        Пример ISA: "FIC-101 LT-200" → ["FIC-101", "LT-200"]
        """
        ...

    @abstractmethod
    def category_role(self, category: str) -> str:
        """
        Семантическая роль категории для universal engine.

        Маппинг category (из classify()) → role:
          'head'       — начало составного кода (HEAD, HEAD_DN)
          'tail'       — хвост составного кода (TAIL)
          'full'       — полный самодостаточный код (FULL, MULTI)
          'standalone' — независимый маркер (DN, LINE)
          'fragment'   — фрагмент для склейки (FRAG)
          'other'      — не целевой

        Используется universal engine для ветвления в multiline
        processing, фильтрации, пост-обработки.
        """
        ...

    def get_grouping_passes(self) -> list[GroupingPass]:
        """
        Получить проходы группировки.

        По умолчанию берёт из YAML. Может быть переопределено
        в Python-профиле для кастомной логики.
        """
        return self._yaml_grouping_passes

    # ── Хуки пост-обработки (переопределяемые) ────

    def postprocess_split_multi(self, target_blocks: list) -> list:
        """
        Разбить блоки с несколькими целевыми кодами.

        По умолчанию: не трогать.
        KKS: split_multi_kks_greens
        """
        return target_blocks

    def postprocess_promote_from_secondary(
            self, secondary_blocks: list,
            existing_targets: list) -> tuple[list, int]:
        """
        Извлечь целевые коды из блоков вторичного языка.

        По умолчанию: не трогать.
        KKS: promote_kks_from_cyrillic
        """
        return [], 0

    def postprocess_merge_secondary(
            self, blocks: list,
            max_vgap=None, max_hgap=None,
            fallback_med_h=None) -> list:
        """
        Склеить близкие блоки вторичного языка.

        По умолчанию: не трогать.
        KKS: merge_cyrillic_blocks
        """
        return blocks

    def postprocess_extract_target_from_merged(
            self, blocks: list,
            image_path: Optional[str] = None) -> list:
        """
        Извлечь целевые коды из merged-блоков.

        По умолчанию: не трогать.
        KKS: extract_kks_from_cyrillic
        """
        return blocks

    def has_secondary_script(self, text: str) -> bool:
        """
        Содержит ли текст символы вторичного скрипта?

        По умолчанию: False.
        KKS: has_cyrillic
        """
        return False

    def collect_secondary_from_raw(
            self, detections: list,
            line_pitch=None,
            fallback_med_h=None) -> list:
        """
        Собрать блоки вторичного языка из сырых OCR-детекций.

        По умолчанию: пустой список.
        KKS: collect_cyrillic_from_raw
        """
        return []

    # ── Парсинг кодов (переопределяемые) ──────────

    def parse_code(self, text: str) -> Optional[CodeMatch]:
        """
        Разобрать текст на компоненты кода оборудования.

        По умолчанию: None (не реализовано).
        Переопределяется в ConfigDrivenProfile и legacy-профилях.
        """
        return None

    def parse_diameter(self, text: str) -> Optional[DiamMatch]:
        """
        Разобрать текст как диаметр.

        По умолчанию: None (не реализовано).
        Переопределяется в ConfigDrivenProfile и legacy-профилях.
        """
        return None


# ── Загрузчик профилей ──────────────────────────

def load_profile(profile_spec: str) -> BaseDomainProfile:
    """
    Загрузить профиль по спецификации.

    Форматы:
      File-path (CLI):
        "configs/projects/thermohydraulics/ocr_profile.py:KKSProfile"
        "configs/projects/thermohydraulics/ocr_profile.py:KKSProfile:ocr_profile.yaml"

      Module-path (server, legacy):
        "modules.ocr.profiles.isa_s5:ISAS5Profile"

    Args:
        profile_spec: строка "path_or_module:ClassName[:yaml_path]"
    """
    parts = profile_spec.split(':')
    if len(parts) < 2:
        raise ValueError(
            f"Profile spec must be 'module:ClassName[:yaml]', "
            f"got: {profile_spec}")

    module_path = parts[0]
    class_name = parts[1]
    yaml_path = parts[2] if len(parts) > 2 else None

    # Динамический импорт: file-path или module-path
    if module_path.endswith('.py'):
        # File-based import (CLI mode)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "domain_profile_impl", module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    else:
        # Module-based import (server mode)
        import importlib
        mod = importlib.import_module(module_path)

    cls = getattr(mod, class_name)
    if not issubclass(cls, BaseDomainProfile):
        raise TypeError(
            f"{class_name} must inherit from BaseDomainProfile")

    if yaml_path:
        return cls(yaml_path=yaml_path)
    else:
        return cls()


# ── Config-driven профиль ────────────────────────

_CYRILLIC_RE = re.compile(r'[\u0400-\u04FF]')
_LATIN_RE = re.compile(r'[A-Za-z]')


class ConfigDrivenProfile(BaseDomainProfile):
    """
    Универсальный профиль — вся логика из YAML.

    Заменяет KKSProfile, PidCyrillicProfile и любые будущие.
    Новый проект = только YAML, ноль Python.
    """

    def __init__(self, yaml_path: Optional[str] = None,
                 config: Optional[dict] = None):
        super().__init__(yaml_path=yaml_path, config=config)

        # Override meta from nested 'meta' section (v2.0 format)
        meta = self._config.get('meta', {})
        if meta.get('name'):
            self.name = meta['name']
        if meta.get('version'):
            self.version = meta['version']
        if meta.get('languages'):
            self.languages = meta['languages']

        # Загрузить classify engine
        from modules.ocr.classify_engine import ClassifyEngine
        classify_cfg = self._config.get('classify', {})
        self._classify_engine = ClassifyEngine(
            rules_config=classify_cfg.get('rules', []),
            patterns=self.patterns,
            roles_config=classify_cfg.get('roles', {}),
        )

        # Загрузить noise engine
        from modules.ocr.noise_engine import NoiseEngine
        noise_cfg = self._config.get('noise', {})
        self._noise_engine = NoiseEngine(
            noise_regexes=self._noise_regexes,
            thresholds=noise_cfg.get('thresholds', {}),
            has_target_fn=self.has_any_target_pattern,
        )

        # Meta (continued)
        self._secondary_script = meta.get('secondary_script', 'cyrillic')
        self._norm_direction = meta.get('normalization_direction', 'cyr_to_lat')

        # Postprocess config
        self._postprocess = self._config.get('postprocess', {})

        # Classify preprocessing
        preprocess_cfg = classify_cfg.get('preprocess', {})
        self._classify_preprocess = bool(preprocess_cfg)
        self._preprocess_clean_html = preprocess_cfg.get('clean_html', False)
        self._preprocess_normalize = preprocess_cfg.get('normalize', False)
        self._preprocess_block_fixes = preprocess_cfg.get('block_fixes', False)

        # Block fixes from ocr_corrections (71→Т1, [1→Т1, etc.)
        ocr_corr = self._config.get('ocr_corrections', {})
        self._block_fixes: dict[str, str] = ocr_corr.get('block_fixes', {})

        # Кэшируемые паттерны (для удобства хуков и target-проверки)
        self._full = self.patterns.get('full_code')
        self._head = self.patterns.get('head_code')
        self._tail = self.patterns.get('tail_code')
        self._dn = self.patterns.get('diameter')
        self._dn_inline = self.patterns.get('diameter_inline')
        self._frag = self.patterns.get('fragment_head')
        self._head_loose = self.patterns.get('head_loose')

        self._unit_re_raw = '|'.join(self.units) if self.units else ''

    # ── preprocess ───────────────────────────────

    def _preprocess_text(self, text: str) -> str:
        """Опциональная предобработка текста перед classify/is_target."""
        if not self._classify_preprocess:
            return text
        if self._preprocess_clean_html:
            text = re.sub(r'</?[a-z]+[^>]*>', '', text)
            text = text.replace('<br>', ' ').replace('\n', ' ')
        if self._preprocess_normalize:
            text = self.normalize_text(text)
        if self._preprocess_block_fixes and self._block_fixes:
            for bad, good in self._block_fixes.items():
                if text.startswith(bad + '-') or text.startswith(bad + '\u2013'):
                    text = good + text[len(bad):]
                    break
        return text.strip()

    # ── classify ─────────────────────────────────

    def classify(self, text: str) -> str:
        text = self._preprocess_text(text)
        return self._classify_engine.classify(text)

    # ── is_noise ─────────────────────────────────

    def is_noise(self, text_raw: str) -> bool:
        return self._noise_engine.is_noise(text_raw)

    # ── is_target ────────────────────────────────

    def is_target(self, text: str) -> bool:
        text = self._preprocess_text(text.strip())
        # Проверяем ВСЕ паттерны с ролью "full" (full_code, bare_code, ...)
        has_full = any(
            p.regex.search(text) for p in self.full_patterns
        )
        # Head + tail
        has_head = any(
            p.regex.search(text) for p in self.head_patterns
        )
        has_tail = bool(
            re.search(r'\b(' + self._unit_re_raw + r')\d{2,4}',
                       text, re.I)) if self._unit_re_raw else False
        # Standalone (diameter, ...)
        is_standalone = any(
            p.regex.match(text.strip()) for p in self.standalone_patterns
        )
        return has_full or (has_head and has_tail) or is_standalone

    # ── has_any_target_pattern ───────────────────

    def has_any_target_pattern(self, text: str) -> bool:
        text = self._preprocess_text(text)
        if self._head and self._head.regex.search(text):
            return True
        if self._tail and self._tail.regex.match(text.strip()):
            return True
        if self._dn and self._dn.regex.match(text.strip()):
            return True
        return False

    # ── category_role ────────────────────────────

    def category_role(self, category: str) -> str:
        return self._classify_engine.category_role(category)

    # ── split_inline ─────────────────────────────

    def split_inline(self, text: str) -> list[str]:
        text = re.sub(r'</?[a-z]+>', '', text).strip()
        tokens = []

        if self._head:
            for m in self._head.regex.finditer(text):
                tokens.append((m.start(), m.end(), m.group()))

        if self._unit_re_raw:
            for m in re.finditer(
                    r'\b(' + self._unit_re_raw + r')(\d{2,4}[A-Z]?)\b',
                    text, re.I):
                if not any(t[0] <= m.start() < t[1] for t in tokens):
                    tokens.append((m.start(), m.end(), m.group()))

        if self._dn_inline:
            for m in self._dn_inline.regex.finditer(text):
                if not any(t[0] <= m.start() < t[1] for t in tokens):
                    tokens.append((m.start(), m.end(), m.group()))

        tokens.sort(key=lambda t: t[0])
        return ([t[2].strip() for t in tokens]
                if len(tokens) > 1 else [text])

    # ── has_secondary_script ─────────────────────

    def has_secondary_script(self, text: str) -> bool:
        if self._secondary_script == 'cyrillic':
            return bool(_CYRILLIC_RE.search(text))
        elif self._secondary_script == 'latin':
            return bool(_LATIN_RE.search(text))
        return False

    # ── postprocess (делегирует в хуки) ──────────

    def postprocess_split_multi(self, target_blocks: list) -> list:
        cfg = self._postprocess.get('split_multi', {})
        if not cfg.get('enabled', True):
            return target_blocks

        from modules.ocr.hooks import SPLIT_MULTI_STRATEGIES
        strategy = cfg.get('strategy', 'head_tail_join')
        fn = SPLIT_MULTI_STRATEGIES.get(strategy)
        if fn:
            return fn(target_blocks, self)
        return target_blocks

    def postprocess_promote_from_secondary(
            self, secondary_blocks: list,
            existing_targets: list) -> tuple[list, int]:
        cfg = self._postprocess.get('promote_from_secondary', {})
        if not cfg.get('enabled', True):
            return [], 0

        from modules.ocr.hooks import promote_from_secondary_generic
        return promote_from_secondary_generic(
            secondary_blocks, existing_targets, self,
            min_head_length=cfg.get('min_head_length', 6),
        )

    def postprocess_merge_secondary(
            self, blocks: list,
            max_vgap=None, max_hgap=None,
            fallback_med_h=None) -> list:
        cfg = self._postprocess.get('merge_secondary', {})
        if not cfg.get('enabled', True):
            return blocks

        from modules.ocr.hooks import merge_secondary_union_find
        return merge_secondary_union_find(
            blocks, self,
            max_vgap_factor=cfg.get('max_vgap_factor', 1.0),
            max_hgap_factor=cfg.get('max_hgap_factor', 1.0),
            fallback_med_h=fallback_med_h,
        )

    def postprocess_extract_target_from_merged(
            self, blocks: list,
            image_path: Optional[str] = None) -> list:
        cfg = self._postprocess.get('extract_target_from_merged', {})
        if not cfg.get('enabled', True):
            return blocks

        from modules.ocr.hooks import extract_target_from_merged_cluster
        return extract_target_from_merged_cluster(blocks, self, image_path)

    def collect_secondary_from_raw(
            self, detections: list,
            line_pitch=None,
            fallback_med_h=None) -> list:
        cyr_candidates = []
        for b in detections:
            text_raw = b.get('text', '')
            text_clean = re.sub(r'</?[a-z]+>', '', text_raw).replace('<br>', ' ')
            if not self.has_secondary_script(text_clean):
                continue
            if self.is_noise(text_raw):
                continue
            cyr_candidates.append({
                'bbox': list(b['bbox']),
                'text': text_clean.strip(),
                'is_target': False,
            })

        if not cyr_candidates:
            return []

        merged = self.postprocess_merge_secondary(
            cyr_candidates, max_vgap=line_pitch, max_hgap=line_pitch,
            fallback_med_h=fallback_med_h)

        result = []
        for b in merged:
            out = {k: v for k, v in b.items() if k != '_members'}
            if (not out.get('is_target', False)
                    and self.has_secondary_script(out.get('text', ''))):
                clean = re.sub(r'</?[a-z]+>', '', out.get('text', ''))
                if self._secondary_script == 'cyrillic':
                    cyr_words = re.findall(r'[а-яёА-ЯЁ]{2,}', clean)
                    if cyr_words:
                        result.append(out)
                else:
                    result.append(out)
        return result
