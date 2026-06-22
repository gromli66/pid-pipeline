# ADR-006: UNet++ ensemble со стратегией OR

**Статус:** Принято  
**Дата:** 2026-02  
**Контекст:** Выбор стратегии ensemble для pipe segmentation

---

## Контекст

Pipe segmentation — критическая стадия pipeline: пропущенная труба = потерянное ребро графа. Обучены несколько моделей (plain UNet++, DualHeadModel) с разными конфигурациями. Вопрос: как комбинировать предсказания для максимального recall?

## Рассмотренные стратегии

| Стратегия | Формула | Precision | Recall |
|-----------|---------|-----------|--------|
| Single best | model_A | Baseline | Baseline |
| AND | mask_A & mask_B | Выше | Ниже |
| Mean | (prob_A + prob_B) / 2 > 0.5 | ~ Baseline | ~ Baseline |
| OR | mask_A \| mask_B | Ниже | **Выше** |

## Решение

Стратегия OR: пиксель = труба если **хотя бы одна** модель так считает.

## Обоснование

1. **Recall > Precision для труб.** Пропущенная труба = потерянное ребро графа = неправильная топология. Лишняя труба — менее критична (фильтруется постобработкой skeleton_gap_fill или вручную). OR максимизирует recall.
2. **Эмпирика.** OR даёт +4% Recall при сохранении Precision (проверено на GT). Разные модели ошибаются в разных местах — OR компенсирует.
3. **Совместимость.** `EnsembleInference` поддерживает любые комбинации: plain + DualHead, два plain, два DualHead. При eval DualHeadModel возвращает только mask_logits — совместим с plain output.

## Последствия

- Два checkpoint монтируются в Docker volume.
- GPU RAM: ~3 GB × 2 = ~6 GB (inference поочерёдно, не одновременно в памяти).
- Настраиваемая стратегия (`strategy` parameter): для use-case где важнее precision → переключить на `and`.
- Fallback: если один checkpoint недоступен — inference на одной модели (graceful degradation).
