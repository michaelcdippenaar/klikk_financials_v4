"""Add the ``service_readonly`` role and widen ``role`` to hold it.

``service_readonly`` is exactly 16 characters, which the old max_length=16
held with zero headroom — the next service role would have needed a second
ALTER. Widened to 32 in the same migration rather than later.

Choices are enforced at the form/serializer layer, not by a DB constraint, so
this ALTER only changes the column width; no existing row is rewritten and no
account changes role here. Applying the role to the ``excel-addin`` user is a
deliberate one-off data change, done by hand — see excel_addin/README.md.
"""


from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0003_user_must_change_password'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(choices=[('standard', 'Standard'), ('auditor', 'Auditor'), ('service_readonly', 'Service (read-only)')], default='standard', max_length=32),
        ),
    ]
