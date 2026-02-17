#!/usr/bin/env bash
# Restore database from dump: create tables and load data.
# Run from project root. Requires: sudo -u postgres access to PostgreSQL.

set -e
DB_NAME="${1:-klikk_financials_v4}"
DUMP_FILE="${2:-/home/mc/apps/klikk_financials_v4/database_dumps/db_20260217_1944.sql}"
APP_USER="klikk_user"
APP_PASSWORD="Number55dip"

echo "Creating role $APP_USER if not exists..."
cd /tmp && sudo -u postgres psql -d postgres -v ON_ERROR_STOP=1 <<EOSQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$APP_USER') THEN
    CREATE ROLE $APP_USER WITH LOGIN PASSWORD '$APP_PASSWORD' CREATEDB;
  END IF;
END
\$\$;
EOSQL

echo "Dropping existing objects in $DB_NAME and restoring from $DUMP_FILE..."
# Replace OWNER TO mc with OWNER TO klikk_user so tables are owned by app user
sed 's/OWNER TO mc/OWNER TO klikk_user/g' "$DUMP_FILE" | sudo -u postgres psql -d "$DB_NAME" -v ON_ERROR_STOP=1 -q

echo "Granting privileges on schema public..."
cd /tmp && sudo -u postgres psql -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "GRANT ALL ON SCHEMA public TO $APP_USER; GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO $APP_USER; GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO $APP_USER; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $APP_USER;"

echo "Restore finished successfully."
