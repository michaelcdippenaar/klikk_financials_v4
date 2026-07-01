from django.apps import AppConfig


class PersonalExpensesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.personal_expenses'
    label = 'personal_expenses'
    verbose_name = 'Personal Expenses'
