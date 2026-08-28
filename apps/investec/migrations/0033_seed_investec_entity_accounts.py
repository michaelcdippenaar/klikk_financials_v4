"""Carry the hard-coded bank binding into the table, unchanged.

Before this, the bank read resolved entity → owner label → INVESTEC_OWNER_MAP →
account numbers, with the entity→label step living in a Python dict. Only one
entity was ever bound. This seeds exactly the accounts that dict already
resolved to, so the change of mechanism moves no money between books.

The share side is deliberately not seeded: no share account was ever bound in
code either, and binding one attributes a real portfolio to a real entity's
books, so it waits for someone to confirm the ownership.
"""
from django.db import migrations

# The state as it stood in code, reproduced here rather than imported: a data
# migration must keep doing what it did on the day it ran, even after the
# constants it came from are deleted.
BANK_ENTITY_BINDINGS = {
    '41ebfa0e-012e-4ff1-82ba-a9a7585c536c': 'Klikk',
}


def seed(apps, schema_editor):
    from apps.investec.owner_map import INVESTEC_OWNER_MAP

    InvestecEntityAccount = apps.get_model('investec', 'InvestecEntityAccount')
    XeroTenant = apps.get_model('xero_core', 'XeroTenant')

    for tenant_id, owner in BANK_ENTITY_BINDINGS.items():
        if not XeroTenant.objects.filter(pk=tenant_id).exists():
            continue
        numbers = [
            account_number
            for account_number, attribution in INVESTEC_OWNER_MAP.items()
            if attribution['entity'] == owner
        ]
        for account_number in numbers:
            InvestecEntityAccount.objects.get_or_create(
                account_number=account_number,
                kind='BANK',
                defaults={'entity_id': tenant_id, 'active': True,
                          'note': f'Seeded from the {owner} owner-map attribution.'},
            )


def unseed(apps, schema_editor):
    InvestecEntityAccount = apps.get_model('investec', 'InvestecEntityAccount')
    InvestecEntityAccount.objects.filter(note__startswith='Seeded from the').delete()


class Migration(migrations.Migration):
    dependencies = [
        ('investec', '0032_investecentityaccount'),
        ('xero_core', '0001_initial'),
    ]
    operations = [migrations.RunPython(seed, unseed)]
