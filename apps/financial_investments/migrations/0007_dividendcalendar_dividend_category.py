from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('financial_investments', '0006_dividendcalendar_tm1_tracking'),
    ]

    operations = [
        migrations.AddField(
            model_name='dividendcalendar',
            name='dividend_category',
            field=models.CharField(
                choices=[('regular', 'Regular'), ('special', 'Special'), ('foreign', 'Foreign')],
                db_index=True,
                default='regular',
                help_text='regular=budgeted, special=not budgeted (only once declared), foreign=budgeted (international)',
                max_length=20,
            ),
        ),
    ]
