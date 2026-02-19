# Generated manually on 2025-12-26

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('investec', '0014_sharemonthlyperformance_quantity_and_more'),
    ]

    operations = [
        # Rename the model
        migrations.RenameModel(
            old_name='ShareMonthlyPerformance',
            new_name='InvestecJseShareMonthlyPerformance',
        ),
        # Add year and month fields
        migrations.AddField(
            model_name='investecjsesharemonthlyperformance',
            name='year',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='investecjsesharemonthlyperformance',
            name='month',
            field=models.IntegerField(blank=True, null=True),
        ),
        # Add index on year, month
        migrations.AddIndex(
            model_name='investecjsesharemonthlyperformance',
            index=models.Index(fields=['year', 'month'], name='investec_js_year_mon_idx'),
        ),
        # Update verbose names
        migrations.AlterModelOptions(
            name='investecjsesharemonthlyperformance',
            options={'ordering': ['-date', 'share_name'], 'verbose_name': 'Investec Jse Share Monthly Performance', 'verbose_name_plural': 'Investec Jse Share Monthly Performances'},
        ),
    ]

