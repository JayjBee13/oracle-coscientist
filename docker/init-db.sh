#!/bin/sh
# Creates the one database Oracle owns, and the role it connects as.
#
# Both names are asserted by the migrations and by the test suite: Oracle refuses to run
# against a database called anything else, so a misconfigured DATABASE_URL cannot point the
# engine at somebody else's data.
#
# This runs ONCE, on an empty data volume, as the Postgres superuser. The password comes from
# APP_DB_PASSWORD in docker/db.env, which the db service reads through `env_file`. If you
# change it later you must also change DATABASE_URL in docker/.env, and alter the role's
# password by hand — or remove the volume (`docker compose down && docker volume rm
# oracle_db`) so this script runs again.
#
# Everything happens inside a subshell, and no `set` runs at the top level, because a bind
# mount cannot be relied on to carry the executable bit: without it the Postgres entrypoint
# *sources* this file instead of running it, and a stray `set -u` would then apply to the
# entrypoint's own remaining work.

(
  set -e

  app_password="${APP_DB_PASSWORD:-changeme}"
  superuser="${POSTGRES_USER:-postgres}"

  psql --username "$superuser" --dbname postgres \
       --set ON_ERROR_STOP=1 --set app_password="$app_password" <<'SQL'
CREATE ROLE ai_coscientist_gui_app LOGIN PASSWORD :'app_password';
CREATE DATABASE ai_coscientist_gui OWNER ai_coscientist_gui_app;
SQL
)
