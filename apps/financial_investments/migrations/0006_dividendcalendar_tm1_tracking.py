from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('financial_investments', '0005_dividendcalendar_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='dividendcalendar',
            name='tm1_adjustment_value',
            field=models.DecimalField(
                blank=True, decimal_places=6, help_text='The adjustment value that was written to TM1',
                max_digits=18, null=True,
            ),
        ),
        migrations.AddField(
            model_name='dividendcalendar',
            name='tm1_written_at',
            field=models.DateTimeField(
                blank=True, help_text='When the TM1 adjustment was written', null=True,
            ),
        ),
        migrations.AddField(
            model_name='dividendcalendar',
            name='tm1_verified',
            field=models.BooleanField(
                default=False, help_text='Whether TM1 value was verified after writing',
            ),
        ),
        migrations.AddField(
            model_name='dividendcalendar',
            name='tm1_verified_at',
            field=models.DateTimeField(
                blank=True, help_text='When TM1 was last verified', null=True,
            ),
        ),
        migrations.AddField(
            model_name='dividendcalendar',
            name='last_checked_at',
            field=models.DateTimeField(
                blank=True, help_text='When yfinance was last checked for this entry', null=True,
            ),
        ),
    ]
