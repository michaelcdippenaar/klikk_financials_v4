from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('financial_investments', '0008_dividendcalendar_tm1_target_month'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='dividendcalendar',
            name='fi_divcal_symbol_exdate_unique',
        ),
        migrations.AddConstraint(
            model_name='dividendcalendar',
            constraint=models.UniqueConstraint(
                fields=['symbol', 'ex_dividend_date', 'dividend_category'],
                name='fi_divcal_symbol_exdate_cat_unique',
            ),
        ),
    ]
