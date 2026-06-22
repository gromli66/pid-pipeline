"""
Конфигурация классов для YOLO Node Detection.

39 классов оборудования P&ID после очистки данных (разметка full_3proj_42class).

Маппинг классов (v2, 42class):
- При обучении удалены: annotation (сырой 34), truba (сырой 36), background (сырой 39)
- Оставшиеся переиндексированы в финальные 0..38:
    output     (сырой 35) -> 34
    unknow     (сырой 37) -> 35
    strelka    (сырой 38) -> 36
    napravlenie(сырой 40) -> 37
    vozdushnik (сырой 41) -> 38
- REVERSE_REINDEX возвращает финальные id к "сырым" (cat_id-1) значениям
  для совместимости с разметкой и downstream-слоями (граф работает в сырой нумерации).
"""

NUM_CLASSES = 39

CLASS_NAMES = {
    0: "armatura_ruchn",           # Арматура ручная
    1: "klapan_obratn",            # Клапан обратный
    2: "regulator_ruchn",          # Регулятор ручной
    3: "armatura_electro",         # Арматура электропривод
    4: "regulator_electro",        # Регулятор электропривод
    5: "drossel",                  # Дроссель
    6: "perehod",                  # Переход
    7: "klapan_obratn_seroprivod", # Клапан обратный серопривод
    8: "armatura_seroprivod",      # Арматура серопривод
    9: "regulator_seroprivod",     # Регулятор серопривод
    10: "armatura_membr_electro",  # Арматура мембранная электро
    11: "nasos",                   # Насос
    12: "ventilaytor",             # Вентилятор
    13: "predohran",               # Предохранительный клапан
    14: "condensatootvod",         # Конденсатоотводчик
    15: "rashodomernaya_shaiba",   # Расходомерная шайба
    16: "vodostruiniy_nasos",      # Водоструйный насос
    17: "teploobmen",              # Теплообменник
    18: "zaglushka",               # Заглушка
    19: "gidrozatvor",             # Гидрозатвор
    20: "bak",                     # Бак
    21: "voronka",                 # Воронка
    22: "filtr_meh",               # Фильтр механический
    23: "separator",               # Сепаратор
    24: "kapleulov",               # Каплеуловитель
    25: "celindr_turb",            # Цилиндр турбины
    26: "redukcion_ustr",          # Редукционное устройство
    27: "bistro_redukc_ustr",      # Быстродействующее редукционное устройство
    28: "separator_paro",          # Сепаратор паро
    29: "dearator",                # Деаэратор
    30: "silfonnii_kompensator",   # Сильфонный компенсатор
    31: "electronagrevat",         # Электронагреватель
    32: "smotrowoe_steclo",        # Смотровое стекло
    33: "datchik",                 # Датчик
    34: "output",                  # Выход
    35: "unknow",                  # Прочее оборудование
    36: "strelka",                 # Стрелка
    37: "napravlenie",             # Направление
    38: "vozdushnik",              # Воздушник
}

# Обратный маппинг: имя -> id
CLASS_IDS = {v: k for k, v in CLASS_NAMES.items()}

# Обратная переиндексация для совместимости с исходной разметкой.
# Модель выдаёт финальные 34..38 → возвращаем к "сырым" (cat_id-1).
REVERSE_REINDEX = {
    34: 35,  # output
    35: 37,  # unknow
    36: 38,  # strelka
    37: 40,  # napravlenie
    38: 41,  # vozdushnik
}
