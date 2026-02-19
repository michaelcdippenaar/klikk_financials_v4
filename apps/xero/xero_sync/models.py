from django.db import models
from django.utils import timezone
import datetime
import logging
from apps.xero.xero_core.models import XeroTenant
import pytz
import json

logger = logging.getLogger(__name__)


class XeroLastUpdateModelManager(models.Manager):
    def update_or_create_timestamp(self, end_point, organisation):
        """Update or create timestamp for an endpoint - simple version like v2."""
        utc_now = datetime.datetime.now(tz=pytz.utc)
        self.update_or_create(
            end_point=end_point,
            organisation=organisation,
            defaults={'date': utc_now}
        )

    def get_utc_date_time(self, end_point, organisation):
        """Get the date as ISO string for API calls."""
        instance, created = self.get_or_create(
            end_point=end_point,
            organisation=organisation,
            defaults={'date': None}
        )
        if created or instance.date is None:
            return '1901-01-01T00:00:00'  # Default for new or None dates
        return instance.date.isoformat(timespec='seconds')
    
    def get_last_update(self, end_point, organisation):
        """Get the last update instance."""
        instance, created = self.get_or_create(
            end_point=end_point,
            organisation=organisation,
            defaults={'date': None}
        )
        return instance


class XeroLastUpdate(models.Model):
    """
    Tracks last update timestamps for various Xero endpoints.
    Simplified version - only tracks date of last successful update.
    
    Endpoints tracked:
    - accounts: Account metadata
    - contacts: Contact metadata
    - tracking_categories: Tracking category metadata
    - journals: Regular journals
    - manual_journals: Manual journals
    - profit_loss: Profit & Loss reports
    """
    ENDPOINT_CHOICES = [
        ('accounts', 'Accounts'),
        ('contacts', 'Contacts'),
        ('tracking_categories', 'Tracking Categories'),
        ('journals', 'Journals'),
        ('manual_journals', 'Manual Journals'),
        ('profit_loss', 'Profit & Loss'),
        ('bank_transactions', 'Bank Transactions'),
        ('invoices', 'Invoices'),
        ('payments', 'Payments'),
        ('credit_notes', 'Credit Notes'),
        ('prepayments', 'Prepayments'),
        ('overpayments', 'Overpayments'),
    ]
    
    name = models.CharField(max_length=200, blank=True, null=True, unique=True, help_text="Optional unique name/identifier for this update record")
    end_point = models.CharField(max_length=100, choices=ENDPOINT_CHOICES)
    organisation = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='last_updates')
    date = models.DateTimeField(blank=True, null=True, help_text="Last successful update timestamp")

    objects = XeroLastUpdateModelManager()

    class Meta:
        unique_together = [('organisation', 'end_point')]
        indexes = [
            models.Index(fields=['organisation', 'end_point'], name='last_update_org_endpoint_idx'),
            models.Index(fields=['name'], name='last_update_name_idx'),
        ]

    def __str__(self):
        if self.date:
            return f"{self.organisation.tenant_name}: {self.end_point} last updated at {self.date}"
        else:
            return f"{self.organisation.tenant_name}: {self.end_point} (never updated)"


class XeroTenantSchedule(models.Model):
    """Configuration for scheduled tasks per tenant."""
    tenant = models.OneToOneField(XeroTenant, on_delete=models.CASCADE, related_name='schedule')
    enabled = models.BooleanField(default=True, help_text="Enable/disable scheduled tasks for this tenant")
    update_interval_minutes = models.IntegerField(default=60, help_text="Minutes between update runs")
    update_start_time = models.TimeField(default=datetime.time(0, 0), help_text="Preferred start time for updates")
    last_update_run = models.DateTimeField(null=True, blank=True, help_text="Last time update task ran")
    last_process_run = models.DateTimeField(null=True, blank=True, help_text="Last time process task ran")
    next_update_run = models.DateTimeField(null=True, blank=True, help_text="Next scheduled update run")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['tenant__tenant_name']

    def __str__(self):
        return f"Schedule for {self.tenant.tenant_name}"

    def should_run_update(self):
        """Check if update task should run now."""
        if not self.enabled:
            return False
        if not self.next_update_run:
            return True
        return timezone.now() >= self.next_update_run

    def should_run_process(self):
        """
        Check if process task should run now (only after update completes).
        Process runs immediately after update, so check if update just completed.
        """
        if not self.enabled:
            return False
        if not self.last_update_run:
            return False  # Don't run process until update has run at least once
        
        # Process should run if update just completed and process hasn't run yet, or
        # if process hasn't run since the last update
        if not self.last_process_run:
            return True  # Process hasn't run yet after update
        
        # Process should run if it hasn't run since the last update
        return self.last_process_run < self.last_update_run

    def update_next_run_times(self):
        """Update next run times based on intervals (in minutes)."""
        now = timezone.now()
        if self.last_update_run:
            # Calculate next update time based on interval
            self.next_update_run = self.last_update_run + datetime.timedelta(minutes=self.update_interval_minutes)
        else:
            # First run - schedule for today at preferred time, or tomorrow if time has passed
            preferred_time = now.replace(
                hour=self.update_start_time.hour,
                minute=self.update_start_time.minute,
                second=0,
                microsecond=0
            )
            
            # If preferred time is in the past today, schedule for tomorrow
            if preferred_time <= now:
                self.next_update_run = preferred_time + datetime.timedelta(days=1)
            else:
                self.next_update_run = preferred_time
        
        # Process doesn't have a separate schedule - it runs immediately after update
        # So we don't set next_process_run anymore
        self.save()


class XeroApiCallLog(models.Model):
    """
    Log Xero API call counts per process run.
    Used to track usage against Xero's rate limits and display in Admin Console.
    """
    PROCESS_CHOICES = [
        ('metadata', 'Update Metadata'),
        ('data', 'Sync Transactions & Journals'),
        ('journals', 'Process Journals'),
        ('trail-balance', 'Build Trail Balance'),
        ('pnl-by-tracking', 'Import P&L by Tracking'),
        ('reconcile', 'Reconcile Reports'),
    ]

    process = models.CharField(max_length=50, choices=PROCESS_CHOICES)
    tenant = models.ForeignKey(
        XeroTenant, on_delete=models.CASCADE, related_name='api_call_logs', null=True, blank=True
    )
    api_calls = models.IntegerField(default=0, help_text="Number of Xero API calls made in this run")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['process', 'created_at'], name='api_call_process_date_idx'),
            models.Index(fields=['tenant', 'created_at'], name='api_call_tenant_date_idx'),
        ]


class XeroTaskExecutionLog(models.Model):
    """Log execution stats for scheduled tasks."""
    TASK_TYPES = [
        ('update_models', 'Update Models'),
        ('process_data', 'Process Data'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
    ]

    tenant = models.ForeignKey(XeroTenant, on_delete=models.CASCADE, related_name='task_logs')
    task_type = models.CharField(max_length=20, choices=TASK_TYPES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True, help_text="Task duration in seconds")
    records_processed = models.IntegerField(null=True, blank=True, help_text="Number of records processed")
    error_message = models.TextField(null=True, blank=True)
    stats = models.JSONField(default=dict, blank=True, help_text="Additional statistics (e.g., API calls, DB queries)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['tenant', 'task_type', 'status'], name='task_log_tenant_type_idx'),
            models.Index(fields=['tenant', 'started_at'], name='task_log_tenant_date_idx'),
            models.Index(fields=['status', 'started_at'], name='task_log_status_date_idx'),
        ]

    def __str__(self):
        return f"{self.tenant.tenant_name} - {self.get_task_type_display()} - {self.status}"

    def mark_completed(self, duration_seconds=None, records_processed=None, stats=None):
        """Mark task as completed with stats."""
        self.status = 'completed'
        self.completed_at = timezone.now()
        if duration_seconds is not None:
            self.duration_seconds = duration_seconds
        elif self.started_at:
            self.duration_seconds = (self.completed_at - self.started_at).total_seconds()
        if records_processed is not None:
            self.records_processed = records_processed
        if stats:
            self.stats = stats
        self.save()

    def mark_failed(self, error_message, duration_seconds=None):
        """Mark task as failed with error message."""
        self.status = 'failed'
        self.completed_at = timezone.now()
        self.error_message = error_message
        if duration_seconds is not None:
            self.duration_seconds = duration_seconds
        elif self.started_at:
            self.duration_seconds = (self.completed_at - self.started_at).total_seconds()
        self.save()


class Trigger(models.Model):
    """
    A trigger that determines if a process or process tree should run.
    Triggers can be based on conditions, schedules, events, or custom logic.
    """
    TRIGGER_TYPES = [
        ('condition', 'Condition'),
        ('schedule', 'Schedule'),
        ('event', 'Event'),
        ('custom', 'Custom Function'),
        ('outdated_check', 'Outdated Data Check'),
    ]
    
    name = models.CharField(
        max_length=200,
        unique=True,
        help_text="Unique name/identifier for this trigger"
    )
    trigger_type = models.CharField(
        max_length=50,
        choices=TRIGGER_TYPES,
        default='condition',
        help_text="Type of trigger"
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Whether this trigger is enabled"
    )
    description = models.TextField(
        blank=True,
        help_text="Description of what this trigger checks"
    )
    
    # Condition/configuration stored as JSON
    # For 'condition': {"field": "value", "operator": "equals", ...}
    # For 'schedule': {"interval_minutes": 60, "start_time": "09:00", ...}
    # For 'event': {"event_name": "data_updated", ...}
    # For 'custom': {"function_ref": "module.function", ...}
    # For 'outdated_check': {"xero_last_update_id": 123, ...}
    configuration = models.JSONField(
        default=dict,
        blank=True,
        help_text="Trigger configuration/parameters"
    )
    
    # Optional: Link to XeroLastUpdate for outdated checks
    xero_last_update = models.ForeignKey(
        'XeroLastUpdate',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='triggers',
        help_text="Optional XeroLastUpdate record for outdated checks"
    )
    
    # Optional: Link to ProcessTree for process tree triggers (legacy - use subscriptions instead)
    process_tree = models.ForeignKey(
        'ProcessTree',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='triggers',
        help_text="Optional ProcessTree this trigger is associated with (legacy - use subscriptions)"
    )
    
    # Note: ProcessTree now has a ForeignKey to Trigger (one-to-many relationship)
    # Use the reverse relation: trigger.process_trees.all() to get all subscribed trees
    
    # Trigger state (for manual control by external processes)
    TRIGGER_STATES = [
        ('pending', 'Pending'),
        ('fired', 'Fired'),
        ('reset', 'Reset'),
    ]
    state = models.CharField(
        max_length=20,
        choices=TRIGGER_STATES,
        default='pending',
        help_text="Current state of the trigger (can be set by external processes)"
    )
    
    # Last check/execution tracking
    last_checked = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time this trigger was checked"
    )
    last_triggered = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time this trigger fired (returned True)"
    )
    last_fired_manually = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time this trigger was manually fired by external process"
    )
    trigger_count = models.IntegerField(
        default=0,
        help_text="Number of times this trigger has fired"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['enabled', 'trigger_type'], name='trigger_enabled_type_idx'),
            models.Index(fields=['process_tree', 'enabled'], name='trigger_tree_enabled_idx'),
            models.Index(fields=['state', 'enabled'], name='trigger_state_enabled_idx'),
        ]

    def __str__(self):
        return f"Trigger: {self.name} ({self.trigger_type})"

    def fire(self, context: dict = None, fired_by: str = None):
        """
        Manually fire this trigger (for external processes).
        This will execute all subscribed process trees.
        
        Args:
            context: Optional context dict to pass to subscribed trees
            fired_by: Optional identifier of what fired this trigger (for logging)
        
        Returns:
            Dict with execution results for all subscribed trees
        """
        if not self.enabled:
            logger.warning(f"Trigger '{self.name}' is disabled, cannot fire")
            return {'success': False, 'error': 'Trigger is disabled'}
        
        now = timezone.now()
        context = context or {}
        
        # Update trigger state
        self.state = 'fired'
        self.last_triggered = now
        self.last_fired_manually = now
        self.trigger_count += 1
        self.save(update_fields=['state', 'last_triggered', 'last_fired_manually', 'trigger_count'])
        
        logger.info(f"Trigger '{self.name}' fired manually by: {fired_by or 'external process'}")
        
        # Execute all subscribed process trees
        results = self.execute_subscribed_trees(context)
        
        return {
            'success': True,
            'trigger': self.name,
            'fired_by': fired_by,
            'subscribed_trees': results
        }
    
    def reset(self):
        """
        Reset trigger state to 'pending'.
        Useful for external processes to reset trigger after handling.
        """
        self.state = 'pending'
        self.save(update_fields=['state'])
        logger.info(f"Trigger '{self.name}' reset to pending state")
    
    def execute_subscribed_trees(self, context: dict = None) -> dict:
        """
        Execute all process trees subscribed to this trigger.
        
        Args:
            context: Optional context dict to pass to trees
        
        Returns:
            Dict mapping tree names to execution results
        """
        from apps.xero.xero_sync.process_manager.tree_builder import ProcessTreeManager
        
        context = context or {}
        results = {}
        
        # Get all process trees that subscribe to this trigger (one-to-many relationship)
        subscribed_trees = self.process_trees.filter(enabled=True)
        
        if not subscribed_trees.exists():
            logger.info(f"No enabled trees subscribed to trigger '{self.name}'")
            return results
        
        logger.info(f"Executing {subscribed_trees.count()} trees subscribed to trigger '{self.name}'")
        
        for tree in subscribed_trees:
            try:
                tree_result = ProcessTreeManager.execute_tree(
                    tree.name,
                    context=context,
                    func_registry={}
                )
                results[tree.name] = tree_result
                logger.info(f"Executed subscribed tree '{tree.name}': success={tree_result.get('success', False)}")
            except Exception as e:
                logger.error(f"Error executing subscribed tree '{tree.name}': {str(e)}", exc_info=True)
                results[tree.name] = {'success': False, 'error': str(e)}
        
        return results
    
    def subscribe_tree(self, tree_name: str):
        """
        Subscribe a process tree to this trigger.
        
        Args:
            tree_name: Name of the ProcessTree to subscribe
        """
        # Import here to avoid circular import
        from apps.xero.xero_sync.models import ProcessTree
        
        try:
            tree = ProcessTree.objects.get(name=tree_name)
            tree.trigger = self
            tree.save(update_fields=['trigger'])
            logger.info(f"Subscribed tree '{tree_name}' to trigger '{self.name}'")
        except ProcessTree.DoesNotExist:
            raise ValueError(f"ProcessTree '{tree_name}' not found")
    
    def unsubscribe_tree(self, tree_name: str):
        """
        Unsubscribe a process tree from this trigger.
        
        Args:
            tree_name: Name of the ProcessTree to unsubscribe
        """
        # Import here to avoid circular import
        from apps.xero.xero_sync.models import ProcessTree
        
        try:
            tree = ProcessTree.objects.get(name=tree_name)
            if tree.trigger == self:
                tree.trigger = None
                tree.save(update_fields=['trigger'])
                logger.info(f"Unsubscribed tree '{tree_name}' from trigger '{self.name}'")
            else:
                logger.warning(f"Tree '{tree_name}' is not subscribed to trigger '{self.name}'")
        except ProcessTree.DoesNotExist:
            raise ValueError(f"ProcessTree '{tree_name}' not found")
    
    def should_trigger(self, context: dict = None) -> bool:
        """
        Check if this trigger should fire (return True to run the process).
        Checks both automatic conditions and manual state.
        
        Args:
            context: Optional context dict for evaluation
        
        Returns:
            True if trigger should fire (process should run), False otherwise
        """
        if not self.enabled:
            return False
        
        # If state is 'fired', trigger should fire (manual trigger)
        if self.state == 'fired':
            return True
        
        # If state is 'reset', check automatic conditions
        if self.state == 'reset':
            # Reset to pending after checking
            self.state = 'pending'
            self.save(update_fields=['state'])
        
        context = context or {}
        now = timezone.now()
        
        # Update last_checked
        self.last_checked = now
        
        try:
            result = False
            
            if self.trigger_type == 'condition':
                result = self._check_condition(context)
            elif self.trigger_type == 'schedule':
                result = self._check_schedule()
            elif self.trigger_type == 'event':
                result = self._check_event(context)
            elif self.trigger_type == 'custom':
                result = self._check_custom(context)
            elif self.trigger_type == 'outdated_check':
                result = self._check_outdated()
            
            # Update tracking if triggered
            if result:
                self.last_triggered = now
                self.trigger_count += 1
                self.save(update_fields=['last_checked', 'last_triggered', 'trigger_count'])
            else:
                self.save(update_fields=['last_checked'])
            
            return result
            
        except Exception as e:
            logger.error(f"Error checking trigger '{self.name}': {str(e)}", exc_info=True)
            return False
    
    def _check_condition(self, context: dict) -> bool:
        """Check condition-based trigger."""
        config = self.configuration or {}
        field = config.get('field')
        operator = config.get('operator', 'equals')
        value = config.get('value')
        
        if not field:
            return False
        
        context_value = context.get(field)
        
        if operator == 'equals':
            return context_value == value
        elif operator == 'not_equals':
            return context_value != value
        elif operator == 'greater_than':
            return context_value > value
        elif operator == 'less_than':
            return context_value < value
        elif operator == 'exists':
            return field in context and context[field] is not None
        elif operator == 'not_exists':
            return field not in context or context[field] is None
        
        return False
    
    def _check_schedule(self) -> bool:
        """Check schedule-based trigger."""
        config = self.configuration or {}
        interval_minutes = config.get('interval_minutes', 60)
        last_triggered = self.last_triggered
        
        if not last_triggered:
            return True  # First run
        
        next_run = last_triggered + datetime.timedelta(minutes=interval_minutes)
        return timezone.now() >= next_run
    
    def _check_event(self, context: dict) -> bool:
        """Check event-based trigger."""
        config = self.configuration or {}
        event_name = config.get('event_name')
        
        if not event_name:
            return False
        
        # Check if event is in context
        events = context.get('events', [])
        return event_name in events
    
    def _check_custom(self, context: dict) -> bool:
        """Check custom function trigger."""
        config = self.configuration or {}
        function_ref = config.get('function_ref')
        
        if not function_ref:
            return False
        
        try:
            # Import and call the function
            module_path, func_name = function_ref.rsplit('.', 1)
            module = __import__(module_path, fromlist=[func_name])
            func = getattr(module, func_name)
            return func(context=context)
        except Exception as e:
            logger.error(f"Error calling custom trigger function '{function_ref}': {str(e)}")
            return False
    
    def _check_outdated(self) -> bool:
        """Check outdated data trigger."""
        if self.xero_last_update:
            # Use the XeroLastUpdate record
            last_update = self.xero_last_update
            if not last_update.date:
                return True  # Never updated, should run
            
            # Since out_of_sync field was removed, we consider it out of sync if date is None
            # (already checked above, so this is just for clarity)
            
            # Check if enough time has passed
            config = self.configuration or {}
            max_age_minutes = config.get('max_age_minutes')
            if max_age_minutes:
                age = timezone.now() - last_update.date
                return age.total_seconds() / 60 > max_age_minutes
        
        return False


class ProcessTree(models.Model):
    """
    Stores process tree definitions in the database.
    Process trees can be built programmatically and stored for reuse.
    """
    name = models.CharField(max_length=100, unique=True, help_text="Unique name for the process tree")
    description = models.TextField(blank=True, help_text="Description of what this process tree does")
    process_tree_data = models.JSONField(help_text="Process tree definition (processes, dependencies, etc.)")
    response_variables = models.JSONField(default=dict, blank=True, help_text="Response variable definitions")
    cache_enabled = models.BooleanField(default=True, help_text="Whether caching is enabled")
    enabled = models.BooleanField(default=True, help_text="Whether this process tree is enabled")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Dependent trees (run after this tree completes)
    dependent_trees = models.ManyToManyField(
        'self',
        symmetrical=False,
        related_name='parent_trees',
        blank=True,
        help_text="Process trees that run after this one completes"
    )
    
    # Sibling trees (run in parallel/async)
    sibling_trees = models.ManyToManyField(
        'self',
        symmetrical=True,
        blank=True,
        help_text="Process trees that run in parallel with this one"
    )
    
    # Trigger subscription (one-to-many: one trigger can have many process trees)
    trigger = models.ForeignKey(
        'Trigger',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='process_trees',
        help_text="Trigger this process tree subscribes to"
    )

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['name', 'enabled'], name='process_tree_name_enabled_idx'),
        ]

    def __str__(self):
        return f"ProcessTree: {self.name}"

    def get_process_tree_dict(self) -> dict:
        """Get process tree as dictionary."""
        return self.process_tree_data

    def get_response_variables_dict(self) -> dict:
        """Get response variables as dictionary."""
        return self.response_variables or {}

    def add_dependent_tree(self, tree_name: str):
        """Add a dependent tree that runs after this one completes."""
        try:
            dependent_tree = ProcessTree.objects.get(name=tree_name)
            self.dependent_trees.add(dependent_tree)
        except ProcessTree.DoesNotExist:
            raise ValueError(f"Process tree '{tree_name}' not found")

    def add_sibling_tree(self, tree_name: str):
        """Add a sibling tree that runs in parallel with this one."""
        try:
            sibling_tree = ProcessTree.objects.get(name=tree_name)
            self.sibling_trees.add(sibling_tree)
        except ProcessTree.DoesNotExist:
            raise ValueError(f"Process tree '{tree_name}' not found")


class ProcessTreeSchedule(models.Model):
    """
    Configuration for scheduling a process tree to run automatically.
    """
    process_tree = models.OneToOneField(
        ProcessTree,
        on_delete=models.CASCADE,
        related_name='schedule',
        help_text="The process tree to schedule"
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Enable/disable scheduled execution for this process tree"
    )
    interval_minutes = models.IntegerField(
        default=60,
        help_text="Minutes between executions"
    )
    start_time = models.TimeField(
        default=datetime.time(0, 0),
        help_text="Preferred start time for executions"
    )
    last_run = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time this process tree was executed"
    )
    next_run = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Next scheduled execution time"
    )
    context = models.JSONField(
        default=dict,
        blank=True,
        help_text="Default context to pass to the process tree execution"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['process_tree__name']
        indexes = [
            models.Index(fields=['enabled', 'next_run'], name='process_tree_schedule_idx'),
        ]

    def __str__(self):
        return f"Schedule for {self.process_tree.name}"

    def should_run(self):
        """Check if process tree should run now."""
        if not self.enabled:
            return False
        if not self.next_run:
            return True
        return timezone.now() >= self.next_run

    def update_next_run_time(self):
        """Update next run time based on interval."""
        now = timezone.now()
        if self.last_run:
            # Calculate next run time based on interval
            self.next_run = self.last_run + datetime.timedelta(minutes=self.interval_minutes)
        else:
            # First run - schedule for today at preferred time, or tomorrow if time has passed
            preferred_time = now.replace(
                hour=self.start_time.hour,
                minute=self.start_time.minute,
                second=0,
                microsecond=0
            )
            
            # If preferred time is in the past today, schedule for tomorrow
            if preferred_time <= now:
                self.next_run = preferred_time + datetime.timedelta(days=1)
            else:
                self.next_run = preferred_time
        
        self.save()
