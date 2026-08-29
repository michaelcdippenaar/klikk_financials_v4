"""Attaching a share name to a share code.

The share account is loaded by uploading statements, and the same instrument
arrives under different spellings on different ones. A name with no mapping
leaves its transactions unattributable to a holding, which is what the
workbench reports.

Two properties this service exists to hold.

The mapping table is GLOBAL — one row per share code, shared by every entity —
while the authority to change it is not. So a caller may only attach a name
that actually appears on the share account bound to their own entity. Without
that, any member of any entity could rewrite attribution for everybody.

And share_code is uniquely constrained, so a second spelling belongs in a free
slot on the existing row. Creating a row would be rejected by the database;
moving a name off another code would silently re-attribute someone's
transactions. Both are refused with a reason instead.
"""
import logging

from django.db import transaction

from apps.investec.models import InvestecJseShareNameMapping, InvestecJseTransaction
from apps.web_api_v2.services.investec_shares import bound_account

logger = logging.getLogger(__name__)

NAME_SLOTS = ('share_name', 'share_name2', 'share_name3')


class ShareMappingError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.safe_message = message


def _entity_share_names(entity_id):
    account = bound_account(entity_id)
    if account is None:
        raise ShareMappingError(
            'NOT_BOUND',
            'No Investec share account is bound to this entity, so there are no '
            'share names it may map.',
        )
    return {
        name.strip()
        for name in InvestecJseTransaction.objects
        .filter(account_number=account)
        .values_list('share_name', flat=True)
        if name and name.strip()
    }


def map_share_name(entity_id, share_name, share_code, *, actor, note=''):
    """Attach share_name to the existing row for share_code."""
    share_name = (share_name or '').strip()
    share_code = (share_code or '').strip()
    if not share_name or not share_code:
        raise ShareMappingError('VALIDATION_ERROR', 'A share name and a share code are required.')

    # The caller's authority extends to names on their own account, not to the
    # whole global table.
    if share_name not in _entity_share_names(entity_id):
        raise ShareMappingError(
            'VALIDATION_ERROR',
            f'"{share_name}" does not appear on this entity\'s share account, so it '
            f'cannot be mapped from here.',
        )

    with transaction.atomic():
        target = (
            InvestecJseShareNameMapping.objects
            .select_for_update()
            .filter(share_code=share_code)
            .first()
        )
        if target is None:
            raise ShareMappingError(
                'NOT_FOUND',
                f'No mapping row exists for share code "{share_code}". Share codes come '
                f'from loaded holdings; this one has none.',
            )

        # Already attached here: say so rather than reporting a change.
        if share_name in (getattr(target, slot) for slot in NAME_SLOTS):
            return {'changed': False, 'slot': None, 'shareCode': share_code,
                    'reason': f'"{share_name}" is already mapped to {share_code}.'}

        holder = (
            InvestecJseShareNameMapping.objects
            .filter(**{f'{NAME_SLOTS[0]}': share_name})
            .exclude(pk=target.pk).first()
            or InvestecJseShareNameMapping.objects
            .filter(share_name2=share_name).exclude(pk=target.pk).first()
            or InvestecJseShareNameMapping.objects
            .filter(share_name3=share_name).exclude(pk=target.pk).first()
        )
        if holder is not None:
            # Moving it would re-attribute existing transactions silently.
            raise ShareMappingError(
                'CONFLICT',
                f'"{share_name}" is already mapped to {holder.share_code}. Moving it '
                f'would re-attribute transactions already counted against that share.',
            )

        slot = next((s for s in NAME_SLOTS[1:] if not getattr(target, s)), None)
        if slot is None:
            raise ShareMappingError(
                'CONFLICT',
                f'The mapping for {share_code} already holds three names, which is all '
                f'the row can carry.',
            )

        setattr(target, slot, share_name)
        target.mapped_by = actor
        target.mapped_note = note or f'Mapped from the V2 share workbench.'
        target.save(update_fields=[slot, 'mapped_by', 'mapped_note', 'updated_at'])

    logger.info(
        'v2_share_mapping_applied entity=%s user=%s name=%r code=%s slot=%s',
        entity_id, getattr(actor, 'pk', '-'), share_name, share_code, slot,
    )
    return {'changed': True, 'slot': slot, 'shareCode': share_code,
            'reason': f'"{share_name}" is now mapped to {share_code}.'}


def mappable_share_codes(entity_id):
    """Share codes a name may be attached to: those with a mapping row."""
    return sorted(
        InvestecJseShareNameMapping.objects
        .exclude(share_code__isnull=True).exclude(share_code='')
        .values_list('share_code', flat=True)
        .distinct()
    )
