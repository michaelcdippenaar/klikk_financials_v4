# Generated manually on 2025-12-23

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('investec', '0006_sharenamemapping'),
    ]

    operations = [
        migrations.RenameModel(
            old_name='ShareNameMapping',
            new_name='InvestecJseShareNameMapping',
        ),
        migrations.AlterModelOptions(
            name='investecjsesharenamemapping',
            options={
                'ordering': ['share_name'],
                'verbose_name': 'Investec Jse Share Name Mapping',
                'verbose_name_plural': 'Investec Jse Share Name Mappings',
            },
        ),
    ]

