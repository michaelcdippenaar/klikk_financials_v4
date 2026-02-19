# Remove duplicate share_code rows and enforce unique share_code

from django.db import migrations, models
from django.db.models import Q


def remove_duplicate_share_codes(apps, schema_editor):
    """Keep one mapping per non-empty share_code (smallest id), delete the rest."""
    from django.db.models import Min

    InvestecJseShareNameMapping = apps.get_model('investec', 'InvestecJseShareNameMapping')
    qs = InvestecJseShareNameMapping.objects.exclude(
        share_code__isnull=True
    ).exclude(
        share_code=''
    )
    keep_ids = set(
        qs.values('share_code').annotate(min_id=Min('id')).values_list('min_id', flat=True)
    )
    # Delete rows that have a non-empty share_code but are not the one we're keeping
    to_delete = InvestecJseShareNameMapping.objects.exclude(
        share_code__isnull=True
    ).exclude(
        share_code=''
    ).exclude(
        id__in=keep_ids
    )
    deleted_count, _ = to_delete.delete()
    if deleted_count:
        print(f'Removed {deleted_count} duplicate share_code mapping(s).')


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('investec', '0018_rename_investec_js_dividend_idx_investec_in_dividen_430afb_idx_and_more'),
    ]

    operations = [
        migrations.RunPython(remove_duplicate_share_codes, noop_reverse),
        migrations.AddConstraint(
            model_name='investecjsesharenamemapping',
            constraint=models.UniqueConstraint(
                fields=('share_code',),
                name='investec_unique_share_code',
                condition=Q(share_code__isnull=False) & ~Q(share_code=''),
            ),
        ),
    ]
