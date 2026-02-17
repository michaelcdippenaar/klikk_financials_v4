#!/usr/bin/env bash
# Create or reset PostgreSQL user klikk_user so Django can connect.
# Run with: ./scripts/setup_db_user.sh   (will prompt for sudo)

set -e
APP_USER="klikk_user"
APP_PASSWORD="Number55dip"
DB_NAME="klikk_financials_v4"

echo "Creating/resetting PostgreSQL user $APP_USER..."
cd /tmp && sudo -u postgres psql -d postgres -v ON_ERROR_STOP=1 <<EOSQL
-- Create user if not exists, then set password (works for both new and existing)
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$APP_USER') THEN
    CREATE ROLE $APP_USER WITH LOGIN PASSWORD '$APP_PASSWORD';
  ELSE
    ALTER ROLE $APP_USER WITH PASSWORD '$APP_PASSWORD';
  END IF;
END
\$\$;

-- Ensure user can connect to the database
GRANT CONNECT ON DATABASE $DB_NAME TO $APP_USER;
GRANT USAGE ON SCHEMA public TO $APP_USER;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO $APP_USER;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO $APP_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $APP_USER;
EOSQL

echo "Done. Try starting the server again."
