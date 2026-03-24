from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.xero.xero_core.models import XeroTenant
from apps.planning_analytics.models import TM1ServerConfig, UserTM1Credentials

from django.utils.text import slugify

from .models import (
    AgentMessage, AgentProject, AgentSession, AgentToolExecutionLog,
    SystemDocument, SkillRegistry, Credential,
)
from .services.chat_runner import (
    build_context_messages,
    generate_assistant_reply,
    generate_assistant_reply_with_tools,
    generate_assistant_reply_with_tool_use,
    plan_tool_calls,
    user_wants_vectorized_knowledge,
)
from .services.tm1_proxy import tm1_request, tm1_test_connection, tm1_get_version
from .services.system_doc_builder import BuildOptions, build_system_document_markdown
from .services.cursor_chat_import import import_cursor_chat_transcript
from .services.session_transcript import build_session_transcript_markdown
from .services.tm1_docs import TM1DocsOptions, build_tm1_docs, build_tm1_docs_bundle
from .services.vector_store import semantic_search_chunks, vectorize_corpus_documents


def _security_disabled():
    return bool(getattr(settings, 'AI_AGENT_DISABLE_SECURITY', False))


def _effective_user(request):
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return user
    return None


def _get_user_tm1_creds(request):
    """Return (tm1_username, tm1_password) for the authenticated user, or (None, None)."""
    user = _effective_user(request)
    if user:
        try:
            creds = user.tm1_credentials
            return creds.tm1_username, creds.tm1_password
        except UserTM1Credentials.DoesNotExist:
            pass
    return None, None


def _sessions_qs(request):
    if _security_disabled():
        return AgentSession.objects.all()
    return AgentSession.objects.filter(created_by=request.user)


def _get_session_for_request(request, session_id: int):
    return _sessions_qs(request).filter(id=session_id).select_related('organisation', 'project').first()


def _projects_qs(request):
    if _security_disabled():
        return AgentProject.objects.all()
    return AgentProject.objects.filter(created_by=request.user)


def _get_project_for_request(request, project_id: int):
    return _projects_qs(request).filter(id=project_id).first()


def _ai_agent_permission_classes():
    """
    Development escape hatch.
    When enabled, all ai_agent endpoints become unauthenticated (AllowAny).
    """
    if getattr(settings, 'AI_AGENT_DISABLE_SECURITY', False):
        return [AllowAny]
    return [IsAuthenticated]


AI_AGENT_PERMISSION_CLASSES = _ai_agent_permission_classes()


def _safe_int(value, default: int, min_v: int, max_v: int) -> int:
    try:
        v = int(value)
    except Exception:
        v = default
    return max(min_v, min(v, max_v))


class AgentHealthView(APIView):
    permission_classes = []

    def get(self, request):
        return Response({'success': True, 'message': 'ai_agent is running'})


class AgentStatusView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request):
        gemini_key = getattr(settings, 'AI_AGENT_GEMINI_API_KEY', None)
        openai_key = getattr(settings, 'AI_AGENT_OPENAI_API_KEY', None)
        model_name = getattr(settings, 'AI_AGENT_GEMINI_MODEL', 'gemini-1.5-flash') if gemini_key else getattr(settings, 'AI_AGENT_MODEL', 'claude-3-5-sonnet-20241022')
        tm1_cfg = TM1ServerConfig.get_active()

        return Response({
            'success': True,
            'providers': {
                'gemini_configured': bool(gemini_key),
                'openai_configured': bool(openai_key),
                'active_model': model_name,
            },
            'security': {
                'disabled': bool(getattr(settings, 'AI_AGENT_DISABLE_SECURITY', False)),
            },
            'tm1': {
                'configured': bool(tm1_cfg and tm1_cfg.base_url),
                'base_url': tm1_cfg.base_url if tm1_cfg and tm1_cfg.base_url else '',
                'username': tm1_cfg.username if tm1_cfg and tm1_cfg.username else '',
            },
        })


class GlossaryRefreshView(APIView):
    """
    Refresh account and contact glossary docs from Xero metadata so the vectorized
    model understands account names/purpose and Suppliers vs Customers.
    Optionally re-vectorize the project's default corpus.
    """
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def post(self, request):
        from .services.glossary_builder import refresh_glossary_documents

        project_id = request.data.get('project_id')
        if project_id is not None:
            try:
                project_id = int(project_id)
            except (TypeError, ValueError):
                return Response({'error': 'project_id must be an integer'}, status=status.HTTP_400_BAD_REQUEST)
            project = _get_project_for_request(request, project_id)
            if not project:
                return Response({'error': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)
        else:
            project_id = None

        vectorize_after = bool(request.data.get('vectorize', False))
        updated = refresh_glossary_documents(project_id=project_id, organisation_id=None)

        result = {'success': True, 'docs_updated': updated}
        if vectorize_after and project_id is not None:
            project = _get_project_for_request(request, project_id)
            if project and project.default_corpus_id:
                try:
                    vec_result = vectorize_corpus_documents(
                        corpus=project.default_corpus,
                        project_id=project.id,
                        force=True,
                    )
                    result['vectorized'] = {
                        'corpus_id': vec_result.corpus_id,
                        'documents_seen': vec_result.documents_seen,
                        'chunks_written': vec_result.chunks_written,
                    }
                except Exception as e:
                    result['vectorize_error'] = str(e)
        elif vectorize_after and project_id is None:
            result['vectorize_error'] = 'Pass project_id when using vectorize=true'

        return Response(result)


class AgentSessionListCreateView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request):
        project_id = request.query_params.get('project_id')
        qs = _sessions_qs(request).select_related('organisation', 'project')
        if project_id:
            try:
                qs = qs.filter(project_id=int(project_id))
            except Exception:
                return Response({'error': 'project_id must be an integer'}, status=status.HTTP_400_BAD_REQUEST)

        sessions = qs[:200]
        data = [
            {
                'id': s.id,
                'title': s.title,
                'status': s.status,
                'project': {
                    'id': s.project_id,
                    'slug': s.project.slug,
                    'name': s.project.name,
                } if s.project else None,
                'organisation': {
                    'tenant_id': s.organisation.tenant_id,
                    'tenant_name': s.organisation.tenant_name,
                } if s.organisation else None,
                'created_at': s.created_at,
                'updated_at': s.updated_at,
            }
            for s in sessions
        ]
        return Response(data)

    def post(self, request):
        title = request.data.get('title', '') or 'New AI Agent Session'
        tenant_id = request.data.get('tenant_id')
        project_id = request.data.get('project_id')
        organisation = None
        if tenant_id:
            organisation = XeroTenant.objects.filter(tenant_id=tenant_id).first()
            if not organisation:
                return Response({'error': 'Invalid tenant_id'}, status=status.HTTP_400_BAD_REQUEST)

        project = None
        if project_id:
            try:
                project = _get_project_for_request(request, int(project_id))
            except Exception:
                project = None
            if not project:
                return Response({'error': 'Invalid project_id'}, status=status.HTTP_400_BAD_REQUEST)

        session = AgentSession.objects.create(
            title=title,
            organisation=organisation,
            project=project,
            created_by=_effective_user(request),
        )
        return Response(
            {
                'id': session.id,
                'title': session.title,
                'status': session.status,
                'project_id': session.project_id,
                'tenant_id': organisation.tenant_id if organisation else None,
                'created_at': session.created_at,
            },
            status=status.HTTP_201_CREATED,
        )


class AgentSessionMessageCreateView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request, session_id):
        session = _get_session_for_request(request, session_id)
        if not session:
            return Response({'error': 'Session not found'}, status=status.HTTP_404_NOT_FOUND)

        messages = session.messages.all().order_by('id')[:500]
        data = [
            {
                'id': m.id,
                'role': m.role,
                'content': m.content,
                'metadata': m.metadata,
                'created_at': m.created_at,
            }
            for m in messages
        ]
        return Response(data)

    def post(self, request, session_id):
        session = _get_session_for_request(request, session_id)
        if not session:
            return Response({'error': 'Session not found'}, status=status.HTTP_404_NOT_FOUND)

        content = (request.data.get('content') or '').strip()
        if not content:
            return Response({'error': 'content is required'}, status=status.HTTP_400_BAD_REQUEST)

        message = AgentMessage.objects.create(
            session=session,
            role=AgentMessage.ROLE_USER,
            content=content,
            metadata=request.data.get('metadata') or {},
            created_by=_effective_user(request),
        )
        session.updated_at = timezone.now()
        session.save(update_fields=['updated_at'])
        return Response(
            {
                'id': message.id,
                'session_id': session.id,
                'role': message.role,
                'content': message.content,
                'metadata': message.metadata,
                'created_at': message.created_at,
            },
            status=status.HTTP_201_CREATED,
        )


class AgentSessionMemoryView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request, session_id):
        session = _get_session_for_request(request, session_id)
        if not session:
            return Response({'error': 'Session not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            'session_id': session.id,
            'memory': session.memory or {},
        })

    def post(self, request, session_id):
        session = _get_session_for_request(request, session_id)
        if not session:
            return Response({'error': 'Session not found'}, status=status.HTTP_404_NOT_FOUND)

        replace = bool(request.data.get('replace', False))
        memory_update = request.data.get('memory', None)
        if memory_update is None:
            return Response({'error': 'memory is required'}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(memory_update, dict):
            return Response({'error': 'memory must be an object/dict'}, status=status.HTTP_400_BAD_REQUEST)

        if replace:
            new_memory = memory_update
        else:
            current = session.memory or {}
            if not isinstance(current, dict):
                current = {}
            new_memory = dict(current)
            for k, v in memory_update.items():
                if v is None:
                    new_memory.pop(k, None)
                else:
                    new_memory[k] = v

        session.memory = new_memory
        session.save(update_fields=['memory', 'updated_at'])
        return Response({
            'session_id': session.id,
            'memory': session.memory or {},
        })


class AgentSessionImportCursorChatView(APIView):
    """
    Import the Cursor chat transcript into a session so the agent can use it as context.

    - Stores the full (redacted) transcript in a SystemDocument
    - Stores a short (redacted) summary + doc reference in AgentSession.memory
    """
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def post(self, request, session_id):
        session = _get_session_for_request(request, session_id)
        if not session:
            return Response({'error': 'Session not found'}, status=status.HTTP_404_NOT_FOUND)

        transcript_path = request.data.get('transcript_path')
        doc_slug = (request.data.get('doc_slug') or f'cursor-chat-session-{session.id}').strip()
        doc_title = (request.data.get('doc_title') or f'Cursor Chat Transcript (session {session.id})').strip()

        # Conservative caps so we don't blow up DB rows or LLM context.
        max_transcript_chars = _safe_int(request.data.get('max_transcript_chars', 200_000), 200_000, 10_000, 2_000_000)
        max_summary_chars = _safe_int(request.data.get('max_summary_chars', 3500), 3500, 500, 20_000)

        try:
            imported = import_cursor_chat_transcript(
                transcript_path=transcript_path,
                project_name=getattr(settings, 'BASE_DIR', None).name if getattr(settings, 'BASE_DIR', None) else None,
                max_transcript_chars=max_transcript_chars,
                max_summary_chars=max_summary_chars,
            )
        except Exception as exc:
            return Response({'success': False, 'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        doc, created = SystemDocument.objects.update_or_create(
            slug=doc_slug,
            defaults={
                'title': doc_title,
                'project': session.project,
                'content_markdown': imported.redacted_text,
                'metadata': {
                    'source': 'cursor-transcript',
                    'transcript_path': imported.transcript_path,
                    'redaction_counts': imported.redaction_counts,
                },
                'is_active': True,
            },
        )

        mem = session.memory if isinstance(session.memory, dict) else {}
        mem = dict(mem)
        mem['cursor_chat'] = {
            'imported_at': timezone.now().isoformat(),
            'transcript_path': imported.transcript_path,
            'system_document_id': doc.id,
            'system_document_slug': doc.slug,
            'summary': imported.summary,
            'redaction_counts': imported.redaction_counts,
        }
        session.memory = mem
        session.save(update_fields=['memory', 'updated_at'])

        # Also store the summary as a system message so it always appears in timeline.
        AgentMessage.objects.create(
            session=session,
            role=AgentMessage.ROLE_SYSTEM,
            content=f'Imported Cursor chat context:\n{imported.summary}',
            metadata={
                'source': 'cursor-transcript',
                'system_document_id': doc.id,
                'system_document_slug': doc.slug,
            },
            created_by=_effective_user(request),
        )

        return Response({
            'success': True,
            'doc': {
                'id': doc.id,
                'slug': doc.slug,
                'title': doc.title,
                'created': created,
                'content_length': len(doc.content_markdown or ''),
            },
            'memory_keys_updated': ['cursor_chat'],
            'summary_length': imported.summary_char_count,
            'redaction_counts': imported.redaction_counts,
        })


class SystemDocumentListCreateView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request):
        project_id = request.query_params.get('project_id')
        corpus_id = request.query_params.get('corpus_id')
        qs = SystemDocument.objects.order_by('-updated_at', '-id')
        if project_id:
            try:
                qs = qs.filter(project_id=int(project_id))
            except Exception:
                return Response({'error': 'project_id must be an integer'}, status=status.HTTP_400_BAD_REQUEST)
        if corpus_id:
            try:
                qs = qs.filter(corpus_id=int(corpus_id))
            except Exception:
                return Response({'error': 'corpus_id must be an integer'}, status=status.HTTP_400_BAD_REQUEST)

        docs = qs[:200]
        data = [
            {
                'id': d.id,
                'slug': d.slug,
                'title': d.title,
                'project_id': d.project_id,
                'corpus_id': d.corpus_id,
                'is_active': d.is_active,
                'pin_to_context': d.pin_to_context,
                'context_order': d.context_order,
                'metadata': d.metadata or {},
                'updated_at': d.updated_at,
            }
            for d in docs
        ]
        return Response(data)

    def post(self, request):
        slug = (request.data.get('slug') or '').strip()
        if not slug:
            return Response({'error': 'slug is required'}, status=status.HTTP_400_BAD_REQUEST)

        title = (request.data.get('title') or '').strip()
        content_markdown = request.data.get('content_markdown') or ''
        if content_markdown is None:
            content_markdown = ''
        metadata = request.data.get('metadata') or {}
        if not isinstance(metadata, dict):
            return Response({'error': 'metadata must be an object/dict'}, status=status.HTTP_400_BAD_REQUEST)

        pin_to_context = bool(request.data.get('pin_to_context', False))
        context_order = _safe_int(request.data.get('context_order', 0), 0, -1000000, 1000000)

        project = None
        project_id = request.data.get('project_id')
        if project_id:
            project = _get_project_for_request(request, int(project_id))
            if not project:
                return Response({'error': 'Invalid project_id'}, status=status.HTTP_400_BAD_REQUEST)

        corpus = None
        corpus_id = request.data.get('corpus_id')
        if corpus_id:
            from .models import KnowledgeCorpus
            corpus = KnowledgeCorpus.objects.filter(id=int(corpus_id)).first()
            if not corpus:
                return Response({'error': 'Invalid corpus_id'}, status=status.HTTP_400_BAD_REQUEST)

        doc, created = SystemDocument.objects.get_or_create(
            slug=slug,
            defaults={
                'title': title,
                'content_markdown': content_markdown,
                'metadata': metadata,
                'project': project,
                'corpus': corpus,
                'pin_to_context': pin_to_context,
                'context_order': context_order,
                'created_by': _effective_user(request),
            },
        )
        if not created:
            doc.title = title or doc.title
            doc.content_markdown = content_markdown
            doc.metadata = metadata
            if project_id:
                doc.project = project
            if corpus_id:
                doc.corpus = corpus
            doc.pin_to_context = pin_to_context
            doc.context_order = context_order
            doc.save(update_fields=['title', 'content_markdown', 'metadata', 'project', 'corpus', 'pin_to_context', 'context_order', 'updated_at'])

        return Response(
            {
                'id': doc.id,
                'slug': doc.slug,
                'title': doc.title,
                'is_active': doc.is_active,
                'corpus_id': doc.corpus_id,
                'pin_to_context': doc.pin_to_context,
                'context_order': doc.context_order,
                'metadata': doc.metadata or {},
                'content_markdown': doc.content_markdown or '',
                'updated_at': doc.updated_at,
                'created': created,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SystemDocumentDetailView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request, doc_id: int):
        doc = SystemDocument.objects.filter(id=doc_id).first()
        if not doc:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            'id': doc.id,
            'slug': doc.slug,
            'title': doc.title,
            'project_id': doc.project_id,
            'corpus_id': doc.corpus_id,
            'is_active': doc.is_active,
            'pin_to_context': doc.pin_to_context,
            'context_order': doc.context_order,
            'metadata': doc.metadata or {},
            'content_markdown': doc.content_markdown or '',
            'created_at': doc.created_at,
            'updated_at': doc.updated_at,
        })

    def post(self, request, doc_id: int):
        doc = SystemDocument.objects.filter(id=doc_id).first()
        if not doc:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        if 'slug' in request.data:
            doc.slug = (request.data.get('slug') or '').strip()
        if 'title' in request.data:
            doc.title = (request.data.get('title') or '').strip()
        if 'content_markdown' in request.data:
            doc.content_markdown = request.data.get('content_markdown') or ''
        if 'metadata' in request.data:
            metadata = request.data.get('metadata') or {}
            if not isinstance(metadata, dict):
                return Response({'error': 'metadata must be an object/dict'}, status=status.HTTP_400_BAD_REQUEST)
            doc.metadata = metadata
        if 'is_active' in request.data:
            doc.is_active = bool(request.data.get('is_active'))
        if 'pin_to_context' in request.data:
            doc.pin_to_context = bool(request.data.get('pin_to_context'))
        if 'context_order' in request.data:
            doc.context_order = _safe_int(request.data.get('context_order', 0), 0, -1000000, 1000000)
        if 'corpus_id' in request.data:
            corpus_id = request.data.get('corpus_id')
            if corpus_id in (None, '', 0, '0'):
                doc.corpus = None
            else:
                from .models import KnowledgeCorpus
                corpus = KnowledgeCorpus.objects.filter(id=int(corpus_id)).first()
                if not corpus:
                    return Response({'error': 'Invalid corpus_id'}, status=status.HTTP_400_BAD_REQUEST)
                doc.corpus = corpus

        doc.save()
        return Response({
            'id': doc.id,
            'slug': doc.slug,
            'title': doc.title,
            'project_id': doc.project_id,
            'corpus_id': doc.corpus_id,
            'is_active': doc.is_active,
            'pin_to_context': doc.pin_to_context,
            'context_order': doc.context_order,
            'metadata': doc.metadata or {},
            'content_markdown': doc.content_markdown or '',
            'updated_at': doc.updated_at,
        })


class SystemDocumentGenerateView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def post(self, request):
        slug = (request.data.get('slug') or 'klikk-system').strip()
        title = (request.data.get('title') or 'Klikk Financials v4 - System Document').strip()
        project_id = request.data.get('project_id')

        include_django = bool(request.data.get('include_django', True))
        include_tm1 = bool(request.data.get('include_tm1', True))
        cube_limit = _safe_int(request.data.get('cube_limit', 30), 30, 1, 200)
        dim_limit_per_cube = _safe_int(request.data.get('dim_limit_per_cube', 50), 50, 1, 200)
        url_limit = _safe_int(request.data.get('url_limit', 200), 200, 10, 2000)
        model_limit = _safe_int(request.data.get('model_limit', 250), 250, 10, 2000)

        options = BuildOptions(
            include_django=include_django,
            include_tm1=include_tm1,
            cube_limit=cube_limit,
            dim_limit_per_cube=dim_limit_per_cube,
            url_limit=url_limit,
            model_limit=model_limit,
        )

        markdown, meta = build_system_document_markdown(title=title, options=options)

        project = None
        if project_id:
            project = _get_project_for_request(request, int(project_id))
            if not project:
                return Response({'error': 'Invalid project_id'}, status=status.HTTP_400_BAD_REQUEST)

        doc, created = SystemDocument.objects.get_or_create(
            slug=slug,
            defaults={
                'title': title,
                'content_markdown': markdown,
                'metadata': meta,
                'project': project,
                'created_by': _effective_user(request),
                'is_active': True,
            },
        )
        if not created:
            doc.title = title
            doc.content_markdown = markdown
            doc.metadata = meta
            if project_id:
                doc.project = project
            doc.save(update_fields=['title', 'content_markdown', 'metadata', 'project', 'updated_at'])

        return Response({
            'success': True,
            'created': created,
            'doc': {
                'id': doc.id,
                'slug': doc.slug,
                'title': doc.title,
                'project_id': doc.project_id,
                'is_active': doc.is_active,
                'updated_at': doc.updated_at,
                'content_length': len(doc.content_markdown or ''),
            },
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class AgentProjectListCreateView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request):
        projects = _projects_qs(request).order_by('-updated_at', '-id')[:200]
        return Response([
            {
                'id': p.id,
                'slug': p.slug,
                'name': p.name,
                'description': p.description,
                'is_active': p.is_active,
                'updated_at': p.updated_at,
            }
            for p in projects
        ])

    def post(self, request):
        name = (request.data.get('name') or '').strip()
        if not name:
            return Response({'error': 'name is required'}, status=status.HTTP_400_BAD_REQUEST)

        slug = (request.data.get('slug') or '').strip()
        if not slug:
            slug = slugify(name)[:120]
        if not slug:
            return Response({'error': 'Could not derive slug from name'}, status=status.HTTP_400_BAD_REQUEST)

        description = request.data.get('description') or ''
        if description is None:
            description = ''

        project, created = AgentProject.objects.get_or_create(
            slug=slug,
            defaults={
                'name': name,
                'description': description,
                'created_by': _effective_user(request),
                'is_active': True,
            },
        )
        if not created:
            # If slug exists, just return it.
            pass

        return Response({
            'id': project.id,
            'slug': project.slug,
            'name': project.name,
            'description': project.description,
            'is_active': project.is_active,
            'created': created,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class AgentProjectDetailView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request, project_id: int):
        project = _get_project_for_request(request, project_id)
        if not project:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            'id': project.id,
            'slug': project.slug,
            'name': project.name,
            'description': project.description,
            'memory': project.memory or {},
            'default_corpus_id': project.default_corpus_id,
            'is_active': project.is_active,
            'updated_at': project.updated_at,
        })

    def post(self, request, project_id: int):
        project = _get_project_for_request(request, project_id)
        if not project:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        if 'name' in request.data:
            project.name = (request.data.get('name') or '').strip()
        if 'slug' in request.data:
            project.slug = (request.data.get('slug') or '').strip()
        if 'description' in request.data:
            project.description = request.data.get('description') or ''
        if 'is_active' in request.data:
            project.is_active = bool(request.data.get('is_active'))
        if 'memory' in request.data:
            mem = request.data.get('memory') or {}
            if not isinstance(mem, dict):
                return Response({'error': 'memory must be an object/dict'}, status=status.HTTP_400_BAD_REQUEST)
            project.memory = mem
        if 'default_corpus_id' in request.data:
            corpus_id = request.data.get('default_corpus_id')
            if corpus_id in (None, '', 0, '0'):
                project.default_corpus = None
            else:
                from .models import KnowledgeCorpus
                corpus = KnowledgeCorpus.objects.filter(id=int(corpus_id)).first()
                if not corpus:
                    return Response({'error': 'Invalid default_corpus_id'}, status=status.HTTP_400_BAD_REQUEST)
                project.default_corpus = corpus

        project.save()
        return Response({
            'id': project.id,
            'slug': project.slug,
            'name': project.name,
            'description': project.description,
            'memory': project.memory or {},
            'default_corpus_id': project.default_corpus_id,
            'is_active': project.is_active,
            'updated_at': project.updated_at,
        })


class AgentProjectImportTM1DocsView(APIView):
    """
    PAW/TM1-only: Import TM1 metadata into project docs.
    Creates:
      - <slug_base>-tm1-summary (pinned)
      - <slug_base>-tm1-full (not pinned by default)
    """
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def post(self, request, project_id: int):
        project = _get_project_for_request(request, project_id)
        if not project:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        slug_base = (request.data.get('slug_base') or project.slug or 'project').strip()
        summary_slug = (request.data.get('summary_slug') or f'{slug_base}-tm1-summary').strip()
        full_slug = (request.data.get('full_slug') or f'{slug_base}-tm1-full').strip()

        pin_summary = bool(request.data.get('pin_summary', True))
        include_elements = bool(request.data.get('include_elements', False))
        include_process_code = bool(request.data.get('include_process_code', False))
        include_cube_rules = bool(request.data.get('include_cube_rules', False))
        split_docs = bool(request.data.get('split_docs', False))

        options = TM1DocsOptions(
            top_cubes=_safe_int(request.data.get('top_cubes', 200), 200, 1, 500),
            top_dimensions=_safe_int(request.data.get('top_dimensions', 200), 200, 1, 500),
            top_processes=_safe_int(request.data.get('top_processes', 200), 200, 1, 500),
            elements_per_hierarchy=_safe_int(request.data.get('elements_per_hierarchy', 50), 50, 1, 500),
            include_elements=include_elements,
            include_process_code=include_process_code,
            include_cube_rules=include_cube_rules,
            max_chars_full=_safe_int(request.data.get('max_chars_full', 400_000), 400_000, 10_000, 2_000_000),
        )

        if split_docs:
            meta, summary_md, full_md, split = build_tm1_docs_bundle(options=options)
        else:
            meta, summary_md, full_md = build_tm1_docs(options=options)
            split = {}

        summary_doc, _ = SystemDocument.objects.update_or_create(
            slug=summary_slug,
            defaults={
                'project': project,
                'corpus': project.default_corpus,
                'title': (request.data.get('summary_title') or 'TM1 / PAW documentation (summary)').strip(),
                'content_markdown': summary_md,
                'metadata': {**(meta or {}), 'kind': 'tm1-summary'},
                'pin_to_context': pin_summary,
                'context_order': _safe_int(request.data.get('summary_context_order', -50), -50, -1000000, 1000000),
                'is_active': True,
                'created_by': _effective_user(request),
            },
        )

        full_doc, _ = SystemDocument.objects.update_or_create(
            slug=full_slug,
            defaults={
                'project': project,
                'corpus': project.default_corpus,
                'title': (request.data.get('full_title') or 'TM1 / PAW documentation (full)').strip(),
                'content_markdown': full_md,
                'metadata': {**(meta or {}), 'kind': 'tm1-full'},
                'pin_to_context': bool(request.data.get('pin_full', False)),
                'context_order': _safe_int(request.data.get('full_context_order', 0), 0, -1000000, 1000000),
                'is_active': True,
                'created_by': _effective_user(request),
            },
        )

        split_docs_written = 0
        if split_docs and isinstance(split, dict) and split:
            import hashlib

            def _make_slug(kind: str, name: str) -> str:
                base = slugify(name or '') or 'item'
                short = base[:60].strip('-') or 'item'
                suffix = hashlib.sha1((name or '').encode('utf-8', errors='ignore')).hexdigest()[:6]
                raw = f'{slug_base}-tm1-{kind}-{short}-{suffix}'
                return raw[:120].strip('-')

            for doc_key, md in split.items():
                try:
                    k, name = (doc_key or '').split(':', 1)
                except Exception:
                    continue
                kind = {'cube': 'cube', 'dim': 'dimension', 'proc': 'process'}.get(k, k or 'item')
                slug = _make_slug(kind, name)
                title = f'TM1 {kind}: {name}'
                SystemDocument.objects.update_or_create(
                    slug=slug,
                    defaults={
                        'project': project,
                        'corpus': project.default_corpus,
                        'title': title[:255],
                        'content_markdown': md or '',
                        'metadata': {
                            'kind': f'tm1-{kind}',
                            'tm1_name': name,
                            'tm1_key': doc_key,
                            **({'base_url': meta.get('base_url'), 'generated_at': meta.get('generated_at')} if isinstance(meta, dict) else {}),
                        },
                        'pin_to_context': False,
                        'context_order': 100,
                        'is_active': True,
                        'created_by': _effective_user(request),
                    },
                )
                split_docs_written += 1

        # Save a tiny marker into project memory for traceability.
        pmem = project.memory if isinstance(project.memory, dict) else {}
        pmem = dict(pmem)
        pmem['tm1_docs'] = {
            'imported_at': timezone.now().isoformat(),
            'summary_slug': summary_doc.slug,
            'full_slug': full_doc.slug,
            'base_url': meta.get('base_url') if isinstance(meta, dict) else '',
            'split_docs': bool(split_docs),
        }
        project.memory = pmem
        project.save(update_fields=['memory', 'updated_at'])

        return Response({
            'success': True,
            'project_id': project.id,
            'docs': {
                'summary': {
                    'id': summary_doc.id,
                    'slug': summary_doc.slug,
                    'pin_to_context': summary_doc.pin_to_context,
                    'content_length': len(summary_doc.content_markdown or ''),
                },
                'full': {
                    'id': full_doc.id,
                    'slug': full_doc.slug,
                    'pin_to_context': full_doc.pin_to_context,
                    'content_length': len(full_doc.content_markdown or ''),
                },
            },
            'split_docs_written': split_docs_written,
            'errors_count': len((meta or {}).get('errors', [])) if isinstance(meta, dict) else 0,
        })


class KnowledgeCorpusListCreateView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request):
        from .models import KnowledgeCorpus
        corpora = KnowledgeCorpus.objects.order_by('-updated_at', '-id')[:200]
        return Response([
            {
                'id': c.id,
                'slug': c.slug,
                'name': c.name,
                'description': c.description,
                'is_active': c.is_active,
                'updated_at': c.updated_at,
            }
            for c in corpora
        ])

    def post(self, request):
        from .models import KnowledgeCorpus
        slug = (request.data.get('slug') or '').strip()
        name = (request.data.get('name') or '').strip()
        if not slug or not name:
            return Response({'error': 'slug and name are required'}, status=status.HTTP_400_BAD_REQUEST)
        desc = request.data.get('description') or ''
        corpus, created = KnowledgeCorpus.objects.get_or_create(
            slug=slug,
            defaults={
                'name': name,
                'description': desc,
                'created_by': _effective_user(request),
                'is_active': True,
            },
        )
        if not created:
            corpus.name = name
            corpus.description = desc
            corpus.save(update_fields=['name', 'description', 'updated_at'])
        return Response({
            'id': corpus.id,
            'slug': corpus.slug,
            'name': corpus.name,
            'description': corpus.description,
            'is_active': corpus.is_active,
            'created': created,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class KnowledgeCorpusVectorizeView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def post(self, request, corpus_id: int):
        from .models import KnowledgeCorpus
        corpus = KnowledgeCorpus.objects.filter(id=corpus_id).first()
        if not corpus:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        project_id = request.data.get('project_id')
        project_id_int = None if project_id in (None, '') else int(project_id)

        chunk_size = _safe_int(request.data.get('chunk_size', 1200), 1200, 200, 4000)
        overlap = _safe_int(request.data.get('overlap', 150), 150, 0, 1000)
        force = bool(request.data.get('force', False))
        embedding_model = (request.data.get('embedding_model') or '').strip() or None

        try:
            result = vectorize_corpus_documents(
                corpus=corpus,
                project_id=project_id_int,
                embedding_model=embedding_model,
                chunk_size=chunk_size,
                overlap=overlap,
                force=force,
            )
            return Response({
                'success': True,
                'corpus_id': result.corpus_id,
                'embedding_model': result.embedding_model,
                'documents_seen': result.documents_seen,
                'chunks_written': result.chunks_written,
                'chunks_deleted': result.chunks_deleted,
            })
        except Exception as exc:
            return Response({'success': False, 'message': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class KnowledgeCorpusSearchView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def post(self, request, corpus_id: int):
        from .models import KnowledgeCorpus
        corpus = KnowledgeCorpus.objects.filter(id=corpus_id).first()
        if not corpus:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        query = (request.data.get('query') or '').strip()
        if not query:
            return Response({'error': 'query is required'}, status=status.HTTP_400_BAD_REQUEST)

        project_id = request.data.get('project_id')
        project_id_int = None if project_id in (None, '') else int(project_id)

        top_k = _safe_int(request.data.get('top_k', 6), 6, 1, 20)
        embedding_model = (request.data.get('embedding_model') or '').strip() or None

        try:
            hits = semantic_search_chunks(
                corpus=corpus,
                query=query,
                project_id=project_id_int,
                embedding_model=embedding_model,
                top_k=top_k,
            )
            return Response({'success': True, 'hits': hits})
        except Exception as exc:
            return Response({'success': False, 'message': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class TM1ProxyExecuteView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def post(self, request):
        method = request.data.get('method', 'GET')
        path = request.data.get('path', '')
        body = request.data.get('body')
        params = request.data.get('params')
        headers = request.data.get('headers')
        session_id = request.data.get('session_id')

        session = None
        if session_id:
            session = _get_session_for_request(request, session_id)
            if not session:
                return Response({'error': 'Invalid session_id'}, status=status.HTTP_400_BAD_REQUEST)

        execution_log = AgentToolExecutionLog.objects.create(
            session=session,
            tool_name='tm1_proxy',
            status=AgentToolExecutionLog.STATUS_PENDING,
            input_payload={
                'method': method,
                'path': path,
                'body': body,
                'params': params,
            },
            executed_by=_effective_user(request),
        )

        user_tm1, user_pw = _get_user_tm1_creds(request)

        try:
            result = tm1_request(method=method, path=path, body=body, params=params, headers=headers, tm1_user=user_tm1, tm1_password=user_pw)
            if result.get('blocked'):
                execution_log.status = AgentToolExecutionLog.STATUS_BLOCKED
            else:
                execution_log.status = (
                    AgentToolExecutionLog.STATUS_SUCCESS if result.get('success') else AgentToolExecutionLog.STATUS_ERROR
                )
            execution_log.output_payload = result
            execution_log.finished_at = timezone.now()
            execution_log.save(update_fields=['status', 'output_payload', 'finished_at'])
            if result.get('blocked'):
                http_status = status.HTTP_403_FORBIDDEN
            elif result.get('success'):
                http_status = status.HTTP_200_OK
            else:
                http_status = status.HTTP_502_BAD_GATEWAY
            return Response(result, status=http_status)
        except Exception as exc:
            execution_log.status = AgentToolExecutionLog.STATUS_ERROR
            execution_log.error_message = str(exc)
            execution_log.finished_at = timezone.now()
            execution_log.save(update_fields=['status', 'error_message', 'finished_at'])
            return Response({'success': False, 'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class AgentSessionToolExecutionsView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request, session_id):
        session = _get_session_for_request(request, session_id)
        if not session:
            return Response({'error': 'Session not found'}, status=status.HTTP_404_NOT_FOUND)

        logs = session.tool_executions.all().order_by('-started_at')[:200]
        data = [
            {
                'id': log.id,
                'tool_name': log.tool_name,
                'status': log.status,
                'input_payload': log.input_payload,
                'output_payload': log.output_payload,
                'error_message': log.error_message,
                'started_at': log.started_at,
                'finished_at': log.finished_at,
            }
            for log in logs
        ]
        return Response(data)


class AgentSessionExportToSystemDocView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def post(self, request, session_id):
        session = _get_session_for_request(request, session_id)
        if not session:
            return Response({'error': 'Session not found'}, status=status.HTTP_404_NOT_FOUND)
        if not session.project:
            # Backward-compatible: attach a project so chats can be shared/scenario-tested.
            project_id = request.data.get('project_id')
            project = None
            if project_id:
                project = _get_project_for_request(request, int(project_id))
            if not project:
                project, _ = AgentProject.objects.get_or_create(
                    slug='default',
                    defaults={
                        'name': 'Default',
                        'description': 'Auto-created default project for scenario testing.',
                        'created_by': _effective_user(request),
                        'is_active': True,
                    },
                )
            session.project = project
            session.save(update_fields=['project', 'updated_at'])

        doc_slug = (request.data.get('doc_slug') or f'chat-{session.id}').strip()
        doc_title = (request.data.get('doc_title') or f'Chat transcript: {session.title or session.id}').strip()
        pin_to_context = bool(request.data.get('pin_to_context', True))
        include_tool_executions = bool(request.data.get('include_tool_executions', True))
        max_messages = _safe_int(request.data.get('max_messages', 500), 500, 10, 2000)

        markdown = build_session_transcript_markdown(
            session=session,
            include_tool_executions=include_tool_executions,
            max_messages=max_messages,
        )

        doc, created = SystemDocument.objects.update_or_create(
            slug=doc_slug,
            defaults={
                'title': doc_title,
                'project': session.project,
                'corpus': session.project.default_corpus if session.project else None,
                'content_markdown': markdown,
                'metadata': {
                    'source': 'session-export',
                    'session_id': session.id,
                    'include_tool_executions': include_tool_executions,
                },
                'pin_to_context': pin_to_context,
                'is_active': True,
            },
        )

        return Response({
            'success': True,
            'created': created,
            'doc': {
                'id': doc.id,
                'slug': doc.slug,
                'title': doc.title,
                'project_id': doc.project_id,
                'pin_to_context': doc.pin_to_context,
                'updated_at': doc.updated_at,
                'content_length': len(doc.content_markdown or ''),
            },
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class AgentSessionRunView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def post(self, request, session_id):
        session = _get_session_for_request(request, session_id)
        if not session:
            return Response({'error': 'Session not found'}, status=status.HTTP_404_NOT_FOUND)

        user_message = (request.data.get('message') or '').strip()
        if user_message:
            AgentMessage.objects.create(
                session=session,
                role=AgentMessage.ROLE_USER,
                content=user_message,
                metadata=request.data.get('metadata') or {},
                created_by=_effective_user(request),
            )

        execution_log = AgentToolExecutionLog.objects.create(
            session=session,
            tool_name='ai_chat_runner',
            status=AgentToolExecutionLog.STATUS_PENDING,
            input_payload={'has_new_user_message': bool(user_message)},
            executed_by=_effective_user(request),
        )

        try:
            context_messages, context_message_count, context_limit = build_context_messages(session)
            llm_result = generate_assistant_reply(session=session, context_messages=context_messages)

            assistant_message = AgentMessage.objects.create(
                session=session,
                role=AgentMessage.ROLE_ASSISTANT,
                content=llm_result['content'],
                metadata={
                    'provider': llm_result.get('provider'),
                    'model': llm_result.get('model'),
                    'context_message_count': context_message_count,
                    'context_limit': context_limit,
                },
                created_by=_effective_user(request),
            )

            session.updated_at = timezone.now()
            session.save(update_fields=['updated_at'])

            result = {
                'success': True,
                'session_id': session.id,
                'assistant_message': {
                    'id': assistant_message.id,
                    'role': assistant_message.role,
                    'content': assistant_message.content,
                    'metadata': assistant_message.metadata,
                    'created_at': assistant_message.created_at.isoformat(),
                },
                'context': {
                    'messages_used': context_message_count,
                    'message_limit': context_limit,
                },
            }

            execution_log.status = AgentToolExecutionLog.STATUS_SUCCESS
            execution_log.output_payload = result
            execution_log.finished_at = timezone.now()
            execution_log.save(update_fields=['status', 'output_payload', 'finished_at'])
            return Response(result)
        except Exception as exc:
            execution_log.status = AgentToolExecutionLog.STATUS_ERROR
            execution_log.error_message = str(exc)
            execution_log.finished_at = timezone.now()
            execution_log.save(update_fields=['status', 'error_message', 'finished_at'])
            return Response(
                {'success': False, 'message': str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class AgentSessionRunWithToolsView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def post(self, request, session_id):
        session = _get_session_for_request(request, session_id)
        if not session:
            return Response({'error': 'Session not found'}, status=status.HTTP_404_NOT_FOUND)

        user_message = (request.data.get('message') or '').strip()
        if not user_message:
            return Response({'error': 'message is required'}, status=status.HTTP_400_BAD_REQUEST)

        AgentMessage.objects.create(
            session=session,
            role=AgentMessage.ROLE_USER,
            content=user_message,
            metadata=request.data.get('metadata') or {},
            created_by=_effective_user(request),
        )

        run_log = AgentToolExecutionLog.objects.create(
            session=session,
            tool_name='ai_chat_runner_with_tools',
            status=AgentToolExecutionLog.STATUS_PENDING,
            input_payload={'message': user_message},
            executed_by=_effective_user(request),
        )

        tool_results = []
        try:
            context_messages, context_message_count, context_limit = build_context_messages(session)

            # Inject RAG: when user asks about vectorized model / documentation, search corpus and add excerpts.
            if getattr(session, 'project', None) and getattr(session.project, 'default_corpus_id', None) and user_wants_vectorized_knowledge(user_message):
                try:
                    from .models import KnowledgeCorpus
                    corpus = KnowledgeCorpus.objects.filter(id=session.project.default_corpus_id).first()
                    if corpus:
                        hits = semantic_search_chunks(
                            corpus=corpus,
                            query=user_message,
                            project_id=session.project_id,
                            top_k=10,
                        )
                        if hits:
                            rag_lines = [
                                'Relevant excerpts from vectorized documentation (use these to support your answer):',
                                'Areas: IBM Cognos Planning Analytics (TI, rules, MDX, API); Accounting/Tax/Booking South Africa; Financial Modeling & BI (CMA).',
                                ''
                            ]
                            for h in hits:
                                rag_lines.append(f"[{h.get('doc_title') or h.get('doc_slug')} (similarity={h.get('similarity', 0):.2f})]")
                                rag_lines.append((h.get('chunk_text') or '')[:1500])
                                rag_lines.append('')
                            context_messages = list(context_messages)
                            context_messages.append({
                                'role': 'system',
                                'content': '\n'.join(rag_lines).strip(),
                            })
                except Exception:
                    pass

            llm_result = generate_assistant_reply_with_tool_use(
                session=session,
                context_messages=context_messages,
                user_message=user_message,
            )

            assistant_message = AgentMessage.objects.create(
                session=session,
                role=AgentMessage.ROLE_ASSISTANT,
                content=llm_result['content'],
                metadata={
                    'provider': llm_result.get('provider'),
                    'model': llm_result.get('model'),
                    'context_message_count': context_message_count,
                    'context_limit': context_limit,
                    'tool_calls_count': len(tool_results),
                },
                created_by=_effective_user(request),
            )

            session.updated_at = timezone.now()
            session.save(update_fields=['updated_at'])

            result = {
                'success': True,
                'session_id': session.id,
                'assistant_message': {
                    'id': assistant_message.id,
                    'role': assistant_message.role,
                    'content': assistant_message.content,
                    'metadata': assistant_message.metadata,
                    'created_at': assistant_message.created_at.isoformat(),
                },
                'tool_trace': tool_results,
                'context': {
                    'messages_used': context_message_count,
                    'message_limit': context_limit,
                },
            }

            run_log.status = AgentToolExecutionLog.STATUS_SUCCESS
            run_log.output_payload = result
            run_log.finished_at = timezone.now()
            run_log.save(update_fields=['status', 'output_payload', 'finished_at'])
            return Response(result)
        except Exception as exc:
            run_log.status = AgentToolExecutionLog.STATUS_ERROR
            run_log.error_message = str(exc)
            run_log.finished_at = timezone.now()
            run_log.save(update_fields=['status', 'error_message', 'finished_at'])
            return Response({'success': False, 'message': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class TM1ProxyTestConnectionView(APIView):
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def post(self, request):
        result = tm1_test_connection()
        return Response(result, status=status.HTTP_200_OK if result.get('success') else status.HTTP_502_BAD_GATEWAY)


class TM1VersionView(APIView):
    """Return TM1 server version info from the configured TM1 instance."""
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request):
        result = tm1_get_version()
        return Response(
            result,
            status=status.HTTP_200_OK if result.get('success') else status.HTTP_502_BAD_GATEWAY,
        )


class TM1ConfigView(APIView):
    """
    Read/update the shared TM1 server config used by both:
    - planning_analytics pipeline
    - ai_agent TM1 proxy
    """
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request):
        cfg = TM1ServerConfig.get_active()
        if not cfg:
            return Response({'base_url': '', 'username': '', 'password': ''})
        return Response({
            'id': cfg.id,
            'base_url': cfg.base_url,
            'username': cfg.username,
            'password': '********' if cfg.password else '',
        })

    def post(self, request):
        base_url = (request.data.get('base_url', '') or '').strip()
        username = (request.data.get('username', '') or '').strip()
        password = request.data.get('password', '')

        cfg = TM1ServerConfig.get_active()
        if cfg:
            cfg.base_url = base_url
            cfg.username = username
            if password and password != '********':
                cfg.password = password
            cfg.save()
        else:
            cfg = TM1ServerConfig.objects.create(
                base_url=base_url,
                username=username,
                password=password if password != '********' else '',
                is_active=True,
            )

        return Response({
            'id': cfg.id,
            'base_url': cfg.base_url,
            'username': cfg.username,
            'message': 'Shared TM1 config saved.',
        })


# ---------------------------------------------------------------------------
# MCP Skills Engine API Views
# ---------------------------------------------------------------------------

class SkillRegistryListView(APIView):
    """List all skills in the registry with tool counts."""
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request):
        skills = SkillRegistry.objects.all()
        # Get tool counts from the loaded registry
        try:
            from .agent.tool_registry import TOOL_TO_SKILL
            skill_tool_counts = {}
            for tool_name, skill_name in TOOL_TO_SKILL.items():
                skill_tool_counts[skill_name] = skill_tool_counts.get(skill_name, 0) + 1
        except Exception:
            skill_tool_counts = {}

        return Response([
            {
                'module_name': s.module_name,
                'import_path': s.import_path,
                'display_name': s.display_name,
                'description': s.description,
                'keywords': s.keywords or [],
                'always_on': s.always_on,
                'enabled': s.enabled,
                'sort_order': s.sort_order,
                'tool_count': skill_tool_counts.get(s.module_name, 0),
                'updated_at': s.updated_at,
            }
            for s in skills
        ])


class SkillRegistryDetailView(APIView):
    """Update a skill registry entry (enabled, keywords, etc.)."""
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request, module_name):
        skill = SkillRegistry.objects.filter(module_name=module_name).first()
        if not skill:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        # Get tool list from the loaded module
        tools = []
        try:
            from .agent.tool_registry import TOOL_TO_SKILL, ANTHROPIC_SCHEMAS
            tools = [
                {'name': s['name'], 'description': s.get('description', '')}
                for s in ANTHROPIC_SCHEMAS
                if TOOL_TO_SKILL.get(s['name']) == module_name
            ]
        except Exception:
            pass

        return Response({
            'module_name': skill.module_name,
            'import_path': skill.import_path,
            'display_name': skill.display_name,
            'description': skill.description,
            'keywords': skill.keywords or [],
            'always_on': skill.always_on,
            'enabled': skill.enabled,
            'sort_order': skill.sort_order,
            'tools': tools,
            'updated_at': skill.updated_at,
        })

    def put(self, request, module_name):
        skill = SkillRegistry.objects.filter(module_name=module_name).first()
        if not skill:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        if 'enabled' in data:
            skill.enabled = bool(data['enabled'])
        if 'keywords' in data:
            skill.keywords = data['keywords'] if isinstance(data['keywords'], list) else []
        if 'display_name' in data:
            skill.display_name = (data['display_name'] or '').strip()
        if 'description' in data:
            skill.description = (data['description'] or '').strip()
        if 'always_on' in data:
            skill.always_on = bool(data['always_on'])
        if 'sort_order' in data:
            skill.sort_order = int(data['sort_order'])

        skill.save()
        return Response({
            'module_name': skill.module_name,
            'enabled': skill.enabled,
            'keywords': skill.keywords,
            'always_on': skill.always_on,
            'updated_at': skill.updated_at,
        })


class CredentialListView(APIView):
    """List all credentials (values masked) or create/update one."""
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def get(self, request):
        creds = Credential.objects.all()
        return Response([
            {
                'key': c.key,
                'label': c.label or c.key,
                'hint': c.masked_value,
                'has_value': bool(c.value),
                'updated_at': c.updated_at,
            }
            for c in creds
        ])


class CredentialDetailView(APIView):
    """Set or delete a credential."""
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def put(self, request, key):
        value = request.data.get('value', '')
        label = request.data.get('label', '')
        cred, created = Credential.objects.update_or_create(
            key=key,
            defaults={'value': value, 'label': label} if label else {'value': value},
        )
        # Invalidate config cache
        try:
            from .agent.config import invalidate_credential_cache
            invalidate_credential_cache(key)
        except Exception:
            pass
        return Response({
            'key': cred.key,
            'label': cred.label,
            'has_value': bool(cred.value),
            'updated_at': cred.updated_at,
        })

    def delete(self, request, key):
        deleted, _ = Credential.objects.filter(key=key).delete()
        try:
            from .agent.config import invalidate_credential_cache
            invalidate_credential_cache(key)
        except Exception:
            pass
        return Response({'deleted': deleted > 0})


class MCPAgentChatView(APIView):
    """Run a message through the MCP skills agent (synchronous HTTP, not WebSocket)."""
    permission_classes = AI_AGENT_PERMISSION_CLASSES

    def post(self, request):
        user_message = (request.data.get('message') or '').strip()
        if not user_message:
            return Response({'error': 'message is required'}, status=status.HTTP_400_BAD_REQUEST)

        history = request.data.get('history', [])
        if not isinstance(history, list):
            history = []

        try:
            from .agent.core import run_agent
            result = run_agent(user_message, history)
            if isinstance(result, tuple):
                text, tool_calls, skills_routed = result
                return Response({
                    'response': text,
                    'tool_calls': [
                        {
                            'name': getattr(tc, 'name', str(tc)),
                            'skill': getattr(tc, 'skill', ''),
                            'duration_ms': getattr(tc, 'duration_ms', 0),
                        }
                        for tc in tool_calls
                    ] if tool_calls else [],
                    'skills_routed': skills_routed,
                })
            return Response({'response': str(result), 'tool_calls': [], 'skills_routed': []})
        except Exception as exc:
            return Response(
                {'error': f'{type(exc).__name__}: {exc}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class AgentRunningStatusView(APIView):
    """Polled by the frontend to get real-time agent status (what tool it's running, etc.)."""
    permission_classes = [AllowAny]

    def get(self, request):
        key = request.query_params.get('key', 'default')
        from .consumers import get_agent_status
        entry = get_agent_status(key)
        if entry is None:
            return Response({"active": False})
        return Response({
            "active": True,
            "status": entry.get("status", ""),
            "tool_calls": entry.get("tool_calls", []),
            "updated_at": entry.get("updated_at"),
        })


class WebSocketBroadcastView(APIView):
    """Accept a message payload from FastAPI and broadcast to WebSocket observers.

    Also persists each message to Django AgentSession / AgentMessage so the
    monitor dashboard and session history stay in sync with FastAPI chats.

    POST /api/ai-agent/ws/broadcast/
    Body: { "session_id": "...", "role": "user"|"assistant", "content": "...",
            "username": "...", "tool_calls": [...], "skills_routed": [...], ... }
    """
    permission_classes = [AllowAny]  # Internal service call; restrict via network/firewall

    def post(self, request):
        from .consumers import broadcast_message

        payload = request.data
        if not isinstance(payload, dict):
            return Response({'error': 'payload must be a JSON object'}, status=status.HTTP_400_BAD_REQUEST)

        # Ensure a type field exists
        if 'type' not in payload:
            payload['type'] = 'message'

        # --- Persist to Django AgentSession / AgentMessage ---
        session_id = payload.get('session_id', '')
        role = payload.get('role', '')
        content = payload.get('content', '')

        if session_id and role in ('user', 'assistant') and content:
            try:
                self._persist_message(session_id, role, content, payload)
            except Exception:
                import logging
                logging.getLogger('ai_agent').warning(
                    "Failed to persist broadcast message to Django",
                    exc_info=True,
                )

        broadcast_message(payload)
        return Response({'status': 'broadcast_sent'})

    @staticmethod
    def _persist_message(session_id: str, role: str, content: str, payload: dict):
        """Get-or-create an AgentSession keyed by FastAPI session_id,
        then append the message."""
        from django.contrib.auth import get_user_model
        User = get_user_model()

        # Try to resolve the user from the username sent by FastAPI
        username = payload.get('username', '')
        user = None
        if username:
            user = User.objects.filter(username=username).first()

        # Use the FastAPI session_id as a lookup key stored in session.memory
        session = AgentSession.objects.filter(
            memory__fastapi_session_id=session_id,
        ).first()

        if not session:
            # Derive a readable title from first user message
            title = content[:80] if role == 'user' else f'Chat {session_id[:8]}'
            session = AgentSession.objects.create(
                title=title,
                status=AgentSession.STATUS_OPEN,
                memory={'fastapi_session_id': session_id},
                created_by=user,
            )

        # Build metadata from extra fields
        metadata = {}
        if payload.get('tool_calls'):
            metadata['tool_calls'] = payload['tool_calls']
        if payload.get('skills_routed'):
            metadata['skills_routed'] = payload['skills_routed']

        AgentMessage.objects.create(
            session=session,
            role=role,
            content=content,
            metadata=metadata,
            created_by=user,
        )

        # Keep session.updated_at fresh
        session.save(update_fields=['updated_at'])


# ---------------------------------------------------------------------------
#  Agent Monitoring Dashboard Views
# ---------------------------------------------------------------------------

class AgentMonitorPerformanceView(APIView):
    """GET /api/ai-agent/monitor/performance/?hours=24&tool_name=..."""
    permission_classes = [IsAuthenticated] if not _security_disabled() else [AllowAny]

    def get(self, request):
        from .skills.agent_monitor import agent_tool_performance
        hours = int(request.query_params.get('hours', 24))
        tool_name = request.query_params.get('tool_name', '')
        return Response(agent_tool_performance(hours=hours, tool_name=tool_name))


class AgentMonitorSessionsView(APIView):
    """GET /api/ai-agent/monitor/sessions/?days=7"""
    permission_classes = [IsAuthenticated] if not _security_disabled() else [AllowAny]

    def get(self, request):
        from .skills.agent_monitor import agent_session_analytics
        days = int(request.query_params.get('days', 7))
        return Response(agent_session_analytics(days=days))


class AgentMonitorHealthView(APIView):
    """GET /api/ai-agent/monitor/health/"""
    permission_classes = [IsAuthenticated] if not _security_disabled() else [AllowAny]

    def get(self, request):
        from .skills.agent_monitor import agent_health_check
        return Response(agent_health_check())


class AgentMonitorErrorsView(APIView):
    """GET /api/ai-agent/monitor/errors/?hours=24&tool_name=...&limit=20"""
    permission_classes = [IsAuthenticated] if not _security_disabled() else [AllowAny]

    def get(self, request):
        from .skills.agent_monitor import agent_diagnose_errors
        hours = int(request.query_params.get('hours', 24))
        tool_name = request.query_params.get('tool_name', '')
        limit = int(request.query_params.get('limit', 20))
        return Response(agent_diagnose_errors(hours=hours, tool_name=tool_name, limit=limit))


class AgentMonitorSlowToolsView(APIView):
    """GET /api/ai-agent/monitor/slow-tools/?hours=24&threshold_ms=2000&limit=20"""
    permission_classes = [IsAuthenticated] if not _security_disabled() else [AllowAny]

    def get(self, request):
        from .skills.agent_monitor import agent_slow_tools
        hours = int(request.query_params.get('hours', 24))
        threshold_ms = int(request.query_params.get('threshold_ms', 2000))
        limit = int(request.query_params.get('limit', 20))
        return Response(agent_slow_tools(hours=hours, threshold_ms=threshold_ms, limit=limit))


class AgentMonitorLiveView(APIView):
    """GET /api/ai-agent/monitor/live/?limit=50&after_id=0
    Returns recent tool executions for a live activity feed.
    Supports polling via after_id — only returns entries newer than that ID.
    """
    permission_classes = [IsAuthenticated] if not _security_disabled() else [AllowAny]

    def get(self, request):
        limit = min(int(request.query_params.get('limit', 50)), 200)
        after_id = int(request.query_params.get('after_id', 0))

        qs = AgentToolExecutionLog.objects.order_by('-id')
        if after_id:
            qs = qs.filter(id__gt=after_id)
        qs = qs[:limit]

        entries = []
        for e in qs:
            duration = None
            if e.finished_at and e.started_at:
                duration = round((e.finished_at - e.started_at).total_seconds() * 1000)
            entries.append({
                'id': e.id,
                'tool_name': e.tool_name,
                'status': e.status,
                'input': e.input_payload,
                'output_preview': str(e.output_payload)[:300] if e.output_payload else None,
                'error': e.error_message[:300] if e.error_message else None,
                'duration_ms': duration,
                'started_at': e.started_at.isoformat() if e.started_at else None,
                'finished_at': e.finished_at.isoformat() if e.finished_at else None,
                'session_id': e.session_id,
            })

        entries.reverse()
        return Response({
            'entries': entries,
            'count': len(entries),
            'latest_id': entries[-1]['id'] if entries else after_id,
        })

