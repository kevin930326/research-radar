-- 在目前的資料庫建立需要的擴充套件（ADR-0018）。遷移只檢查、不建立：
-- pgvector 的 vector 不是 trusted extension，非超級使用者的擁有者建不了。
-- 本機與 CI 由 db 容器初始化時以超級使用者執行；測試建的每個資料庫也執行這一份。
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
