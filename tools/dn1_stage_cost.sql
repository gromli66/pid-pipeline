-- dn1_stage_cost.sql — цена одной схемы из processing_stages (ДН1, пункт дороги 0.9).
--
-- Отвечает на вопрос, который нигде не задавался: сколько стоит одна схема и
-- где в ней стоит человек. `app/api/stats.py` читает те же строки, но только
-- ради p50 машинного времени и ЯВНО выбрасывает ручные стадии (`_MANUAL_STAGES`,
-- stats.py:40-45) — то есть единственный потребитель этих данных отбрасывает
-- сигнал оператора by design.
--
-- Как считается человеческое время. Порядок конвейера на сервере не канонизирован
-- (он живёт в UI, `ui/services/progress_model.py`), а OCR идёт параллельно
-- graph_building, поэтому LAG по «следующей стадии» даёт отрицательные разрывы.
-- Здесь вместо порядка — ОБЪЕДИНЕНИЕ интервалов [started_at, completed_at]
-- (gaps-and-islands): машинное время = длина объединения, человек+простой =
-- полное время минус объединение. Параллельность и ретраи (до 27 попыток на
-- стадию) при этом считаются один раз, а не суммируются.
--
-- ⚠ Что этими числами доказать НЕЛЬЗЯ: разрыв — это «человек ИЛИ простой в
-- очереди», разделить их нечем; `cvat_validation.duration_seconds` — не время
-- оператора (строка открывается, когда он уже нажал подтверждение).
--
-- Запуск (из корня репо, только чтение):
--   docker exec -i pid_postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -P pager=off -f -' < tools/dn1_stage_cost.sql

\set ON_ERROR_STOP on

CREATE TEMP VIEW dn1_busy AS
WITH s AS (
    SELECT diagram_uid, stage_type::text AS stage, started_at, completed_at
    FROM processing_stages
    WHERE status = 'completed'
      AND started_at IS NOT NULL AND completed_at IS NOT NULL
      AND completed_at >= started_at
), b AS (
    SELECT *, max(completed_at) OVER (
        PARTITION BY diagram_uid ORDER BY started_at, completed_at
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
    FROM s
), m AS (
    SELECT *, CASE WHEN prev_end IS NULL OR started_at > prev_end THEN 1 ELSE 0 END AS is_new
    FROM b
), g AS (
    SELECT *, sum(is_new) OVER (
        PARTITION BY diagram_uid ORDER BY started_at, completed_at) AS island
    FROM m
)
SELECT diagram_uid, island,
       min(started_at) AS island_start,
       max(completed_at) AS island_end,
       (array_agg(stage ORDER BY started_at))[1] AS first_stage,
       (array_agg(stage ORDER BY completed_at DESC))[1] AS last_stage
FROM g GROUP BY diagram_uid, island;

CREATE TEMP VIEW dn1_cost AS
SELECT diagram_uid,
       count(*) AS islands,
       round(extract(epoch FROM max(island_end) - min(island_start))::numeric, 0) AS wall_s,
       round(sum(extract(epoch FROM island_end - island_start))::numeric, 0) AS machine_s,
       round((extract(epoch FROM max(island_end) - min(island_start))
              - sum(extract(epoch FROM island_end - island_start)))::numeric, 0) AS idle_s
FROM dn1_busy GROUP BY diagram_uid;

\echo '=== 1. Цена схемы: сводка по диаграммам, дошедшим до fxml_generation ==='
SELECT count(*) AS diagrams,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY wall_s)::numeric, 0) AS wall_p50_s,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY machine_s)::numeric, 0) AS machine_p50_s,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY idle_s)::numeric, 0) AS idle_p50_s,
       round(100.0 * sum(idle_s) / nullif(sum(wall_s), 0), 1) AS idle_pct
FROM dn1_cost c
WHERE EXISTS (SELECT 1 FROM processing_stages p
              WHERE p.diagram_uid = c.diagram_uid
                AND p.stage_type = 'fxml_generation' AND p.status = 'completed');

\echo '=== 2. Цена схемы: все диаграммы со стадиями ==='
SELECT count(*) AS diagrams,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY wall_s)::numeric, 0) AS wall_p50_s,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY machine_s)::numeric, 0) AS machine_p50_s,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY idle_s)::numeric, 0) AS idle_p50_s,
       round(100.0 * sum(idle_s) / nullif(sum(wall_s), 0), 1) AS idle_pct
FROM dn1_cost;

\echo '=== 3. Разрывы: после какой стадии ждут и сколько (все разрывы > 60 с) ==='
WITH gaps AS (
    SELECT a.diagram_uid, a.last_stage AS after_stage, b.first_stage AS before_stage,
           extract(epoch FROM b.island_start - a.island_end) AS gap_s
    FROM dn1_busy a
    JOIN dn1_busy b ON b.diagram_uid = a.diagram_uid AND b.island = a.island + 1
)
SELECT after_stage || ' -> ' || before_stage AS gap,
       count(*) AS n,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap_s)::numeric, 0) AS p50_s,
       round(sum(gap_s)::numeric, 0) AS total_s,
       round(100.0 * sum(gap_s) / sum(sum(gap_s)) OVER (), 1) AS share_pct
FROM gaps WHERE gap_s > 60
GROUP BY 1 ORDER BY total_s DESC LIMIT 15;

\echo '=== 4. Машинное время по стадиям: все попытки против последней ==='
WITH last_try AS (
    SELECT DISTINCT ON (diagram_uid, stage_type) id
    FROM processing_stages WHERE status = 'completed'
    ORDER BY diagram_uid, stage_type, attempt DESC
)
SELECT p.stage_type::text AS stage,
       count(*) AS rows_all,
       round(sum(p.duration_seconds)::numeric, 0) AS sum_all_s,
       round(sum(p.duration_seconds) FILTER (
             WHERE p.id IN (SELECT id FROM last_try))::numeric, 0) AS sum_last_s,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY p.duration_seconds)::numeric, 1) AS p50_s
FROM processing_stages p
WHERE p.status = 'completed'
GROUP BY 1 ORDER BY sum_all_s DESC;

\echo '=== 5. Именованные стыки «машина закончила -> машина продолжила»: между ними человек ==='
-- Пары стадий сшиваются ПОСТРОЧНО: для каждого запуска стадии B берётся
-- ближайшее ПРЕДШЕСТВУЮЩЕЕ завершение стадии A той же диаграммы. Через
-- max(A)/min(B) считать нельзя — при 27 попытках на стадию (см. §7) поздний
-- перезапуск A уводит разрыв в минус и стык исчезает из отчёта.
-- Разрывы длиннее CAP (4 ч) отброшены отдельной колонкой: на машине разработки
-- диаграмму возвращают к работе через дни, и это не смена оператора, а пауза
-- в разработке.
WITH pairs AS (
    SELECT * FROM (VALUES
        ('detection', 'cvat_validation', 'подтверждение боксов в CVAT'),
        ('junction_classification', 'final_skeletonization', 'правка масок и перекрёстков'),
        ('graph_building', 'contour_extraction', 'проверка графа'),
        ('layout', 'fxml_generation', 'ручная правка холста')
    ) AS p(from_stage, to_stage, what)
), g AS (
    SELECT p.what,
           extract(epoch FROM b.started_at - (
               SELECT max(a.completed_at) FROM processing_stages a
               WHERE a.diagram_uid = b.diagram_uid
                 AND a.stage_type::text = p.from_stage
                 AND a.status = 'completed'
                 AND a.completed_at < b.started_at)) AS gap_s
    FROM pairs p
    JOIN processing_stages b ON b.stage_type::text = p.to_stage
                            AND b.started_at IS NOT NULL
)
SELECT what,
       count(*) FILTER (WHERE gap_s <= 14400) AS n,
       count(*) FILTER (WHERE gap_s > 14400) AS over_cap,
       round(percentile_cont(0.5) WITHIN GROUP (
             ORDER BY gap_s) FILTER (WHERE gap_s <= 14400)::numeric, 0) AS p50_s,
       round(max(gap_s) FILTER (WHERE gap_s <= 14400)::numeric, 0) AS max_s
FROM g WHERE gap_s IS NOT NULL GROUP BY what ORDER BY p50_s DESC NULLS LAST;

\echo '=== 5a. Строка cvat_validation против реального ожидания оператора ==='
SELECT count(*) AS rows_cvat,
       round(percentile_cont(0.5) WITHIN GROUP (
             ORDER BY duration_seconds)::numeric, 1) AS row_duration_p50_s
FROM processing_stages WHERE stage_type = 'cvat_validation' AND duration_seconds IS NOT NULL;

\echo '=== 6. Покрытие: какие стадии вообще не пишутся в таблицу ==='
SELECT s.stage AS never_written
FROM (VALUES ('upload'), ('frame_removal'), ('detection'), ('cvat_validation'),
             ('direction_classification'), ('segmentation'), ('skeletonization'),
             ('junction_classification'), ('mask_validation'), ('final_skeletonization'),
             ('graph_building'), ('graph_validation'), ('contour_extraction'),
             ('ocr'), ('layout'), ('fxml_generation')) AS s(stage)
WHERE NOT EXISTS (SELECT 1 FROM processing_stages p WHERE p.stage_type::text = s.stage);

\echo '=== 7. Ретраи: сколько машинного времени съедено перезапусками ==='
SELECT count(*) FILTER (WHERE attempt > 1) AS rows_retry,
       count(*) AS rows_all,
       round(sum(duration_seconds) FILTER (WHERE attempt > 1)::numeric, 0) AS retry_s,
       round(sum(duration_seconds)::numeric, 0) AS all_s,
       max(attempt) AS max_attempt
FROM processing_stages WHERE status = 'completed';
