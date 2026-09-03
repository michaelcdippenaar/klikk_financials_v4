"""
Who a comment is authored by — stamped by the server, not typed by the client.

The Excel add-in used to carry a free-text "Your name" box and send whatever
was in it. app.cube_comments records what that cost: `ewffew` x12 (a keyboard
mash that became a durable author), `test`, `test2`, MC's own notes split
across author_key 'MC' and '', and 55 rows authored by nobody at all.

Three properties are pinned here, and each of them is a way this could be
plausibly and quietly wrong:

* the add-in's SHARED credential must stamp the PERSON behind it, not the
  account name `excel-addin` — which is accurate and useless in a filter;
* a client that still sends an author from that credential must be IGNORED,
  not trusted — otherwise the box is gone from the pane and the hole is not;
* an AGENT on the MCP service token must still be able to declare
  `claude:year-end-audit` and `codex:fy2026-account-allocation`, because MC
  reads those strings to tell workstreams apart. Fixing MC's case by breaking
  the agents is not fixing it.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.user import identity
from apps.xero.xero_data import cube_mentions, pivot_comments

User = get_user_model()

COMMENTS = '/xero/data/journals/pivot/comments/'
IDENTITY = '/xero/data/journals/pivot/comments/identity/'

OPERATORS = {'excel-addin': 'mc@tremly.com'}


def _payload(**over):
    body = {
        'measure': 'amount',
        'row_dims': ['account_class', 'account'],
        'row_path': ['EXPENSE', '406 - Consulting'],
        'col_dims': ['fin_year'],
        'col_path': 'FY2023',
        'filters': {'tenant': '', 'journal_type': ''},
        'cell_value': 134200,
        'comment': 'check this',
    }
    body.update(over)
    return body


class _Base(TestCase):
    def setUp(self):
        self.client = APIClient()
        # The two accounts that matter: the person, and the tool they hold.
        self.mc = User.objects.create_user(username='mc@tremly.com', password='pw-not-logged')
        self.addin = User.objects.create_user(username='excel-addin', password='pw-not-logged')
        # Lazy DDL cached in a module global; TestCase rolls the DDL back too,
        # so the cache outlives the schema unless it is reset per test.
        cube_mentions._ready = False
        pivot_comments._ready = False
        pivot_comments._ensure_table()
        cube_mentions.ensure_tables()
        identity._warned.clear()


@override_settings(SERVICE_ACCOUNT_OPERATORS=OPERATORS)
class AddInAuthorTests(_Base):
    """The add-in's token — the case MC complained about."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.addin)

    def test_the_person_is_stamped_not_the_service_account(self):
        r = self.client.post(COMMENTS, _payload(), format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['author'], 'mc@tremly.com')
        self.assertEqual(r.data['author_key'], 'mc@tremly.com')
        self.assertTrue(r.data['author_verified'])

    def test_an_author_sent_by_the_client_is_ignored(self):
        # The pane no longer has the box. This is about the CREDENTIAL: a
        # copied token must not be able to sign a comment as somebody else.
        r = self.client.post(COMMENTS, _payload(author='someone else'), format='json')
        self.assertEqual(r.data['author'], 'mc@tremly.com')
        self.assertEqual(r.data['author_key'], 'mc@tremly.com')

    def test_an_empty_author_no_longer_produces_unattributed(self):
        # 46 rows in the live register are keyed 'unattributed' — the pane
        # posting with the name box empty.
        r = self.client.post(COMMENTS, _payload(author=''), format='json')
        self.assertNotEqual(r.data['author_key'], 'unattributed')
        self.assertEqual(r.data['author_key'], 'mc@tremly.com')

    def test_re_posting_the_same_cell_edits_one_row(self):
        # The register upserts on (subject, author_key). A stamped identity
        # must therefore be STABLE, or every save makes a new row.
        self.client.post(COMMENTS, _payload(comment='first'), format='json')
        r = self.client.post(COMMENTS, _payload(comment='second'), format='json')
        self.assertEqual(r.data['comment'], 'second')
        listing = self.client.get(COMMENTS, {'status': 'all'})
        self.assertEqual(listing.data['count'], 1, listing.data['results'])

    def test_the_identity_endpoint_answers_before_anything_is_written(self):
        r = self.client.get(IDENTITY)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['author'], 'mc@tremly.com')
        self.assertTrue(r.data['stamped'])
        # And it answered without writing: the pane asks this on connect.
        self.assertEqual(self.client.get(COMMENTS, {'status': 'all'}).data['count'], 0)

    @override_settings(SERVICE_ACCOUNT_OPERATORS={'excel-addin': 'nobody@nowhere'})
    def test_a_mapping_to_a_missing_user_does_not_invent_an_identity(self):
        # A typo in configuration must not be able to create an author.
        r = self.client.post(COMMENTS, _payload(), format='json')
        self.assertNotEqual(r.data['author'], 'nobody@nowhere')

    def test_a_disabled_operator_is_refused(self):
        self.mc.is_active = False
        self.mc.save(update_fields=['is_active'])
        r = self.client.post(COMMENTS, _payload(), format='json')
        self.assertNotEqual(r.data['author'], 'mc@tremly.com')


@override_settings(SERVICE_ACCOUNT_OPERATORS=OPERATORS)
class AgentAuthorTests(_Base):
    """The MCP service token — one credential, many workstreams."""

    def setUp(self):
        super().setUp()
        from klikk_business_intelligence.permissions import ServiceAccount
        self.client.force_authenticate(ServiceAccount())

    def test_an_agent_still_declares_its_own_workstream(self):
        for who in ('claude:year-end-audit', 'codex:fy2026-account-allocation'):
            r = self.client.post(COMMENTS, _payload(author=who, row_path=['EXPENSE', who]),
                                 format='json')
            self.assertEqual(r.status_code, 200, r.data)
            self.assertEqual(r.data['author'], who)
            self.assertEqual(r.data['author_key'], who)
            # A declared name is a claim, not a fact, and says so.
            self.assertFalse(r.data['author_verified'])

    def test_two_agents_on_one_cell_keep_their_own_notes(self):
        self.client.post(COMMENTS, _payload(author='claude:year-end-audit',
                                            comment='mine'), format='json')
        self.client.post(COMMENTS, _payload(author='codex:year-end-audit',
                                            comment='also mine'), format='json')
        rows = self.client.get(COMMENTS, {'status': 'all'}).data['results']
        self.assertEqual(sorted(r['author'] for r in rows),
                         ['claude:year-end-audit', 'codex:year-end-audit'])


@override_settings(SERVICE_ACCOUNT_OPERATORS=OPERATORS)
class PersonAuthorTests(_Base):
    """A real person signed in as themselves — unchanged by any of this."""

    def test_the_username_wins_over_anything_declared(self):
        self.client.force_authenticate(self.mc)
        r = self.client.post(COMMENTS, _payload(author='not me'), format='json')
        self.assertEqual(r.data['author'], 'mc@tremly.com')
        self.assertTrue(r.data['author_verified'])

    def test_an_auditor_signs_with_their_own_login(self):
        auditor = User.objects.create_user(username='anzellev@mstb.co.za',
                                           password='pw-not-logged')
        self.client.force_authenticate(auditor)
        r = self.client.post(COMMENTS, _payload(), format='json')
        self.assertEqual(r.data['author'], 'anzellev@mstb.co.za')


class OperatorResolutionTests(_Base):
    """apps.user.identity — the mapping itself."""

    @override_settings(SERVICE_ACCOUNT_OPERATORS=OPERATORS)
    def test_only_a_mapped_account_resolves(self):
        self.assertEqual(identity.service_operator(self.addin), 'mc@tremly.com')
        self.assertIsNone(identity.service_operator(self.mc))
        self.assertIsNone(identity.service_operator(None))

    @override_settings(SERVICE_ACCOUNT_OPERATORS={})
    def test_no_mapping_is_the_old_behaviour(self):
        self.assertIsNone(identity.service_operator(self.addin))
