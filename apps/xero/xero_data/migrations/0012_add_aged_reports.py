"""
Migration: add AgedPayable and AgedReceivable tables.

Xero's Aged Payables/Receivables by Contact reports expose six buckets:
  Current | 1 Month | 2 Months | 3 Months | Older | Total
"""
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('xero_data', '0011_add_journal_exclusion'),
        ('xero_core', '__first__'),
    ]

    operations = [
        migrations.CreateModel(
            name='AgedPayable',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tenant', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='aged_payables',
                    to='xero_core.xerotenant',
                )),
                ('contact_id', models.CharField(max_length=100, help_text='Xero ContactID (UUID)')),
                ('contact_name', models.CharField(max_length=500, blank=True, default='')),
                ('report_date', models.DateField(help_text='The "as at" date of the report')),
                ('current', models.DecimalField(max_digits=18, decimal_places=2, default=0)),
                ('one_month', models.DecimalField(max_digits=18, decimal_places=2, default=0)),
                ('two_months', models.DecimalField(max_digits=18, decimal_places=2, default=0)),
                ('three_months', models.DecimalField(max_digits=18, decimal_places=2, default=0)),
                ('older', models.DecimalField(max_digits=18, decimal_places=2, default=0)),
                ('total', models.DecimalField(max_digits=18, decimal_places=2, default=0)),
                ('synced_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Aged Payable',
                'verbose_name_plural': 'Aged Payables',
                'ordering': ['tenant', 'report_date', 'contact_name'],
            },
        ),
        migrations.AddConstraint(
            model_name='agedpayable',
            constraint=models.UniqueConstraint(
                fields=['tenant', 'contact_id', 'report_date'],
                name='xero_data_agedpayable_tenant_contact_date_uniq',
            ),
        ),
        migrations.AddIndex(
            model_name='agedpayable',
            index=models.Index(
                fields=['tenant', 'report_date'],
                name='xd_agedpayable_ten_date_idx',
            ),
        ),
        migrations.CreateModel(
            name='AgedReceivable',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tenant', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='aged_receivables',
                    to='xero_core.xerotenant',
                )),
                ('contact_id', models.CharField(max_length=100, help_text='Xero ContactID (UUID)')),
                ('contact_name', models.CharField(max_length=500, blank=True, default='')),
                ('report_date', models.DateField(help_text='The "as at" date of the report')),
                ('current', models.DecimalField(max_digits=18, decimal_places=2, default=0)),
                ('one_month', models.DecimalField(max_digits=18, decimal_places=2, default=0)),
                ('two_months', models.DecimalField(max_digits=18, decimal_places=2, default=0)),
                ('three_months', models.DecimalField(max_digits=18, decimal_places=2, default=0)),
                ('older', models.DecimalField(max_digits=18, decimal_places=2, default=0)),
                ('total', models.DecimalField(max_digits=18, decimal_places=2, default=0)),
                ('synced_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Aged Receivable',
                'verbose_name_plural': 'Aged Receivables',
                'ordering': ['tenant', 'report_date', 'contact_name'],
            },
        ),
        migrations.AddConstraint(
            model_name='agedreceivable',
            constraint=models.UniqueConstraint(
                fields=['tenant', 'contact_id', 'report_date'],
                name='xero_data_agedreceivable_tenant_contact_date_uniq',
            ),
        ),
        migrations.AddIndex(
            model_name='agedreceivable',
            index=models.Index(
                fields=['tenant', 'report_date'],
                name='xd_agedreceivable_ten_date_idx',
            ),
        ),
    ]
