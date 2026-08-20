"""Create ``audit.v_finding_graph`` — the traversal seam for the findings graph.

WHY A VIEW, NOT A GRAPH STORE
-----------------------------
The edges a finding has are ALREADY stored, exactly once, in three ordinary
Postgres columns: ``audit_auditfindinglink (kind, ref)``,
``audit_auditfindingattachment (finding_id)`` and ``audit_auditfinding.check_code``.
Standing up a separate graph store (or even a materialised edge table) would mean
a second copy of that truth and therefore a sync problem — every write path would
have to remember to mirror itself, and the first missed mirror silently loses an
evidence trail. A plain VIEW has no write path of its own, cannot drift, and
costs nothing to keep current.

It is also fast enough by construction: the link table is indexed BOTH ways —
``(finding_id, kind, ref)`` via the unique constraint for the forward walk
("what does this finding cite") and ``(kind, ref)`` for the reverse walk
("which findings cite this slip"), which is the direction the graph endpoint
actually needs. A constant ``to_type`` in the attachment/check branches lets the
planner eliminate them outright when a caller filters on one node type.

SCHEMA SPLIT — read this before editing
---------------------------------------
The raw registry schema ``audit`` (``audit.checks`` / ``check_runs`` /
``check_results`` / ``xero_writes``, created by 0001 and by other tooling) is
NOT the same place as the Django-managed findings tables, which live in the
default schema ``public``. The view LIVES in ``audit`` but READS from ``public``.
Every reference below is schema-qualified on purpose: nothing here may depend on
``search_path``. No pre-existing ``audit.*`` table is created, altered or dropped
— the reverse drops the view and nothing else.

CONSEQUENCE FOR FUTURE MIGRATIONS
---------------------------------
Postgres will refuse to change the TYPE of any column this view selects
(``audit_auditfindinglink.kind`` / ``.ref`` / ``.label``,
``audit_auditfindingattachment.original_name``, ``audit_auditfinding.check_code``)
while the view exists. A later migration that alters one of those must
``DROP VIEW audit.v_finding_graph`` first and recreate it afterwards, in the same
migration. Adding columns is unaffected.

Zero-downtime classification: TRIVIAL. ``CREATE OR REPLACE VIEW`` takes no lock
on the underlying tables beyond a brief ACCESS SHARE, writes no rows, and is
instant regardless of table size.
"""
from django.db import migrations


FORWARD = """
CREATE OR REPLACE VIEW audit.v_finding_graph AS
    -- linked entities: slip / xero_document / bank_transaction / journal / invoice / asana
    SELECT
        'finding'::text     AS from_type,
        f.id::text          AS from_id,
        l.kind::text        AS edge,
        l.kind::text        AS to_type,
        l.ref::text         AS to_id,
        l.label::text       AS label,
        f.fy                AS fy,
        l.created_at        AS created_at
    FROM public.audit_auditfindinglink l
    JOIN public.audit_auditfinding f ON f.id = l.finding_id

    UNION ALL

    -- uploaded evidence files
    SELECT
        'finding'::text,
        f.id::text,
        'attachment'::text,
        'attachment'::text,
        a.id::text,
        a.original_name::text,
        f.fy,
        a.created_at
    FROM public.audit_auditfindingattachment a
    JOIN public.audit_auditfinding f ON f.id = a.finding_id

    UNION ALL

    -- the deterministic registry check the finding came from, when it names one
    SELECT
        'finding'::text,
        f.id::text,
        'check'::text,
        'check'::text,
        f.check_code::text,
        f.check_code::text,
        f.fy,
        f.created_at
    FROM public.audit_auditfinding f
    WHERE f.check_code IS NOT NULL AND btrim(f.check_code) <> '';

COMMENT ON VIEW audit.v_finding_graph IS
    'One row per finding edge (link / attachment / check). Deliberately a view over the '
    'existing tables, not a separate graph store: one place of truth, no sync problem.';
"""

# Reverse drops the view and NOTHING else. The schema and every pre-existing
# audit.* table survive.
BACKWARD = """
DROP VIEW IF EXISTS audit.v_finding_graph;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('audit', '0003_finding_cube_and_links'),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD, reverse_sql=BACKWARD),
    ]
