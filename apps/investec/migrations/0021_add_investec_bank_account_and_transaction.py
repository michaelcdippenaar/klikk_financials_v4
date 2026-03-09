# Generated manually for Investec Private Banking (bank) models

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('investec', '0020_add_share_name2_share_name3'),
    ]

    operations = [
        migrations.CreateModel(
            name='InvestecBankAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('account_id', models.CharField(db_index=True, max_length=40, unique=True)),
                ('account_number', models.CharField(max_length=40)),
                ('account_name', models.CharField(blank=True, max_length=70)),
                ('reference_name', models.CharField(blank=True, max_length=70)),
                ('product_name', models.CharField(blank=True, max_length=70)),
                ('kyc_compliant', models.BooleanField(default=False)),
                ('profile_id', models.CharField(blank=True, max_length=70)),
                ('profile_name', models.CharField(blank=True, max_length=70)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Investec Bank Account',
                'verbose_name_plural': 'Investec Bank Accounts',
                'ordering': ['account_number'],
            },
        ),
        migrations.CreateModel(
            name='InvestecBankTransaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('type', models.CharField(choices=[('CREDIT', 'Credit'), ('DEBIT', 'Debit')], max_length=10)),
                ('transaction_type', models.CharField(blank=True, db_index=True, max_length=40)),
                ('status', models.CharField(choices=[('POSTED', 'Posted'), ('PENDING', 'Pending')], db_index=True, max_length=10)),
                ('description', models.CharField(blank=True, max_length=255)),
                ('card_number', models.CharField(blank=True, max_length=40)),
                ('posted_order', models.IntegerField(blank=True, null=True)),
                ('posting_date', models.DateField(blank=True, null=True)),
                ('value_date', models.DateField(blank=True, null=True)),
                ('action_date', models.DateField(blank=True, null=True)),
                ('transaction_date', models.DateField(blank=True, db_index=True, null=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=15)),
                ('running_balance', models.DecimalField(blank=True, decimal_places=2, max_digits=15, null=True)),
                ('uuid', models.CharField(blank=True, db_index=True, max_length=40, null=True, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('account', models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name='transactions', to='investec.investecbankaccount')),
            ],
            options={
                'verbose_name': 'Investec Bank Transaction',
                'verbose_name_plural': 'Investec Bank Transactions',
                'ordering': ['-posting_date', '-posted_order'],
            },
        ),
        migrations.AddIndex(
            model_name='investecbanktransaction',
            index=models.Index(fields=['account', 'posting_date'], name='investec_in_account_8a1f2a_idx'),
        ),
        migrations.AddIndex(
            model_name='investecbanktransaction',
            index=models.Index(fields=['status'], name='investec_in_status_2c4b8b_idx'),
        ),
        migrations.AddConstraint(
            model_name='investecbanktransaction',
            constraint=models.UniqueConstraint(
                condition=models.Q(posting_date__isnull=False, posted_order__isnull=False),
                fields=('account', 'posting_date', 'posted_order'),
                name='investec_bank_txn_account_posting_order',
            ),
        ),
    ]
