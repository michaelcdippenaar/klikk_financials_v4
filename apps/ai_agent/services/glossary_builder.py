"""
Build account and contact glossary markdown from Xero metadata for the AI agent's
vectorized knowledge base. Used so the agent understands account names/purpose and
contacts (Suppliers vs Customers).
"""
from __future__ import annotations

from apps.xero.xero_metadata.models import XeroAccount, XeroContacts


def build_account_glossary_markdown(organisation_id=None):
    """
    Build markdown listing all accounts with code, name, type, and purpose (reporting code name).
    organisation_id: XeroTenant id (optional). If None, include all organisations.
    """
    qs = XeroAccount.objects.select_related('organisation').order_by('organisation__tenant_name', 'code')
    if organisation_id is not None:
        qs = qs.filter(organisation_id=organisation_id)

    lines = [
        '# Xero account glossary',
        '',
        'Use this to understand which account to use for what. Account type and reporting code describe purpose.',
        '',
        '| Organisation | Code | Name | Type | Purpose (reporting code) |',
        '|--------------|------|------|------|-------------------------|',
    ]
    for acc in qs:
        org_name = (acc.organisation.tenant_name or '')[:30]
        code = (acc.code or '')[:12]
        name = (acc.name or '')[:50].replace('|', ' ')
        type_ = (acc.type or '')[:20]
        purpose = (acc.reporting_code_name or acc.reporting_code or '')[:40].replace('|', ' ')
        lines.append(f'| {org_name} | {code} | {name} | {type_} | {purpose} |')

    lines.append('')
    lines.append('_Types: REVENUE, EXPENSE, ASSET, LIABILITY, EQUITY, etc. Use reporting code / purpose for intended use._')
    return '\n'.join(lines)


def build_contacts_glossary_markdown(organisation_id=None):
    """
    Build markdown listing contacts with Supplier/Customer role.
    organisation_id: XeroTenant id (optional). If None, include all organisations.
    """
    qs = XeroContacts.objects.select_related('organisation').order_by('organisation__tenant_name', 'name')
    if organisation_id is not None:
        qs = qs.filter(organisation_id=organisation_id)

    lines = [
        '# Xero contacts: Suppliers and Customers',
        '',
        'Use this to understand who is a supplier (we pay them) vs customer (they pay us).',
        '',
        '| Organisation | Contact name | Supplier | Customer |',
        '|--------------|--------------|----------|----------|',
    ]
    for c in qs:
        org_name = (c.organisation.tenant_name or '')[:30]
        name = (c.name or '')[:50].replace('|', ' ')
        coll = c.collection or {}
        is_supplier = 'Yes' if coll.get('IsSupplier') else 'No'
        is_customer = 'Yes' if coll.get('IsCustomer') else 'No'
        lines.append(f'| {org_name} | {name} | {is_supplier} | {is_customer} |')

    lines.append('')
    lines.append('_Supplier: we receive bills from them. Customer: we send invoices to them._')
    return '\n'.join(lines)


def refresh_glossary_documents(project_id=None, organisation_id=None):
    """
    Create or update SystemDocument(s) for account and contact glossaries so they
    stay in the vectorized knowledge base. Call after Xero account/contact sync.
    If project_id is None, updates all projects that have a default_corpus.
    """
    from apps.ai_agent.models import AgentProject, SystemDocument

    account_md = build_account_glossary_markdown(organisation_id=organisation_id)
    contacts_md = build_contacts_glossary_markdown(organisation_id=organisation_id)

    if project_id is not None:
        projects = AgentProject.objects.filter(id=project_id, default_corpus__isnull=False)
    else:
        projects = AgentProject.objects.filter(default_corpus__isnull=False)

    updated = 0
    for project in projects:
        slug_account = f'{project.slug}-xero-account-glossary'
        slug_contacts = f'{project.slug}-xero-contacts-glossary'
        for slug, title, content in [
            (slug_account, 'Xero account glossary (names and purpose)', account_md),
            (slug_contacts, 'Xero contacts (Suppliers and Customers)', contacts_md),
        ]:
            doc, created = SystemDocument.objects.update_or_create(
                slug=slug,
                defaults={
                    'project': project,
                    'corpus': project.default_corpus,
                    'title': title,
                    'content_markdown': content,
                    'metadata': {'kind': 'xero-glossary', 'organisation_id': organisation_id},
                    'pin_to_context': False,
                    'context_order': 50,
                    'is_active': True,
                },
            )
            updated += 1
    return updated
