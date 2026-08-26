"""
Text binding module — привязка OCR текста (диаметры, KKS) к элементам графа.

⛔ `binder.py` (TextBinder) СНЯТ 26.08.2026. Его `propagate_diameters` — старое
правило потока диаметров: свободный проход через любой `type == equipment` при
любой степени (2152 протечки из 4745 проходов на корпусе 24 схем), сравнение
ориентаций H/V вместо угла (ломалось на 342 диагоналях) и единственное классовое
правило `perehod`. Заменён `modules/binding/diameter_lines.py` («правило линии»).
`bind_diameters` ушёл вместе с ним: авто-привязка Ø выключена решением заказчика.

В пакете остались `config.py` (TextRecognitionConfig — его читает вкладка
привязки) и DEPRECATED-шимы `matcher.py` / `geometry.py`, делегирующие
в `modules/binding/`.
"""
