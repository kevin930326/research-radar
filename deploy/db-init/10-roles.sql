-- 本機與 CI 的 db 容器第一次初始化時執行（docker-entrypoint-initdb.d），以超級使用者 postgres 身分。
-- 兩個資料庫角色（ADR-0003 決定第 3 點、ADR-0018）；正式環境（Azure）由管理者手動建立同樣的角色。
-- 本機用 trust 驗證、只綁 127.0.0.1，所以角色不設密碼。
--
-- radar_owner：資料庫 radar 的擁有者，只給 radar migrate 用（RADAR_MIGRATION_DATABASE_URL）。
-- radar_app：api、worker 與一般 CLI 指令用（DATABASE_URL）；不能建表，權限由第一個遷移給。
CREATE ROLE radar_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE radar_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

ALTER DATABASE radar OWNER TO radar_owner;
