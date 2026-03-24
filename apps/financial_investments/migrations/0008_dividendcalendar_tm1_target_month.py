from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('financial_investments', '0007_dividendcalendar_dividend_category'),
    ]

    operations = [
        migrations.AddField(
            model_name='dividendcalendar',
            name='tm1_target_month',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Resolved TM1 month for the adjustment (e.g. Apr). Set by TM1 probe or payment_date.',
                max_length=3,
            ),
        ),
    ]
