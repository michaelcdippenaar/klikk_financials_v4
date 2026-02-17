#!/usr/bin/env bash
# Set ownership of all tables and sequences in public schema to klikk_user
# so Django (as klikk_user) can run migrations. Run with: ./scripts/fix_db_ownership.sh

set -e
DB_NAME="${1:-klikk_financials_v4}"
APP_USER="klikk_user"

echo "Setting ownership of all tables and sequences in $DB_NAME to $APP_USER..."
cd /tmp && sudo -u postgres psql -d "$DB_NAME" -v ON_ERROR_STOP=1 <<EOSQL
DO \$\$
DECLARE
  r RECORD;
BEGIN
  FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public')
  LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO klikk_user', r.tablename);
  END LOOP;
  FOR r IN (SELECT sequencename FROM pg_sequences WHERE schemaname = 'public')
  LOOP
    EXECUTE format('ALTER SEQUENCE public.%I OWNER TO klikk_user', r.sequencename);
  END LOOP;
END
\$\$;
EOSQL

echo "Done. You can now run: python manage.py migrate"
