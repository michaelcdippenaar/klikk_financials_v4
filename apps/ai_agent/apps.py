from django.apps import AppConfig


class AiAgentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.ai_agent'
    verbose_name = 'AI Agent'

    def ready(self):
        import apps.ai_agent.signals  # noqa: F401

        # Pre-warm TM1 element cache for key dimensions in a background thread.
        # This runs once on startup so the first agent query doesn't have to wait
        # for cold TM1 lookups.  Safe to fail silently (TM1 may not be reachable
        # immediately at startup).
        import threading

        def _prewarm():
            try:
                from apps.ai_agent.skills.mcp_bridge import _prewarm_element_lookups
                _prewarm_element_lookups()
            except Exception:
                pass  # TM1 not ready yet — cache will fill lazily

        threading.Thread(target=_prewarm, daemon=True, name="tm1-cache-prewarm").start()

