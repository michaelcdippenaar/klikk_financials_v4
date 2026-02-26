"""
Test the AI-agent chatbot in-process (no HTTP server required).

Creates a session, sends a message, and runs the same chat runner logic
used by the run-with-tools API. Use for CI or quick verification from Cursor.

Usage:
  python manage.py test_chatbot
  python manage.py test_chatbot --message "Find accounts relating to property"
  python manage.py test_chatbot --assert-response   # exit non-zero if reply lacks expected keywords
"""
from django.core.management.base import BaseCommand

from apps.ai_agent.models import AgentMessage, AgentSession
from apps.ai_agent.services.chat_runner import (
    build_context_messages,
    generate_assistant_reply_with_tool_use,
)


class Command(BaseCommand):
    help = 'Query the AI-agent chatbot in-process to test functionality (no server required).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--message',
            default='Find accounts relating to property',
            help='User message to send to the chatbot',
        )
        parser.add_argument(
            '--project',
            type=int,
            default=None,
            help='Optional project id to attach to the session (for RAG/corpus).',
        )
        parser.add_argument(
            '--assert-response',
            action='store_true',
            help='Exit with non-zero code if reply does not contain expected keywords.',
        )

    def handle(self, *args, **options):
        message = (options['message'] or '').strip()
        if not message:
            self.stderr.write(self.style.ERROR('Message cannot be empty.'))
            return 1

        project_id = options['project']
        assert_response = options['assert_response']

        # Create a session (no auth user in management command)
        session = AgentSession.objects.create(
            project_id=project_id,
            created_by=None,
            organisation=None,
            title='test_chatbot',
        )

        try:
            # Add user message (same as the view)
            AgentMessage.objects.create(
                session=session,
                role=AgentMessage.ROLE_USER,
                content=message,
                metadata={},
                created_by=None,
            )

            context_messages, context_message_count, context_limit = build_context_messages(session)
            self.stdout.write(
                f'Context: {context_message_count} messages (limit {context_limit})'
            )

            llm_result = generate_assistant_reply_with_tool_use(
                session=session,
                context_messages=context_messages,
                user_message=message,
            )

            content = llm_result.get('content') or ''
            provider = llm_result.get('provider', '')
            model = llm_result.get('model', '')

            self.stdout.write('--- Assistant reply ---')
            self.stdout.write(content)
            self.stdout.write('---')
            self.stdout.write(self.style.SUCCESS(f'Provider: {provider}, Model: {model}'))

            if assert_response:
                content_lower = content.lower()
                keywords = ['dimensions', 'account', 'elements', 'property']
                if not any(kw in content_lower for kw in keywords):
                    self.stderr.write(
                        self.style.ERROR(
                            f'Assert failed: reply should contain one of {keywords}'
                        )
                    )
                    return 2

            return 0
        finally:
            session.delete()
