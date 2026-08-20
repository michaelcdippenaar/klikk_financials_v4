"""
Tests for the read-only knowledge-base API (apps.kb).

The ``kb`` schema is a local register maintained outside Django migrations
(seeded by the klikk-books-kb build, 2026-08-20), so the test database does not
contain it. setUpClass creates a minimal copy of the schema inside the class-wide
test transaction and seeds one row per table; everything rolls back afterwards.

Pins:
1. The lockdown gate: every /api/kb/ endpoint is 401 anonymous, usable authed.
2. Each endpoint returns the seeded content (search actually matches via FTS,
   filters actually filter).

Run:
    python manage.py test apps.kb -v 2
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()

KB_DDL = """
CREATE SCHEMA IF NOT EXISTS kb;
CREATE TABLE IF NOT EXISTS kb.events (
  event_name text PRIMARY KEY, tracking_option text,
  invoiced_by text NOT NULL DEFAULT 'Klikk (Pty) Ltd', customer text,
  window_start date, window_end date, venue text, income numeric, costs numeric,
  notes text, source text NOT NULL DEFAULT 'test',
  reviewed_by_mc boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kb.documents (
  slug text PRIMARY KEY,
  title text NOT NULL,
  body text NOT NULL,
  source text NOT NULL DEFAULT 'test',
  updated_at timestamptz NOT NULL DEFAULT now(),
  fts tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || body)) STORED
);
CREATE TABLE IF NOT EXISTS kb.supplier_rules (
  contact_pattern text NOT NULL, lines integer, total_spend numeric,
  expected_account text, account_name text, dominant_pct integer,
  expected_tax text, expected_tracking1 text, rule_strength text, notes text,
  source text NOT NULL DEFAULT 'test', reviewed_by_mc boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kb.customer_rules (
  contact_pattern text NOT NULL, lines integer, total_income numeric,
  expected_account text, account_name text, dominant_pct integer,
  expected_tax text, expected_tracking1 text, rule_strength text, notes text,
  source text NOT NULL DEFAULT 'test', reviewed_by_mc boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kb.account_dictionary (
  code text, name text NOT NULL, type text, n_lines integer, net_amt numeric,
  last_used date, meaning text, deductibility text, when_to_use text,
  when_not_to_use text, source text NOT NULL DEFAULT 'test',
  reviewed_by_mc boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kb.tracking_dictionary (
  category_slot integer, category text NOT NULL, option text NOT NULL,
  option_id uuid, meaning text, applies_to text,
  source text NOT NULL DEFAULT 'test', reviewed_by_mc boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);
"""

KB_SEED = """
INSERT INTO kb.documents (slug, title, body) VALUES
  ('01-processing-rules', 'Processing rules',
   'Personal spend from the bank account goes to the director loan accounts, never the profit and loss.'),
  ('02-accounts', 'Chart of accounts', 'The prefix system: PM is property maintenance.');
INSERT INTO kb.supplier_rules
  (contact_pattern, lines, total_spend, expected_account, account_name,
   dominant_pct, expected_tax, expected_tracking1, rule_strength) VALUES
  ('Herotel', 281, 202333, 'PM--IT01', 'Property / Internet', 99, 'NONE', '6 Irene Park', 'hard'),
  ('BUILDERS', 500, 400000, 'PB--CO01', 'Consumables', 22, 'NONE', NULL, 'info');
INSERT INTO kb.customer_rules
  (contact_pattern, lines, total_income, expected_account, account_name,
   dominant_pct, expected_tax, expected_tracking1, rule_strength) VALUES
  ('M C Dippenaar Boerdery', 6, 6402265, 'PA--RI01', 'Rental Income - Agricultural',
   100, 'OUTPUT3', 'Goedeverwagting Farm', 'hard');
INSERT INTO kb.account_dictionary (code, name, type, n_lines) VALUES
  ('PM--SE01', 'Property / Security Monthly Fee', 'EXPENSE', 4000),
  ('883', 'Loan - MC Dippenaar', 'LIABILITY', 3000);
INSERT INTO kb.events (event_name, tracking_option, invoiced_by, window_start, window_end) VALUES
  ('EarthDance 2025', 'Event - EarthDance 2025', 'Klikk (Pty) Ltd', '2025-09-03', '2025-10-03'),
  ('Craig party 2026', NULL, 'Dippenaar Family', '2026-07-01', '2026-07-14');
INSERT INTO kb.tracking_dictionary (category_slot, category, option) VALUES
  (1, 'Profit Center', '4 Otterkuil'),
  (3, 'Custom Tracking', 'Personal - Groceries');
"""

ENDPOINTS = [
    '/api/kb/documents/',
    '/api/kb/documents/01-processing-rules/',
    '/api/kb/search/?q=personal',
    '/api/kb/suppliers/',
    '/api/kb/customers/',
    '/api/kb/accounts/',
    '/api/kb/tracking/',
    '/api/kb/events/',
]


class KbApiTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cur:
            cur.execute(KB_DDL)
            cur.execute(KB_SEED)
        cls.user = User.objects.create_user(username='kb-reader', password='x')

    def authed(self):
        client = APIClient()
        client.force_authenticate(self.user)
        return client

    # -- the lockdown gate -------------------------------------------------

    def test_anonymous_is_401_everywhere(self):
        client = APIClient()
        for url in ENDPOINTS:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 401)

    def test_authenticated_is_not_auth_rejected(self):
        client = self.authed()
        for url in ENDPOINTS:
            with self.subTest(url=url):
                self.assertNotIn(client.get(url).status_code, (401, 403))

    # -- content -----------------------------------------------------------

    def test_documents_list_and_read(self):
        client = self.authed()
        slugs = [d['slug'] for d in client.get('/api/kb/documents/').json()]
        self.assertIn('01-processing-rules', slugs)
        doc = client.get('/api/kb/documents/01-processing-rules/').json()
        self.assertIn('director loan accounts', doc['body'])
        self.assertEqual(client.get('/api/kb/documents/nope/').status_code, 404)

    def test_search_matches_and_requires_q(self):
        client = self.authed()
        hits = client.get('/api/kb/search/?q=personal loan').json()
        self.assertEqual(hits[0]['slug'], '01-processing-rules')
        self.assertIn('snippets', hits[0])
        self.assertEqual(client.get('/api/kb/search/').status_code, 400)

    def test_supplier_lookup_filters_and_legend(self):
        client = self.authed()
        body = client.get('/api/kb/suppliers/?name=hero').json()
        self.assertEqual(len(body['rules']), 1)
        rule = body['rules'][0]
        self.assertEqual(rule['expected_account'], 'PM--IT01')
        self.assertEqual(rule['rule_strength'], 'hard')
        self.assertIn('hard', body['legend'])
        hard_only = client.get('/api/kb/suppliers/?strength=hard').json()['rules']
        self.assertEqual([r['contact_pattern'] for r in hard_only], ['Herotel'])

    def test_customer_lookup(self):
        client = self.authed()
        rules = client.get('/api/kb/customers/?name=boerdery').json()['rules']
        self.assertEqual(rules[0]['expected_tax'], 'OUTPUT3')

    def test_account_lookup(self):
        client = self.authed()
        rows = client.get('/api/kb/accounts/?q=PM--SE01').json()
        self.assertEqual(rows[0]['name'], 'Property / Security Monthly Fee')

    def test_events_window_screen(self):
        client = self.authed()
        rows = client.get('/api/kb/events/').json()
        self.assertEqual(len(rows), 2)
        hits = client.get('/api/kb/events/?on=2025-09-20').json()
        self.assertEqual([e['event_name'] for e in hits], ['EarthDance 2025'])
        self.assertEqual(client.get('/api/kb/events/?on=2024-01-01').json(), [])
        named = client.get('/api/kb/events/?q=craig').json()
        self.assertEqual(named[0]['invoiced_by'], 'Dippenaar Family')

    def test_tracking_slot_filter(self):
        client = self.authed()
        rows = client.get('/api/kb/tracking/?slot=3').json()
        self.assertEqual([r['option'] for r in rows], ['Personal - Groceries'])
        self.assertEqual(client.get('/api/kb/tracking/?slot=x').status_code, 400)
