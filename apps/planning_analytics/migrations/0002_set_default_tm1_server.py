# Generated data migration: default TM1 server and trail balance import process

from django.db import migrations

# Default server (matches settings.base TM1_CONFIG)
DEFAULT_BASE_URL = "http://192.168.1.194:44414/api/v1"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = ""

# TI process that imports data into the trail balance cube (gl_src_trail_balance)
TRAIL_BALANCE_IMPORT_PROCESS = "cub.gl_src_trial_balance.import"


def set_default_tm1_server(apps, schema_editor):
    TM1ServerConfig = apps.get_model("planning_analytics", "TM1ServerConfig")
    TM1ProcessConfig = apps.get_model("planning_analytics", "TM1ProcessConfig")

    # Ensure the default server is the single active config
    TM1ServerConfig.objects.filter(is_active=True).update(is_active=False)
    TM1ServerConfig.objects.update_or_create(
        base_url=DEFAULT_BASE_URL,
        defaults={
            "username": DEFAULT_USERNAME,
            "password": DEFAULT_PASSWORD,
            "is_active": True,
        },
    )

    # Ensure the trail balance import process exists and is first in pipeline
    if not TM1ProcessConfig.objects.filter(process_name=TRAIL_BALANCE_IMPORT_PROCESS).exists():
        TM1ProcessConfig.objects.create(
            process_name=TRAIL_BALANCE_IMPORT_PROCESS,
            enabled=True,
            sort_order=0,
            parameters={},
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("planning_analytics", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(set_default_tm1_server, noop_reverse),
    ]
