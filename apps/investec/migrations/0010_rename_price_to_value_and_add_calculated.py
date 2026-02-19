# Generated manually on 2025-12-23

from django.db import migrations, models
from decimal import Decimal


def convert_cents_to_rands_and_calculate(apps, schema_editor):
    """Convert price_per_share_cents to value_per_share (divide by 100) and calculate value_calculated."""
    InvestecJseTransaction = apps.get_model('investec', 'InvestecJseTransaction')
    
    for transaction in InvestecJseTransaction.objects.exclude(price_per_share_cents__isnull=True):
        # Convert from cents to rands (divide by 100)
        transaction.value_per_share = transaction.price_per_share_cents / Decimal('100')
        
        # Calculate value_calculated = value_per_share * quantity
        if transaction.value_per_share and transaction.quantity:
            transaction.value_calculated = transaction.value_per_share * transaction.quantity
            
            # Make negative for Buy transactions
            if transaction.type == 'Buy':
                transaction.value_calculated = transaction.value_calculated * Decimal('-1')
        
        transaction.save()


def reverse_conversion(apps, schema_editor):
    """Reverse: convert value_per_share back to cents (multiply by 100)."""
    InvestecJseTransaction = apps.get_model('investec', 'InvestecJseTransaction')
    
    for transaction in InvestecJseTransaction.objects.exclude(value_per_share__isnull=True):
        # Convert from rands back to cents (multiply by 100)
        transaction.price_per_share_cents = transaction.value_per_share * Decimal('100')
        transaction.save()


class Migration(migrations.Migration):

    dependencies = [
        ('investec', '0009_add_price_per_share_cents'),
    ]

    operations = [
        # First, add the new fields
        migrations.AddField(
            model_name='investecjsetransaction',
            name='value_per_share',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=15, null=True),
        ),
        migrations.AddField(
            model_name='investecjsetransaction',
            name='value_calculated',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=15, null=True),
        ),
        # Convert existing data
        migrations.RunPython(convert_cents_to_rands_and_calculate, reverse_conversion),
        # Remove the old field
        migrations.RemoveField(
            model_name='investecjsetransaction',
            name='price_per_share_cents',
        ),
    ]

