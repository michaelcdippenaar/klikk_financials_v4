"""Poll for queued ingest runs and execute them outside the request cycle."""

import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from apps.web_api_v2.services.ingest_execution import (
    claim_next_run,
    execute_claimed_run,
    reap_expired_runs,
)


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Execute queued V2 ingest process runs. Safe to run more than one.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--poll-seconds', type=float, default=5.0,
            help='Idle wait between polls. Ignored while work remains.',
        )
        parser.add_argument(
            '--once', action='store_true',
            help='Drain the queue and exit, rather than run forever. For tests and cron.',
        )
        parser.add_argument(
            '--max-runs', type=int, default=0,
            help='Stop after this many runs. 0 means no limit.',
        )

    def handle(self, *args, **options):
        stopping = {'now': False}

        def _stop(signum, _frame):
            # Finish the run in hand rather than abandoning it mid-provider-call
            # and leaving a lease to expire.
            stopping['now'] = True
            self.stdout.write(f'Signal {signum} received; finishing current run then stopping.')

        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, _stop)

        completed = 0
        limit = options['max_runs']
        while not stopping['now']:
            # A daemon must drop connections the database has already closed.
            # In --once mode the caller owns the connection — closing it there
            # would tear down a surrounding transaction.
            if not options['once']:
                close_old_connections()
            reaped = reap_expired_runs()
            if reaped:
                self.stdout.write(f'Reaped {reaped} run(s) with an expired lease.')

            run = claim_next_run()
            if run is None:
                if options['once']:
                    break
                time.sleep(options['poll_seconds'])
                continue

            self.stdout.write(f'Running {run.process_key} for entity {run.entity_id} ({run.pk}).')
            execute_claimed_run(run)
            completed += 1
            self.stdout.write(f'  -> {run.state}')
            if limit and completed >= limit:
                break

        self.stdout.write(self.style.SUCCESS(f'Worker stopped after {completed} run(s).'))
