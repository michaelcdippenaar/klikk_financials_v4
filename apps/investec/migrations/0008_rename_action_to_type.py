# Generated manually on 2025-12-23

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('investec', '0007_rename_sharenamemapping_to_investecjsesharenamemapping'),
    ]

    operations = [
        migrations.RenameField(
            model_name='investecjsetransaction',
            old_name='action',
            new_name='type',
        ),
    ]

