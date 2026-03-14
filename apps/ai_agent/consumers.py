"""
WebSocket consumer for live AI agent chat.

Connect to: ws://<host>/ws/ai-agent/chat/
Optional: ws://<host>/ws/ai-agent/chat/<session_id>/

Status updates are delivered via a separate REST endpoint (GET /api/ai-agent/agent-status/)
which the frontend polls, avoiding WebSocket threading issues entirely.

Send message format:
    {"type": "send", "message": "What is ABG's dividend forecast?", "session_id": 1}
    {"type": "send", "message": "...", "history": [...]}  # stateless mode (no session)
    {"type": "filter", "session_id": 2}                   # update subscription filter
"""
import json
import logging
import threading
import time
import traceback

from channels.generic.websocket import WebsocketConsumer

log = logging.getLogger('ai_agent')

_subscribers: set['ChatObserverConsumer'] = set()
_lock = threading.Lock()

# ---------------------------------------------------------------------------
#  In-memory agent status store (polled by REST endpoint)
# ---------------------------------------------------------------------------

_agent_status: dict[str, dict] = {}
_status_lock = threading.Lock()


def set_agent_status(key: str, status_msg: str, tool_calls: list | None = None):
    """Called from the agent's on_event callback to update current status."""
    with _status_lock:
        _agent_status[key] = {
            "status": status_msg,
            "tool_calls": tool_calls or [],
            "updated_at": time.time(),
        }


def get_agent_status(key: str) -> dict | None:
    """Called by the REST endpoint to read current status."""
    with _status_lock:
        return _agent_status.get(key)


def clear_agent_status(key: str):
    with _status_lock:
        _agent_status.pop(key, None)


def broadcast_message(payload: dict):
    """Called from signals.py when an AgentMessage is saved."""
    with _lock:
        consumers = list(_subscribers)

    for consumer in consumers:
        try:
            consumer.send_event(payload)
        except Exception:
            log.debug("Failed to broadcast to consumer", exc_info=True)
            with _lock:
                _subscribers.discard(consumer)


class ChatObserverConsumer(WebsocketConsumer):
    """WebSocket endpoint for observing and interacting with the AI agent."""

    def connect(self):
        self.session_filter = self.scope.get('url_route', {}).get('kwargs', {}).get('session_id')
        if not self.session_filter:
            qs = self.scope.get('query_string', b'').decode()
            for param in qs.split('&'):
                if param.startswith('session_id='):
                    self.session_filter = param.split('=', 1)[1]

        with _lock:
            _subscribers.add(self)
        self.accept()
        self.send(text_data=json.dumps({
            'type': 'connected',
            'session_filter': str(self.session_filter) if self.session_filter else 'all',
        }))
        log.info("Chat observer connected (filter=%s)", self.session_filter or 'all')

    def disconnect(self, close_code):
        with _lock:
            _subscribers.discard(self)
        log.info("Chat observer disconnected")

    def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            self.send(text_data=json.dumps({'type': 'error', 'error': 'Invalid JSON'}))
            return

        msg_type = data.get('type', 'send')

        if msg_type == 'filter':
            self.session_filter = str(data['session_id']) if data.get('session_id') else None
            self.send(text_data=json.dumps({
                'type': 'filter_updated',
                'session_filter': self.session_filter or 'all',
            }))
            return

        if msg_type == 'send':
            self._handle_send(data)
            return

        self.send(text_data=json.dumps({'type': 'error', 'error': f'Unknown type: {msg_type}'}))

    def _handle_send(self, data: dict):
        message = (data.get('message') or '').strip()
        if not message:
            self.send(text_data=json.dumps({'type': 'error', 'error': 'message is required'}))
            return

        status_key = self.session_filter or 'default'
        set_agent_status(status_key, "Thinking...")

        self.send(text_data=json.dumps({
            'type': 'ack',
            'message': message,
            'status_key': status_key,
        }))

        thread = threading.Thread(
            target=self._run_agent,
            args=(message, data, status_key),
            daemon=True,
        )
        thread.start()

    def _make_on_event(self, status_key: str):
        """Create an on_event callback that writes to the in-memory status store."""
        tool_calls = []

        def on_event(event: dict):
            if event.get("type") == "status":
                set_agent_status(status_key, event.get("message", ""), tool_calls)
            elif event.get("type") == "tool_call":
                tool_calls.append({
                    "name": event.get("name", ""),
                    "skill": event.get("skill", ""),
                    "status": event.get("status", ""),
                    "started_at": time.time(),
                })
                set_agent_status(
                    status_key,
                    event.get("status", f"Running {event.get('name', '')}..."),
                    tool_calls,
                )

        return on_event

    def _run_agent(self, message: str, data: dict, status_key: str):
        try:
            import django
            django.setup()

            on_event = self._make_on_event(status_key)
            session_id_raw = data.get('session_id') or self.session_filter
            history = data.get('history', [])

            try:
                session_id = int(session_id_raw) if session_id_raw else None
            except (TypeError, ValueError):
                session_id = None

            if session_id:
                response_text, tool_calls_info = self._run_with_session(
                    message, session_id, on_event
                )
            else:
                response_text, tool_calls_info = self._run_stateless(
                    message, history, on_event
                )

            clear_agent_status(status_key)
            self.send(text_data=json.dumps({
                'type': 'response',
                'role': 'assistant',
                'content': response_text,
                'tool_calls': tool_calls_info,
            }, default=str))

        except Exception as e:
            clear_agent_status(status_key)
            log.error("Agent error via WebSocket: %s", e, exc_info=True)
            try:
                self.send(text_data=json.dumps({
                    'type': 'error',
                    'error': f'{type(e).__name__}: {e}',
                    'traceback': traceback.format_exc(),
                }))
            except Exception:
                pass

    def _run_stateless(self, message: str, history: list, on_event) -> tuple[str, list]:
        from apps.ai_agent.agent.core import run_agent

        result = run_agent(message, history, on_event=on_event)
        if isinstance(result, tuple):
            text, tool_calls, skills_routed = result
            return text, [
                {
                    'name': getattr(tc, 'name', str(tc)),
                    'input': getattr(tc, 'input', {}),
                    'result': str(getattr(tc, 'result', ''))[:500],
                    'skill': getattr(tc, 'skill', ''),
                }
                for tc in tool_calls
            ]
        return str(result), []

    def _run_with_session(self, message: str, session_id: int, on_event) -> tuple[str, list]:
        from apps.ai_agent.models import AgentMessage, AgentSession
        from apps.ai_agent.agent.core import run_agent

        try:
            session = AgentSession.objects.get(id=session_id)
        except AgentSession.DoesNotExist:
            return f"Session {session_id} not found.", []

        AgentMessage.objects.create(session=session, role='user', content=message)
        db_messages = session.messages.order_by('created_at').values_list('role', 'content')
        history = [{'role': r, 'content': c} for r, c in db_messages if r in ('user', 'assistant')]
        if history and history[-1]['role'] == 'user':
            history = history[:-1]

        result = run_agent(message, history, on_event=on_event)
        if isinstance(result, tuple):
            text, tool_calls, skills_routed = result
        else:
            text, tool_calls, skills_routed = str(result), [], []

        AgentMessage.objects.create(
            session=session, role='assistant', content=text,
            metadata={
                'tool_calls': [getattr(tc, 'name', str(tc)) for tc in tool_calls],
                'skills_routed': skills_routed,
            },
        )
        return text, [
            {
                'name': getattr(tc, 'name', str(tc)),
                'input': getattr(tc, 'input', {}),
                'result': str(getattr(tc, 'result', ''))[:500],
                'skill': getattr(tc, 'skill', ''),
            }
            for tc in tool_calls
        ]

    def send_event(self, payload: dict):
        """Send a broadcast event if it matches the session filter."""
        if self.session_filter:
            msg_session = str(payload.get('session_id', ''))
            if msg_session != self.session_filter:
                return
        try:
            self.send(text_data=json.dumps(payload, default=str))
        except Exception:
            with _lock:
                _subscribers.discard(self)
