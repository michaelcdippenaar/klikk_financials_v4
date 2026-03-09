# Add fallback_key for accounts where API returns no uuid/posted_order

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("investec", "0021_add_investec_bank_account_and_transaction"),
    ]

    operations = [
        migrations.AddField(
            model_name="investecbanktransaction",
            name="fallback_key",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Stable hash when API returns no uuid/posted_order; (transaction_date, value_date, action_date, amount, description).",
                max_length=64,
                null=True,
                unique=True,
            ),
        ),
    ]
