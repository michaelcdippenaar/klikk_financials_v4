# Add InvestecBankSyncLog for tracking last sync (incremental updates)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("investec", "0022_investecbanktransaction_fallback_key"),
    ]

    operations = [
        migrations.CreateModel(
            name="InvestecBankSyncLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(default="default", max_length=32, unique=True)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "Investec Bank Sync Log",
                "verbose_name_plural": "Investec Bank Sync Logs",
            },
        ),
    ]
