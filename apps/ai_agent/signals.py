"""
Signals:
- SystemDocument content changes → remove embeddings so next vectorize re-embeds.
- AgentMessage saved → broadcast to WebSocket observers.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import AgentMessage, AgentToolExecutionLog, KnowledgeChunkEmbedding, SystemDocument


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


@receiver(post_save, sender=AgentMessage)
def broadcast_agent_message(sender, instance, created=False, **kwargs):
    """Broadcast new AgentMessage to WebSocket observers."""
    if not created:
        return
    try:
        from .consumers import broadcast_message
        broadcast_message({
            'type': 'message',
            'message_id': instance.pk,
            'session_id': instance.session_id,
            'role': instance.role,
            'content': instance.content[:2000],
            'metadata': instance.metadata if hasattr(instance, 'metadata') else {},
            'created_at': instance.created_at.isoformat() if instance.created_at else None,
        })
    except Exception:
        pass


@receiver(post_save, sender=AgentToolExecutionLog)
def broadcast_tool_execution(sender, instance, created=False, **kwargs):
    """Broadcast tool execution events to WebSocket observers."""
    try:
        from .consumers import broadcast_message
        broadcast_message({
            'type': 'tool_execution',
            'execution_id': instance.pk,
            'session_id': instance.session_id,
            'tool_name': instance.tool_name,
            'status': instance.status,
            'input_payload': instance.input_payload,
            'output_payload': instance.output_payload if instance.status == 'success' else {},
            'error_message': instance.error_message,
            'created': created,
        })
    except Exception:
        pass
