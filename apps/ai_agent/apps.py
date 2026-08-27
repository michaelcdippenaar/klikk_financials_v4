import os
import sys

from django.apps import AppConfig
from django.conf import settings


# Only a process that actually serves requests may open a connection to TM1 at
# startup. Django runs AppConfig.ready() for every management command, so an
# unguarded prewarm dials an external integration during tests, migrations,
# collectstatic and any diagnostic shell. That makes ordinary ORM diagnosis
# unsafe, and pulls CI runners onto an internal network they have no business
# reaching. The 2026-08-22 postmortem names this as a required control.
#
# The allow-list is deliberate: an unrecognised entrypoint does not prewarm.
SERVING_ENTRYPOINTS = frozenset({'uvicorn', 'gunicorn', 'daphne', 'hypercorn'})
SERVING_COMMANDS = frozenset({'runserver'})


def tm1_prewarm_allowed(argv=None, environ=None):
    """Whether this process may prewarm the TM1 element cache on startup."""
    if not getattr(settings, 'AI_AGENT_TM1_PREWARM', True):
        return False

    environ = os.environ if environ is None else environ
    # pytest never goes through manage.py, so argv alone would not catch it.
    if environ.get('PYTEST_CURRENT_TEST'):
        return False

    argv = sys.argv if argv is None else argv
    if not argv:
        return False

    program = os.path.basename(argv[0]).split('.')[0]
    if program in SERVING_ENTRYPOINTS:
        return True
    # manage.py runserver, and nothing else that goes through manage.py.
    return len(argv) > 1 and argv[1] in SERVING_COMMANDS


class AiAgentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.ai_agent'
    verbose_name = 'AI Agent'

    def ready(self):
        import apps.ai_agent.signals  # noqa: F401

        if not tm1_prewarm_allowed():
            return

        # Pre-warm the TM1 element cache for key dimensions in a background
        # thread so the first agent query does not wait on cold lookups.
        # Safe to fail silently: TM1 may not be reachable yet at startup.
        import threading

        def _prewarm():
            try:
                from apps.ai_agent.skills.mcp_bridge import _prewarm_element_lookups
                _prewarm_element_lookups()
            except Exception:
                pass  # TM1 not ready yet — cache will fill lazily

        threading.Thread(target=_prewarm, daemon=True, name="tm1-cache-prewarm").start()
