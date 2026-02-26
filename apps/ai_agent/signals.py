"""
Signals to keep the vectorized knowledge base in sync:
- When a SystemDocument's content changes, remove its embeddings so the next vectorize run re-embeds.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import KnowledgeChunkEmbedding, SystemDocument


@receiver(post_save, sender=SystemDocument)
def invalidate_chunks_on_doc_save(sender, instance, update_fields=None, **kwargs):
    """When a system document's content is saved, delete its chunk embeddings so next vectorize re-embeds."""
    if not instance.pk:
        return
    # Only invalidate when content actually changed (avoid clearing on pin_to_context toggle only).
    if update_fields is not None and 'content_markdown' not in update_fields:
        return
    deleted, _ = KnowledgeChunkEmbedding.objects.filter(system_document_id=instance.pk).delete()
    if deleted:
        pass
