BEGIN TRANSACTION;

SELECT id INTO #to_delete
FROM (
    SELECT id, ROW_NUMBER() OVER (PARTITION BY nid ORDER BY id) AS rn
    FROM taxpayers
    WHERE nid IS NOT NULL
) x
WHERE rn > 1;

DELETE FROM taxpayer_addresses WHERE taxpayer_id IN (SELECT id FROM #to_delete);
DELETE FROM taxpayers WHERE id IN (SELECT id FROM #to_delete);

SELECT COUNT(*) AS deleted FROM #to_delete;
DROP TABLE #to_delete;

COMMIT;