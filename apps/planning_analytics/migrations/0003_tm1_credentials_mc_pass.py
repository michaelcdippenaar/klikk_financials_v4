# Update default TM1 credentials to mc / pass

from django.db import migrations

DEFAULT_BASE_URL = "http://192.168.1.194:44414/api/v1"
DEFAULT_USERNAME = "mc"
DEFAULT_PASSWORD = "pass"


def update_tm1_credentials(apps, schema_editor):
    TM1ServerConfig = apps.get_model("planning_analytics", "TM1ServerConfig")
    TM1ServerConfig.objects.filter(base_url=DEFAULT_BASE_URL).update(
        username=DEFAULT_USERNAME,
        password=DEFAULT_PASSWORD,
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("planning_analytics", "0002_set_default_tm1_server"),
    ]

    operations = [
        migrations.RunPython(update_tm1_credentials, noop_reverse),
    ]
