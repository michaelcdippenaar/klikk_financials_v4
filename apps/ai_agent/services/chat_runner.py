import os
import re
import json

import requests
from django.conf import settings

from apps.ai_agent.models import AgentMessage


# OpenAI function-calling tools so the model can run any TM1 query (and web search).
TM1_TOOLS_OPENAI = [
    {
        'type': 'function',
        'function': {
            'name': 'tm1_get',
            'description': (
                'Execute a TM1/Planning Analytics REST GET request. Path is the relative API path, '
                "e.g. Dimensions('account')/Elements?$filter=contains(Name,'Property') or Cubes?$select=Name "
                "or Dimensions('account')/Hierarchies('account')?$expand=Elements($select=Name;$top=500)."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                        'description': 'TM1 REST path (no leading slash), e.g. Cubes?$select=Name',
                    },
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'tm1_mdx',
            'description': 'Execute an MDX query against TM1 (ExecuteMDX). Returns cell set; use for cube data.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'Full MDX query string',
                    },
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'web_search',
            'description': 'Search the web for current information (e.g. stock price, news, real-time data).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'Search query',
                    },
                },
                'required': ['query'],
            },
        },
    },
]


DEFAULT_CONTEXT_MESSAGE_LIMIT = 30
DEFAULT_MODEL = 'gpt-5.2'
DEFAULT_MEMORY_CONTEXT_MAX_CHARS = 4000
DEFAULT_DOC_CONTEXT_MAX_CHARS = 8000
DEFAULT_DOC_CONTEXT_MAX_DOCS = 3


def _context_limit():
    value = getattr(settings, 'AI_AGENT_CONTEXT_MESSAGE_LIMIT', DEFAULT_CONTEXT_MESSAGE_LIMIT)
    try:
        value = int(value)
    except Exception:
        value = DEFAULT_CONTEXT_MESSAGE_LIMIT
    return max(5, min(value, 200))


def _memory_context_max_chars():
    value = getattr(settings, 'AI_AGENT_MEMORY_CONTEXT_MAX_CHARS', DEFAULT_MEMORY_CONTEXT_MAX_CHARS)
    try:
        value = int(value)
    except Exception:
        value = DEFAULT_MEMORY_CONTEXT_MAX_CHARS
    return max(500, min(value, 20000))


def _doc_context_max_chars():
    value = getattr(settings, 'AI_AGENT_DOC_CONTEXT_MAX_CHARS', DEFAULT_DOC_CONTEXT_MAX_CHARS)
    try:
        value = int(value)
    except Exception:
        value = DEFAULT_DOC_CONTEXT_MAX_CHARS
    return max(500, min(value, 50000))


def _doc_context_max_docs():
    value = getattr(settings, 'AI_AGENT_DOC_CONTEXT_MAX_DOCS', DEFAULT_DOC_CONTEXT_MAX_DOCS)
    try:
        value = int(value)
    except Exception:
        value = DEFAULT_DOC_CONTEXT_MAX_DOCS
    return max(0, min(value, 20))


def build_context_messages(session):
    context_limit = _context_limit()
    recent_messages = list(session.messages.all().order_by('-id')[:context_limit])
    recent_messages.reverse()

    context_messages = []
    for m in recent_messages:
        context_messages.append({
            'role': m.role if m.role in {'system', 'user', 'assistant'} else 'system',
            'content': m.content,
        })

    memory = getattr(session, 'memory', None) or {}
    if isinstance(memory, dict) and memory:
        try:
            rendered = json.dumps(memory, ensure_ascii=False, sort_keys=True, indent=2)
        except Exception:
            rendered = str(memory)
        max_chars = _memory_context_max_chars()
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars] + '\n... (truncated)'
        context_messages.insert(0, {
            'role': 'system',
            'content': 'Session memory (persisted across runs):\n' + rendered,
        })

    project = getattr(session, 'project', None)
    project_memory = getattr(project, 'memory', None) if project else None
    if isinstance(project_memory, dict) and project_memory:
        try:
            rendered = json.dumps(project_memory, ensure_ascii=False, sort_keys=True, indent=2)
        except Exception:
            rendered = str(project_memory)
        max_chars = _memory_context_max_chars()
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars] + '\n... (truncated)'
        context_messages.insert(0, {
            'role': 'system',
            'content': 'Project memory (shared across chats):\n' + rendered,
        })

    # Inject pinned project documents (shared across chats) as system context.
    if project:
        max_docs = _doc_context_max_docs()
        if max_docs > 0:
            try:
                docs_qs = project.system_documents.filter(is_active=True, pin_to_context=True).order_by('context_order', '-updated_at')
                docs = list(docs_qs[:max_docs])
            except Exception:
                docs = []

            max_chars = _doc_context_max_chars()
            for d in reversed(docs):
                text = (d.content_markdown or '').strip()
                if not text:
                    continue
                if len(text) > max_chars:
                    text = text[:max_chars] + '\n... (truncated)'
                context_messages.insert(0, {
                    'role': 'system',
                    'content': f'Project document (pinned): {d.title or d.slug} [{d.slug}]\n\n{text}',
                })

    return context_messages, len(recent_messages), context_limit


def _system_prompt(session):
    tenant_hint = ''
    if session.organisation:
        tenant_hint = (
            f" Active tenant: {session.organisation.tenant_name} "
            f"({session.organisation.tenant_id})."
        )
    return (
        'The system runs TM1/PAW API calls for the user and injects the results into this conversation. When you see "Tool execution results" below with success and response data, you MUST use that actual data in your reply: list the account names, dimension elements, cell values, etc. Do NOT say you cannot execute API calls—the system has already run them; show the user the results. If a tool failed, explain the error and suggest a fix.'
        '\n\n'
        'When "web_search" tool results are provided, use the titles, snippets, and links (or knowledgeGraph if present) to answer the user—e.g. current stock price, real-time info, or anything they asked to look up. Summarize the search results clearly; if no results or search is not configured, say so and suggest setting SERPER_API_KEY.'
        '\n\n'
        'You are Klikk AI Agent. Your tools and knowledge include three main areas—use them when relevant:'
        '\n\n'
        '1) IBM COGNOS PLANNING ANALYTICS (TM1/PA): TI (TurboIntegrator) and Rule syntax; generating MDX queries; REST API endpoints; cubes, dimensions, processes, and all other PA material. Use the built-in tm1_get / tm1_mdx (or paw get / paw mdx) tools for live API calls. Use "Relevant excerpts from vectorized documentation" when provided for TI code, rules, MDX, and API usage. Study and apply PA documentation in detail.'
        '\n\n'
        '2) ACCOUNTING, TAX AND BOOKING IN SOUTH AFRICA: South African accounting standards, VAT, income tax, booking practices, and related compliance. Use provided excerpts and project documentation when answering on SA accounting and tax.'
        '\n\n'
        '3) FINANCIAL MODELING AND BUSINESS INTELLIGENCE: Concepts at Chartered Management Accountant (CMA) level—ratios, valuation, budgeting, forecasting, dashboards, KPIs, and BI best practices. Use provided material to support answers.'
        '\n\n'
        'When the user says "vectorised" or "vectorized" in the context of documentation or knowledge, they mean our RAG/embedding system (semantic search over imported docs). Use the provided excerpts. Do not confuse with TM1 technical vectorization (batch processing).'
        '\n\n'
        'General: Be concise. When "Relevant excerpts" are provided, use them. If a tool fails, explain using status_code/message and suggest the next step. Writes to Actuals/Forecast are blocked unless security is disabled.'
        f'{tenant_hint}'
    )


def _resolve_openai_key():
    key = getattr(settings, 'AI_AGENT_OPENAI_API_KEY', None)
    if key:
        return key
    return os.environ.get('AI_AGENT_OPENAI_API_KEY')


def _resolve_model():
    return getattr(settings, 'AI_AGENT_MODEL', DEFAULT_MODEL)


def _resolve_gemini_key():
    key = getattr(settings, 'AI_AGENT_GEMINI_API_KEY', None)
    if key:
        return key
    
    val1 = os.environ.get('AI_AGENT_GEMINI_API_KEY')
    val2 = os.environ.get('GEMINI_API_KEY')
    return val1 or val2


def _resolve_gemini_model():
    return getattr(settings, 'AI_AGENT_GEMINI_MODEL', 'gemini-2.5-flash')


def _context_to_text(context_messages):
    lines = []
    for msg in context_messages:
        role = (msg.get('role') or 'user').upper()
        content = (msg.get('content') or '').strip()
        if content:
            lines.append(f'{role}: {content}')
    return '\n'.join(lines)


def generate_assistant_reply(session, context_messages):
    """
    Generate assistant text.
    If no API key is configured, returns deterministic local fallback response.
    """
    openai_key = _resolve_openai_key()
    model = _resolve_model()
    gemini_key = _resolve_gemini_key()
    gemini_model = _resolve_gemini_model()

    if not openai_key and not gemini_key:
        last_user = next((m['content'] for m in reversed(context_messages) if m['role'] == 'user'), '')
        return {
            'content': (
                'AI provider is not configured yet (set AI_AGENT_OPENAI_API_KEY or AI_AGENT_GEMINI_API_KEY). '
                f'I stored your message and context. Last user input: {last_user[:300]}'
            ),
            'provider': 'local-fallback',
            'model': 'none',
        }

    # Prefer Gemini when configured explicitly for this project.
    if gemini_key:
        prompt = _context_to_text([
            {'role': 'system', 'content': _system_prompt(session)},
            *context_messages,
        ])
        response = requests.post(
            f'https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent',
            params={'key': gemini_key},
            headers={'Content-Type': 'application/json'},
            json={
                'contents': [
                    {
                        'parts': [
                            {'text': prompt},
                        ],
                    }
                ],
                'generationConfig': {
                    'temperature': 0.2,
                },
            },
            timeout=60,
        )
        if not response.ok:
            error_text = response.text[:2000]
            raise RuntimeError(
                f'Gemini API error ({response.status_code}) for model {gemini_model}: {error_text}'
            )
        data = response.json()
        parts = (
            data.get('candidates', [{}])[0]
            .get('content', {})
            .get('parts', [])
        )
        content = ''.join(p.get('text', '') for p in parts).strip()
        return {
            'content': content or 'No content returned by model.',
            'provider': 'gemini',
            'model': gemini_model,
        }

    payload_messages = [{'role': 'system', 'content': _system_prompt(session)}]
    payload_messages.extend(context_messages)

    response = requests.post(
        'https://api.openai.com/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {openai_key}',
            'Content-Type': 'application/json',
        },
        json={
            'model': model,
            'messages': payload_messages,
            'temperature': 0.2,
        },
        timeout=60,
    )
    if not response.ok:
        error_text = response.text[:2000]
        raise RuntimeError(f'OpenAI API error ({response.status_code}): {error_text}')
    data = response.json()
    content = (
        data.get('choices', [{}])[0]
        .get('message', {})
        .get('content', '')
        .strip()
    )
    return {
        'content': content or 'No content returned by model.',
        'provider': 'openai',
        'model': model,
    }


def _execute_tool(tool_name, args):
    """Execute one tool call; return dict with success, status_code, message, response_body, blocked."""
    from apps.ai_agent.services.tm1_proxy import tm1_request
    from apps.ai_agent.services.web_search import web_search as run_web_search

    if tool_name == 'tm1_get':
        path = (args or {}).get('path') or ''
        return tm1_request(method='GET', path=path)
    if tool_name == 'tm1_mdx':
        query = (args or {}).get('query') or ''
        return tm1_request(
            method='POST',
            path='ExecuteMDX?$expand=Cells($select=Value)',
            body={'MDX': query},
        )
    if tool_name == 'web_search':
        out = run_web_search(query=(args or {}).get('query') or '')
        out.setdefault('blocked', False)
        return out
    return {
        'success': False,
        'status_code': 0,
        'message': f'Unknown tool: {tool_name}',
        'response_body': None,
        'blocked': False,
    }


def generate_assistant_reply_with_tool_use(session, context_messages, user_message):
    """
    Let the LLM decide when to call tm1_get / tm1_mdx / web_search (OpenAI function calling).
    Run up to max_rounds iterations; return final assistant content and provider/model.
    """
    openai_key = _resolve_openai_key()
    model = _resolve_model()
    max_rounds = 3

    if not openai_key:
        # No OpenAI: fall back to single reply without tools.
        fallback_messages = list(context_messages)
        fallback_messages.append({'role': 'user', 'content': user_message})
        return generate_assistant_reply(session=session, context_messages=fallback_messages)

    messages = [
        {'role': 'system', 'content': _system_prompt(session)},
        *context_messages,
    ]

    for _ in range(max_rounds):
        payload = {
            'model': model,
            'messages': messages,
            'temperature': 0.2,
            'tools': TM1_TOOLS_OPENAI,
            'tool_choice': 'auto',
        }
        response = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {openai_key}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=90,
        )
        if not response.ok:
            error_text = response.text[:2000]
            raise RuntimeError(f'OpenAI API error ({response.status_code}): {error_text}')

        data = response.json()
        choice = data.get('choices', [{}])[0]
        message = choice.get('message') or {}
        tool_calls = message.get('tool_calls') or []

        if not tool_calls:
            content = (message.get('content') or '').strip()
            return {
                'content': content or 'No content returned by model.',
                'provider': 'openai',
                'model': model,
            }

        # Append assistant message with tool_calls (OpenAI format).
        assistant_msg = {'role': 'assistant', 'content': message.get('content') or ''}
        assistant_msg['tool_calls'] = [
            {
                'id': tc.get('id'),
                'type': 'function',
                'function': {'name': tc.get('function', {}).get('name'), 'arguments': tc.get('function', {}).get('arguments') or '{}'},
            }
            for tc in tool_calls
        ]
        messages.append(assistant_msg)

        # Execute each tool and append tool results.
        for tc in tool_calls:
            tc_id = tc.get('id')
            fn = tc.get('function') or {}
            name = fn.get('name') or ''
            try:
                arguments = json.loads(fn.get('arguments') or '{}')
            except (ValueError, json.JSONDecodeError):
                arguments = {}
            result = _execute_tool(name, arguments)
            # Format for OpenAI tool message: content is string (e.g. JSON of result).
            body = result.get('response_body')
            if body is not None:
                try:
                    content_str = json.dumps(body, ensure_ascii=False)[:8000]
                except Exception:
                    content_str = str(body)[:8000]
            else:
                content_str = json.dumps({
                    'success': result.get('success'),
                    'status_code': result.get('status_code'),
                    'message': result.get('message'),
                })
            messages.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'content': content_str,
            })

    # Max rounds reached with no final text reply; synthesize one from last assistant turn.
    last_content = (messages[-1].get('content') if messages else '') or 'Tool loop ended without a final reply.'
    return {
        'content': last_content,
        'provider': 'openai',
        'model': model,
    }


def plan_tool_calls(user_message):
    """
    Lightweight intent router for MVP tool execution.
    Supports:
      - tm1 get <path>
      - tm1 mdx <query>
      - mdx: <query>
    """
    text = (user_message or '').strip()
    if not text:
        return []

    lower = text.lower()

    mdx_prefix_match = re.match(r'^\s*mdx\s*:\s*(.+)$', text, flags=re.IGNORECASE | re.DOTALL)
    if mdx_prefix_match:
        return [{'tool': 'tm1_mdx', 'args': {'query': mdx_prefix_match.group(1).strip()}}]

    tm1_mdx_match = re.match(r'^\s*tm1\s+mdx\s+(.+)$', text, flags=re.IGNORECASE | re.DOTALL)
    if tm1_mdx_match:
        return [{'tool': 'tm1_mdx', 'args': {'query': tm1_mdx_match.group(1).strip()}}]

    tm1_get_match = re.match(r'^\s*tm1\s+get\s+(.+)$', text, flags=re.IGNORECASE | re.DOTALL)
    if tm1_get_match:
        return [{'tool': 'tm1_get', 'args': {'path': tm1_get_match.group(1).strip()}}]

    # PAW / Planning Analytics Workspace shorthand maps to TM1 REST in this app.
    paw_mdx_match = re.match(r'^\s*paw\s+mdx\s+(.+)$', text, flags=re.IGNORECASE | re.DOTALL)
    if paw_mdx_match:
        return [{'tool': 'tm1_mdx', 'args': {'query': paw_mdx_match.group(1).strip()}}]

    paw_get_match = re.match(r'^\s*paw\s+get\s+(.+)$', text, flags=re.IGNORECASE | re.DOTALL)
    if paw_get_match:
        return [{'tool': 'tm1_get', 'args': {'path': paw_get_match.group(1).strip()}}]

    # Web search: stock price, look up, search the web, current price, etc.
    wants_web_search = any(x in lower for x in (
        'current price', 'stock price', 'share price', 'price of ', 'what is the price',
        'look up', 'search the web', 'search the internet', 'find on the internet',
        'browse the internet', 'access the internet', 'access the web',
        'google ', 'search for ', 'look up ', 'can you find', 'real-time data',
        'aspen', 'aspen pharmacare', 'jse ', 'johannesburg stock exchange',
    ))
    if wants_web_search:
        return [{'tool': 'web_search', 'args': {'query': text.strip()}}]

    # "Execute this for me GET /Dimensions('account')/Elements?$filter=..." → extract path and run tm1_get.
    if re.search(r'\b(execute|run|run this)\b', lower) and re.search(r'\bGET\s+', text, re.IGNORECASE):
        path_match = re.search(r'\bGET\s+/?(.+)$', text, re.IGNORECASE | re.DOTALL)
        if path_match:
            path = path_match.group(1).strip()
            if path.startswith(("Dimensions(", "Cubes(", "Processes(", "Hierarchies(", "Cells(")) or "/Dimensions(" in path or "/Cubes(" in path:
                return [{'tool': 'tm1_get', 'args': {'path': path}}]

    # "List my accounts" / "retrieve elements" / "complete hierarchy of the account dimension" → fetch account dimension elements from TM1.
    wants_account_list = any(x in lower for x in (
        'list of my accounts', 'list my accounts', 'give me a list of my accounts',
        'list accounts', 'give me accounts', 'what are my accounts', 'my accounts',
        'list the accounts', 'show me the accounts', 'all accounts', 'list of accounts',
        'elements of the account dimension', 'account dimension elements',
        'elements in the account dimension', 'specific elements of the account dimension',
        'retrieve the elements of the account dimension', 'get the account dimension',
        'account dimension', 'elements of account',
        'hierarchy of elements', 'complete hierarchy', 'full hierarchy',
        'hierarchy of the account dimension', 'account dimension hierarchy',
    ))
    if wants_account_list:
        return [
            {'tool': 'tm1_get', 'args': {'path': "Cubes('Trail_Balance')?$expand=Dimensions($select=Name)"}},
            {'tool': 'tm1_get', 'args': {'path': "Dimensions('account')/Hierarchies('account')?$expand=Elements($select=Name;$top=500)"}},
        ]

    # User asks for cubes and/or dimensions (or "model" / Planning / TM1) → run live API via tools.
    # E.g. "retrieve cubes and dimensions from Klikk_Group_Planning_Production" / "list cubes" / "get dimensions".
    wants_cubes = any(x in lower for x in ('cube', 'cubes', 'list cube', 'get cube', 'retrieve cube'))
    wants_dims = any(x in lower for x in ('dimension', 'dimensions', 'dims', 'list dim', 'get dim', 'retrieve dim'))
    wants_tm1_or_model = any(x in lower for x in (
        'tm1', 'paw', 'planning analytics', 'planning model', 'production model',
        'klikk_group_planning', 'group_planning_production',
    ))
    if (wants_cubes or wants_dims or wants_tm1_or_model) and (
        'cube' in lower or 'dimension' in lower or 'retrieve' in lower or 'list' in lower or 'get ' in lower
    ):
        tool_calls = []
        if wants_cubes or (wants_tm1_or_model and 'cube' not in lower and 'dimension' not in lower):
            tool_calls.append({'tool': 'tm1_get', 'args': {'path': 'Cubes?$select=Name&$top=500'}})
        if wants_dims or (wants_tm1_or_model and 'dimension' in lower):
            tool_calls.append({'tool': 'tm1_get', 'args': {'path': 'Dimensions?$select=Name&$top=500'}})
        if tool_calls:
            return tool_calls

    # Common shorthand: "query cube" / "show dimensions"
    if 'trail_balance' in lower and ('dimension' in lower or 'dims' in lower):
        return [{'tool': 'tm1_get', 'args': {'path': "Cubes('Trail_Balance')?$expand=Dimensions($select=Name)"}}]

    # User asks for values/expenses/amounts from Trail_Balance for an entity (e.g. "4 Otterkuil") → run GET structure + MDX.
    if 'trail_balance' in lower or 'trail balance' in lower:
        wants_data = any(x in lower for x in ('value', 'expense', 'amount', 'month', 'monthly', 'data', 'retrieve', 'find', 'get ', 'show'))
        # Extract entity name: "4 Otterkuil" or quoted string after "for" / "entity"
        entity_name = '4 Otterkuil'
        for pat in [
            r"(?:for|entity)\s+[\"']([^\"']+)[\"']",
            r"[\"']([^\"']+)[\"']\s*(?:from|in)\s*(?:the\s+)?trail",
            r"(\d+\s*Otterkuil)",
            r"(Otterkuil)",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                entity_name = m.group(1).strip()
                break
        if wants_data:
            return [
                {'tool': 'tm1_get', 'args': {'path': "Cubes('Trail_Balance')?$expand=Dimensions($select=Name)"}},
                {'tool': 'tm1_mdx', 'args': {'query': f"SELECT NON EMPTY {{[measure_trail_balance].[amount]}} ON COLUMNS, NON EMPTY [month].[month].Members ON ROWS FROM [Trail_Balance] WHERE ([entity].[{entity_name}])"}},
            ]

    # If user asks to "use PAW API" / "TM1 API", auto-fetch useful metadata.
    if ('paw api' in lower) or ('planning analytics workspace' in lower) or ('tm1 api' in lower):
        tool_calls = []
        if 'process' in lower:
            tool_calls.append({'tool': 'tm1_get', 'args': {'path': 'Processes?$select=Name&$top=200'}})
        if 'dimension' in lower or 'dims' in lower:
            tool_calls.append({'tool': 'tm1_get', 'args': {'path': 'Dimensions?$select=Name&$top=200'}})
        if 'cube' in lower:
            tool_calls.append({'tool': 'tm1_get', 'args': {'path': 'Cubes?$select=Name&$top=200'}})
        if 'version' in lower:
            tool_calls.append({'tool': 'tm1_get', 'args': {'path': "Dimensions('Version')?$expand=Hierarchies($select=Name;$expand=Elements($select=Name;$top=200))"}})

        # Default: at least list cubes.
        if not tool_calls:
            tool_calls = [{'tool': 'tm1_get', 'args': {'path': 'Cubes?$select=Name&$top=200'}}]
        return tool_calls

    return []


def user_wants_vectorized_knowledge(message):
    """
    True when the user is asking about any of the agent's knowledge areas so we run RAG
    and inject relevant excerpts (Planning Analytics, Accounting/Tax SA, Financial Modeling/BI).
    """
    if not (message or '').strip():
        return False
    lower = (message or '').lower()
    triggers = (
        # Vectorized model / TM1 / PA
        'vectorised', 'vectorized', 'vectorise', 'vectorize',
        'read the model', 'read the vectorised', 'read the vectorized',
        'knowledge of', 'knowledge about', "what's in the model", 'whats in the model',
        'what is in our tm1', 'what is in the tm1 model', 'our tm1 model',
        'documentation for', 'model documentation', 'imported documentation',
        'what do we have in', 'what cubes', 'which cubes', 'which dimensions',
        'ti syntax', 'turbointegrator', 'rule syntax', 'mdx', 'planning analytics',
        'cognos', 'tm1 api', 'api endpoint', 'rest api',
        # Accounting, Tax, Booking South Africa
        'accounting', 'tax', 'vat', 'income tax', 'booking', 'south africa', 'sa accounting',
        'ifrs', 'companies act', 'sars', 'tax compliance', 'double entry',
        # Financial Modeling, BI, CMA
        'financial model', 'financial modeling', 'cma', 'chartered management accountant',
        'business intelligence', 'bi ', 'kpi', 'ratio', 'valuation', 'budgeting',
        'forecast', 'dashboard', 'management accounting',
    )
    return any(t in lower for t in triggers)


def generate_assistant_reply_with_tools(session, context_messages, tool_results):
    """
    Build final assistant response using tool execution outcomes.
    Uses LLM when configured; otherwise deterministic fallback.
    Injects actual response bodies (not just status) so the LLM can use cube names, dimension lists, cell values, etc.
    """
    if not tool_results:
        return generate_assistant_reply(session=session, context_messages=context_messages)

    summary_parts = []
    max_body_chars = 6000  # per tool, so we don't overflow context
    for idx, item in enumerate(tool_results, start=1):
        status = (
            'blocked by policy' if item.get('blocked') else
            f"failed (status_code={item.get('status_code', 0)})" if not item.get('success') else
            f"success (status_code={item.get('status_code', 0)})"
        )
        summary_parts.append(f"--- Tool {idx}: {item.get('tool')} -> {status}")
        result = item.get('result') or {}
        body = result.get('response_body')
        if isinstance(body, dict):
            try:
                snippet = json.dumps(body, ensure_ascii=False)[:max_body_chars]
                if len(json.dumps(body)) > max_body_chars:
                    snippet += '...'
                summary_parts.append(snippet)
            except Exception:
                summary_parts.append(str(body)[:max_body_chars])
        elif body and isinstance(body, (str, list)):
            summary_parts.append(str(body)[:max_body_chars])
        summary_parts.append('')

    tool_block = '\n'.join(summary_parts).strip()
    enriched_context = list(context_messages)
    enriched_context.append({
        'role': 'system',
        'content': (
            'Tool execution results (use the actual data below in your answer—cube names, dimensions, cell values, etc.):\n\n'
            f'{tool_block}'
        ),
    })
    return generate_assistant_reply(session=session, context_messages=enriched_context)

