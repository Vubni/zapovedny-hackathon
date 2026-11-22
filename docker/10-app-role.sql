-- Run once by the official PostgreSQL entrypoint, on an empty data volume.
\getenv app_password METEO_APP_DB_PASSWORD
CREATE ROLE meteo_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
REVOKE CONNECT ON DATABASE meteo FROM PUBLIC;
GRANT CONNECT ON DATABASE meteo TO meteo_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO meteo_app;
