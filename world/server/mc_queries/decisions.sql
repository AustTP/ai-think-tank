-- tables: decision_archive
SELECT id AS "id", ts AS "ts", kind AS "kind", model AS "model",
       choice AS "choice", confidence AS "confidence", ok AS "ok",
       cost AS "cost"
FROM decision_archive
ORDER BY ts DESC
LIMIT 100
