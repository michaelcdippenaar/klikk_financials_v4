# Generated manually on 2025-12-26

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('investec', '0016_rename_investec_sh_share_n_e5ca0b_idx_investec_in_share_n_53fe0e_idx_and_more'),
    ]

    operations = [
        # Add dividend_type field (nullable first, will be populated by data migration or set to 'Dividend' for existing records)
        migrations.AddField(
            model_name='investecjsesharemonthlyperformance',
            name='dividend_type',
            field=models.CharField(blank=True, db_index=True, max_length=50, null=True),
        ),
        # Add investec_account field
        migrations.AddField(
            model_name='investecjsesharemonthlyperformance',
            name='investec_account',
            field=models.CharField(blank=True, db_index=True, max_length=50, null=True),
        ),
        # Data migration: Set dividend_type to 'Dividend' for existing records
        migrations.RunPython(
            code=lambda apps, schema_editor: apps.get_model('investec', 'InvestecJseShareMonthlyPerformance').objects.filter(dividend_type__isnull=True).update(dividend_type='Dividend'),
            reverse_code=migrations.RunPython.noop,
        ),
        # Make dividend_type non-nullable
        migrations.AlterField(
            model_name='investecjsesharemonthlyperformance',
            name='dividend_type',
            field=models.CharField(db_index=True, max_length=50),
        ),
        # Remove old unique_together and add new one with dividend_type
        migrations.AlterUniqueTogether(
            name='investecjsesharemonthlyperformance',
            unique_together=set(),
        ),
        migrations.AlterUniqueTogether(
            name='investecjsesharemonthlyperformance',
            unique_together={('share_name', 'date', 'dividend_type')},
        ),
        # Add indexes
        migrations.AddIndex(
            model_name='investecjsesharemonthlyperformance',
            index=models.Index(fields=['dividend_type'], name='investec_js_dividend_idx'),
        ),
        migrations.AddIndex(
            model_name='investecjsesharemonthlyperformance',
            index=models.Index(fields=['investec_account'], name='investec_js_account_idx'),
        ),
    ]

