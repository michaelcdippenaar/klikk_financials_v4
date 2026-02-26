from django.apps import AppConfig


class AiAgentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.ai_agent'
    verbose_name = 'AI Agent'

    def ready(self):
        import apps.ai_agent.signals  # noqa: F401

