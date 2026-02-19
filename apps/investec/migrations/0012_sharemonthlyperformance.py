# Generated manually on 2025-12-23

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('investec', '0011_add_dividend_ttm'),
    ]

    operations = [
        migrations.CreateModel(
            name='ShareMonthlyPerformance',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('share_name', models.CharField(db_index=True, max_length=100)),
                ('date', models.DateField(help_text='Month End date')),
                ('dividend_ttm', models.DecimalField(decimal_places=2, max_digits=15)),
                ('closing_price', models.DecimalField(blank=True, decimal_places=2, max_digits=15, null=True)),
                ('dividend_yield', models.DecimalField(blank=True, decimal_places=4, help_text='e.g., 0.0523 for 5.23%', max_digits=10, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Share Monthly Performance',
                'verbose_name_plural': 'Share Monthly Performances',
                'ordering': ['-date', 'share_name'],
                'unique_together': {('share_name', 'date')},
            },
        ),
        migrations.AddIndex(
            model_name='sharemonthlyperformance',
            index=models.Index(fields=['share_name', 'date'], name='investec_sh_share_n_idx'),
        ),
        migrations.AddIndex(
            model_name='sharemonthlyperformance',
            index=models.Index(fields=['date'], name='investec_sh_date_idx'),
        ),
    ]

