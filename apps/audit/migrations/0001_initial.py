"""
Create schema ``audit`` (if missing) and the registry tables.

``audit.xero_writes`` already exists in production and is deliberately NOT
touched here (no CREATE/ALTER/DROP on it, and no reverse that drops the schema).
"""
from django.db import migrations


FORWARD = """
CREATE SCHEMA IF NOT EXISTS audit;

CREATE TABLE IF NOT EXISTS audit.checks (
    code          text PRIMARY KEY,
    title         text NOT NULL,
    category      text NOT NULL,
    severity      text NOT NULL CHECK (severity IN ('critical','high','medium','low')),
    description   text NOT NULL,
    rationale     text NOT NULL DEFAULT '',
    sql_text      text NOT NULL,
    expected      text NOT NULL CHECK (expected IN ('zero_rows','list','value')),
    owner_action  text NOT NULL DEFAULT '',
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    source        text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit.check_runs (
    run_id        bigserial PRIMARY KEY,
    fy            integer NOT NULL,
    tenant_id     text NOT NULL,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    triggered_by  text NOT NULL DEFAULT '',
    summary       jsonb
);

CREATE TABLE IF NOT EXISTS audit.check_results (
    id            bigserial PRIMARY KEY,
    run_id        bigint NOT NULL REFERENCES audit.check_runs(run_id) ON DELETE CASCADE,
    code          text NOT NULL REFERENCES audit.checks(code) ON UPDATE CASCADE,
    status        text NOT NULL CHECK (status IN ('PASS','WARN','FAIL','ERROR')),
    row_count     integer,
    sample_rows   jsonb,
    duration_ms   integer,
    notes         text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS check_results_code_idx ON audit.check_results (code, run_id DESC);
CREATE INDEX IF NOT EXISTS check_runs_fy_idx ON audit.check_runs (fy, tenant_id, run_id DESC);
"""

# Reverse only drops what this migration created. Never the schema, never xero_writes.
BACKWARD = """
DROP TABLE IF EXISTS audit.check_results;
DROP TABLE IF EXISTS audit.check_runs;
DROP TABLE IF EXISTS audit.checks;
"""


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.RunSQL(FORWARD, BACKWARD),
    ]
