"""The application must not dial TM1 merely by initialising.

Django calls AppConfig.ready() for every management command, so an unguarded
startup thread reaches an external integration during tests, migrations,
collectstatic and diagnostic shells. The 2026-08-22 postmortem records this as
a required prevention control, and the browser E2E harness confirmed it was
still live: a test run logged a connection attempt to the TM1 server.
"""

from django.test import SimpleTestCase, override_settings

from apps.ai_agent.apps import tm1_prewarm_allowed


class Tm1PrewarmBoundaryTests(SimpleTestCase):
    def test_serving_entrypoints_may_prewarm(self):
        for argv in (['uvicorn', 'klikk_business_intelligence.asgi:application'],
                     ['/usr/local/bin/gunicorn', 'wsgi'],
                     ['manage.py', 'runserver']):
            with self.subTest(argv=argv):
                self.assertTrue(tm1_prewarm_allowed(argv=argv, environ={}))

    def test_management_commands_never_prewarm(self):
        # migrate and collectstatic run from the same container entrypoint as
        # the server, immediately before it starts.
        for command in ('migrate', 'collectstatic', 'test', 'shell', 'dbshell',
                        'makemigrations', 'export_graphql_schema',
                        'provision_ingest_catalogue'):
            with self.subTest(command=command):
                self.assertFalse(
                    tm1_prewarm_allowed(argv=['manage.py', command], environ={})
                )

    def test_an_unrecognised_entrypoint_does_not_prewarm(self):
        self.assertFalse(tm1_prewarm_allowed(argv=['something-else'], environ={}))
        self.assertFalse(tm1_prewarm_allowed(argv=[], environ={}))

    def test_pytest_never_prewarms_even_from_a_serving_argv(self):
        self.assertFalse(tm1_prewarm_allowed(
            argv=['uvicorn'], environ={'PYTEST_CURRENT_TEST': 'x.py::test (call)'},
        ))

    @override_settings(AI_AGENT_TM1_PREWARM=False)
    def test_the_setting_can_disable_prewarm_outright(self):
        self.assertFalse(tm1_prewarm_allowed(argv=['uvicorn'], environ={}))

    @override_settings(AI_AGENT_TM1_PREWARM=True)
    def test_the_setting_alone_does_not_authorise_a_non_serving_process(self):
        self.assertFalse(tm1_prewarm_allowed(argv=['manage.py', 'migrate'], environ={}))
