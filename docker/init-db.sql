-- Creates the one database Oracle owns, and the role it connects as.
--
-- Both names are asserted by the migrations and by the test suite: Oracle refuses to run
-- against a database called anything else, so that a misconfigured DATABASE_URL cannot
-- point the engine at somebody else's data. Change them here only if you change them in
-- backend/alembic/env.py and the tests as well.
--
-- Runs once, on an empty data volume. Docker Compose substitutes nothing in this file, so
-- the password below is the bundled development password; override it by supplying your
-- own DATABASE_URL and POSTGRES_PASSWORD in docker/.env before the first `up`.

CREATE ROLE ai_coscientist_gui_app LOGIN PASSWORD 'changeme';
CREATE DATABASE ai_coscientist_gui OWNER ai_coscientist_gui_app;
