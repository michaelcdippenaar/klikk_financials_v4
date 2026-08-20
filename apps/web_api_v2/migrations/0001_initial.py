import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('xero_core', '0005_xeroapiquota'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserEntityMembership',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(choices=[('OWNER', 'Owner'), ('ADMIN', 'Administrator'), ('REVIEWER', 'Reviewer'), ('VIEWER', 'Viewer')], default='VIEWER', max_length=16)),
                ('active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('entity', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='user_memberships', to='xero_core.xerotenant')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='entity_memberships', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ('entity__tenant_name', 'entity_id')},
        ),
        migrations.CreateModel(
            name='ViewerPreference',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('default_financial_year', models.PositiveSmallIntegerField(blank=True, null=True, validators=[django.core.validators.MinValueValidator(1900), django.core.validators.MaxValueValidator(9999)])),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('default_entity', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='xero_core.xerotenant')),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='web_api_v2_preferences', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddIndex(
            model_name='userentitymembership',
            index=models.Index(fields=['user', 'active'], name='web_api_v2_user_active_idx'),
        ),
        migrations.AddConstraint(
            model_name='userentitymembership',
            constraint=models.UniqueConstraint(fields=('user', 'entity'), name='web_api_v2_unique_user_entity'),
        ),
    ]
