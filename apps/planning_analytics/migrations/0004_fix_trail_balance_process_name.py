# Fix trail balance import process name: use TM1 convention cub.gl_src_trial_balance.import

from django.db import migrations

# Correct TI process name (trail balance / gl_src_trail_balance)
CORRECT_PROCESS_NAME = "cub.gl_src_trial_balance.import"


def fix_trail_balance_process_name(apps, schema_editor):
    TM1ProcessConfig = apps.get_model("planning_analytics", "TM1ProcessConfig")
    # Fix the wrong underscore-only name if it exists
    TM1ProcessConfig.objects.filter(process_name="cub_trail_balance_import").update(
        process_name=CORRECT_PROCESS_NAME
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("planning_analytics", "0003_tm1_credentials_mc_pass"),
    ]

    operations = [
        migrations.RunPython(fix_trail_balance_process_name, noop_reverse),
    ]
