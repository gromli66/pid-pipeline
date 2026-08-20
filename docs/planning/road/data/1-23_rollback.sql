-- Откат правки данных пункта дороги 1-23 (1.24), ЗАХОД 2 — 2026-08-20.
--
-- ЧТО БЫЛО СДЕЛАНО. 18 строк `processing_stages` на БД машины разработки
-- (`pid_postgres`, docker, порт 5433). Состав утверждён Максимом 2026-08-20:
--   * 16 брошенных `frame_removal` в статусе `running` (§94.28)  -> skipped
--   * строка 671 `junction_classification`, `running` с 2026-07-20 (§94.9) -> skipped
--   * строка 716 — вторая половина той же пары Б10: ТОЛЬКО пометка в
--     `error_message`; статус/attempt/duration/completed_at НЕ менялись.
-- Строки НЕ удалялись. Ни одна строка вне этих 18 не тронута — доказано
-- отпечатками md5 до/после (MEASUREMENTS §105).
--
-- ДАМП ДО ПРАВКИ (значимые колонки; остальные у всех 18 строк не менялись).
-- Все 17 закрытых строк имели ровно: status='running', completed_at=NULL,
-- error_message=NULL. Строка 716 имела error_message=NULL.
--
--   id   | uid (8)  | stage_type              | status    | attempt | started_at
--   -----+----------+-------------------------+-----------+---------+---------------------------
--    640 | d4bd65d7 | frame_removal           | running   |       1 | 2026-07-10 12:23:47.156754
--    671 | 6e7144d5 | junction_classification | running   |       7 | 2026-07-20 10:16:03.334776
--    703 | 8c9317e5 | frame_removal           | running   |       1 | 2026-07-20 10:24:20.977680
--    704 | 89ca7583 | frame_removal           | running   |       1 | 2026-07-20 10:24:37.543771
--    716 | 6e7144d5 | junction_classification | completed |       8 | 2026-07-20 12:33:36.104937
--    717 | 89ca7583 | frame_removal           | running   |       3 | 2026-07-20 12:39:06.328328
--    755 | d4bd65d7 | frame_removal           | running   |       2 | 2026-07-27 19:56:47.234148
--    807 | 8c9317e5 | frame_removal           | running   |       2 | 2026-07-30 14:32:42.648986
--    857 | b0a8820a | frame_removal           | running   |       1 | 2026-08-05 10:55:34.015199
--    871 | 8516730a | frame_removal           | running   |       1 | 2026-08-05 11:22:39.618164
--    884 | 85b70a73 | frame_removal           | running   |       1 | 2026-08-06 08:10:03.026638
--    885 | f252a4be | frame_removal           | running   |       1 | 2026-08-06 08:10:23.658498
--    899 | d2bb835a | frame_removal           | running   |       1 | 2026-08-06 09:00:08.348601
--    931 | 85b70a73 | frame_removal           | running   |       2 | 2026-08-08 16:00:14.057502
--    932 | f252a4be | frame_removal           | running   |       2 | 2026-08-08 16:00:19.125732
--    934 | 38c8ba6a | frame_removal           | running   |       1 | 2026-08-10 08:47:18.350185
--    978 | 767a96b5 | frame_removal           | running   |       1 | 2026-08-12 12:02:54.894784
--   1017 | 40b99e7e | frame_removal           | running   |       1 | 2026-08-13 12:50:12.791999
--
-- Отпечаток 18 целевых строк ДО правки:
--   md5 = ee13e8c475991093cba4c2026149119c
-- Отпечаток остальных 975 строк (обязан совпасть и до, и после):
--   md5 = d910332b5b49d79685138d3b40d071b1
--
-- КАК ОТКАТИТЬ (одной командой из корня репозитория):
--   docker exec -i pid_postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -' \
--     < docs/planning/road/data/1-23_rollback.sql
--
-- После отката отпечаток 18 строк обязан снова стать
-- ee13e8c475991093cba4c2026149119c — проверка в самом конце файла.

BEGIN;

-- 1. Семнадцать закрытых строк — вернуть в `running`.
UPDATE processing_stages
   SET status        = 'running',
       completed_at  = NULL,
       error_message = NULL
 WHERE id IN (640, 671, 703, 704, 717, 755, 807, 857, 871,
              884, 885, 899, 931, 932, 934, 978, 1017);

-- 2. Строка 716 — снять пометку. Больше у неё ничего не менялось.
UPDATE processing_stages
   SET error_message = NULL
 WHERE id = 716;

COMMIT;

-- 3. Контроль: обязан напечатать 18 и ee13e8c475991093cba4c2026149119c.
SELECT count(*) AS rows_target,
       md5(string_agg(t::text, '|' ORDER BY id)) AS md5_target
FROM (SELECT * FROM processing_stages
      WHERE id IN (640, 671, 703, 704, 716, 717, 755, 807, 857, 871,
                   884, 885, 899, 931, 932, 934, 978, 1017)) t;
