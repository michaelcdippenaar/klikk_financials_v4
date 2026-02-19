# Generated manually on 2025-12-23

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('investec', '0010_rename_price_to_value_and_add_calculated'),
    ]

    operations = [
        migrations.AddField(
            model_name='investecjsetransaction',
            name='dividend_ttm',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=15, null=True),
        ),
    ]

