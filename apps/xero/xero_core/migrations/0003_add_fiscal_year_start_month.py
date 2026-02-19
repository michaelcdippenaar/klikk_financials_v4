# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('xero_core', '0002_use_tracking_category_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='xerotenant',
            name='fiscal_year_start_month',
            field=models.IntegerField(
                blank=True,
                help_text='Month when fiscal year starts (1-12). Fetched from Xero Organisation. Default 7 (July) if not set.',
                null=True
            ),
        ),
    ]
