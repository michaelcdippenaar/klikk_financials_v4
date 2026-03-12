# Generated manually for financial_investments

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Symbol',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('symbol', models.CharField(db_index=True, max_length=20, unique=True)),
                ('name', models.CharField(blank=True, max_length=255)),
                ('exchange', models.CharField(blank=True, max_length=50)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Symbol',
                'verbose_name_plural': 'Symbols',
                'ordering': ['symbol'],
            },
        ),
        migrations.CreateModel(
            name='PricePoint',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(db_index=True)),
                ('open', models.DecimalField(decimal_places=4, max_digits=18)),
                ('high', models.DecimalField(decimal_places=4, max_digits=18)),
                ('low', models.DecimalField(decimal_places=4, max_digits=18)),
                ('close', models.DecimalField(decimal_places=4, max_digits=18)),
                ('volume', models.BigIntegerField(blank=True, null=True)),
                ('adjusted_close', models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('symbol', models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name='price_points', to='financial_investments.symbol')),
            ],
            options={
                'verbose_name': 'Price point',
                'verbose_name_plural': 'Price points',
                'ordering': ['-date'],
            },
        ),
        migrations.AddConstraint(
            model_name='pricepoint',
            constraint=models.UniqueConstraint(fields=('symbol', 'date'), name='financial_investments_symbol_date_unique'),
        ),
        migrations.AddIndex(
            model_name='pricepoint',
            index=models.Index(fields=['symbol', 'date'], name='fi_symbol_date_idx'),
        ),
    ]
