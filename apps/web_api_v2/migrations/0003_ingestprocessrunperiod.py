import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


def _valid_period(value):
    return (
        isinstance(value, str)
        and len(value) == 7
        and value[4] == '-'
        and value[:4].isdigit()
        and value[5:].isdigit()
        and 1 <= int(value[5:]) <= 12
    )


def backfill_explicit_run_periods(apps, schema_editor):
    Run = apps.get_model('web_api_v2', 'IngestProcessRun')
    RunPeriod = apps.get_model('web_api_v2', 'IngestProcessRunPeriod')
    pending = []
    for run in Run.objects.only('pk', 'periods').iterator(chunk_size=500):
        if not isinstance(run.periods, list):
            continue
        for period in sorted({value for value in run.periods if _valid_period(value)}):
            pending.append(RunPeriod(run_id=run.pk, period=period))
            if len(pending) >= 1000:
                RunPeriod.objects.bulk_create(pending, ignore_conflicts=True, batch_size=1000)
                pending = []
    if pending:
        RunPeriod.objects.bulk_create(pending, ignore_conflicts=True, batch_size=1000)


class Migration(migrations.Migration):
    dependencies = [
        ('web_api_v2', '0002_ingestprocessrun_ingestprocessauditevent_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='IngestProcessRunPeriod',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'period',
                    models.CharField(
                        max_length=7,
                        validators=[
                            django.core.validators.RegexValidator(
                                message='Period must use YYYY-MM.',
                                regex=r'^\d{4}-(0[1-9]|1[0-2])$',
                            ),
                        ],
                    ),
                ),
                (
                    'run',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='run_periods',
                        to='web_api_v2.ingestprocessrun',
                    ),
                ),
            ],
            options={'ordering': ('period', 'pk')},
        ),
        migrations.AddIndex(
            model_name='ingestprocessrunperiod',
            index=models.Index(
                fields=['period', 'run'],
                name='web_api_v2_period_run_idx',
            ),
        ),
        migrations.AddConstraint(
            model_name='ingestprocessrunperiod',
            constraint=models.UniqueConstraint(
                fields=('run', 'period'),
                name='web_api_v2_unique_run_period',
            ),
        ),
        migrations.RunPython(backfill_explicit_run_periods, migrations.RunPython.noop),
    ]
