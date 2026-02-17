"""
Examples of using ProcessTreeBuilder and ProcessTreeManager.

This demonstrates how to build process trees programmatically,
store them in the database, and execute them by name.

For detailed documentation and examples, see EXAMPLES.md in this directory.
"""
from apps.xero.xero_sync.process_manager.tree_builder import ProcessTreeBuilder, ProcessTreeManager, ProcessTreeInstance
from apps.xero.xero_sync.process_manager.xero_builder import build_xero_sync_tree
from apps.xero.xero_sync.process_manager.xero import check_xero_sync_status


def example_register_process_tree_schedule():
    """Example: Register a process tree with the task scheduler."""
    from apps.xero.xero_core.models import XeroTenant
    from apps.xero.xero_data.models import XeroJournalsSource
    from apps.xero.xero_sync.models import XeroLastUpdate
    from apps.xero.xero_sync.process_manager.outdated_checkers import create_data_outdated_checker
    
    # Create or load process tree
    tree = ProcessTreeInstance('scheduled_journals_sync', overwrite=True, description='Scheduled journals sync')
    
    # Get organisation
    organisation = XeroTenant.objects.first()
    if not organisation:
        print("No XeroTenant found. Please create one first.")
        return
    
    # Create outdated checker trigger first
    last_update, created = XeroLastUpdate.objects.get_or_create(
        organisation=organisation,
        end_point='journals',
        defaults={'name': f'{organisation.tenant_name}_journals_update'}
    )
    
    # Create trigger for outdated check
    trigger = tree.create_trigger(
        name='journals_outdated_trigger',
        trigger_type='outdated_check',
        xero_last_update_id=last_update.id,
        configuration={'max_age_minutes': 60},
        description='Run if journals data is older than 60 minutes'
    )
    
    # Create outdated checker using trigger name
    journals_outdated_check = create_data_outdated_checker('journals_outdated_trigger')
    
    # Add process
    def create_journals_wrapper(**context):
        org = context.get('organisation', organisation)
        return XeroJournalsSource.objects.create_journals_from_xero(org)
    
    tree.add_function('create_journals_wrapper', create_journals_wrapper)
    tree.add_process(
        'getDataFromXero',
        func=create_journals_wrapper,
        dependencies=[],
        outdated_check=journals_outdated_check
    )
    
    # Save tree
    saved_tree = tree.save()
    print(f"Saved process tree: {saved_tree.name}")
    
    # Register with scheduler (runs every 60 minutes, starting at midnight)
    import datetime
    schedule = tree.register_schedule(
        interval_minutes=60,
        start_time=datetime.time(0, 0),
        enabled=True,
        context={'organisation': organisation}
    )
    print(f"Registered schedule: {schedule}")
    print(f"Next run: {schedule.next_run}")
    
    # Create management command
    command_path = tree.create_command()
    print(f"Created management command: {command_path}")
    print(f"Run with: python manage.py {saved_tree.name.lower().replace(' ', '_')}")


def example_trigger_usage():
    """Example: Using triggers to determine if processes should run."""
    from apps.xero.xero_core.models import XeroTenant
    from apps.xero.xero_data.models import XeroJournalsSource
    from apps.xero.xero_sync.models import XeroLastUpdate
    
    # Create process tree
    tree = ProcessTreeInstance('triggered_workflow', overwrite=True, description='Workflow with triggers')
    
    # Get organisation
    organisation = XeroTenant.objects.first()
    if not organisation:
        print("No XeroTenant found. Please create one first.")
        return
    
    # Create an outdated check trigger
    last_update, created = XeroLastUpdate.objects.get_or_create(
        organisation=organisation,
        end_point='journals',
        defaults={'name': f'{organisation.tenant_name}_journals_update'}
    )
    
    trigger = tree.create_trigger(
        name='journals_outdated_trigger',
        trigger_type='outdated_check',
        xero_last_update_id=last_update.id,
        configuration={'max_age_minutes': 60},
        description='Run if journals data is older than 60 minutes'
    )
    print(f"Created trigger: {trigger.name} (ID: {trigger.id})")
    
    # Create a condition trigger
    condition_trigger = tree.create_trigger(
        name='tenant_check_trigger',
        trigger_type='condition',
        configuration={
            'field': 'tenant_id',
            'operator': 'equals',
            'value': organisation.tenant_id
        },
        description='Only run for specific tenant'
    )
    print(f"Created trigger: {condition_trigger.name}")
    
    # Add process function
    def sync_journals(**context):
        org = context.get('organisation', organisation)
        return XeroJournalsSource.objects.create_journals_from_xero(org)
    
    tree.add_function('sync_journals', sync_journals)
    
    # Add process with trigger
    tree.add_process(
        'sync_journals',
        func=sync_journals,
        dependencies=[],
        trigger='journals_outdated_trigger'  # Use trigger name
    )
    
    # Save tree
    saved_tree = tree.save()
    print(f"\nSaved process tree: {saved_tree.name}")
    print(f"Tree ID: {saved_tree.id}")
    print(f"\nProcess 'sync_journals' will only run if trigger '{trigger.name}' fires")
    print(f"Trigger checks if journals data is older than 60 minutes")


def example_trigger_subscriptions():
    """
    Example: Multiple process trees subscribing to the same trigger using decorator.
    External processes can fire triggers, and all subscribed trees will execute.
    """
    from apps.xero.xero_core.models import XeroTenant
    from apps.xero.xero_sync.process_manager.trigger_utils import (
        fire_trigger,
        subscribe_tree_to_trigger,
        get_trigger_subscriptions,
        reset_trigger
    )
    from apps.xero.xero_sync.process_manager.trigger_decorators import register_to_trigger
    
    # Get organisation
    organisation = XeroTenant.objects.first()
    if not organisation:
        print("No XeroTenant found. Please create one first.")
        return
    
    # Create a shared trigger that external processes can fire
    print("=== Creating Shared Trigger ===")
    from apps.xero.xero_sync.models import Trigger
    
    trigger, created = Trigger.objects.get_or_create(
        name='p&l_report_changed',
        defaults={
            'trigger_type': 'event',
            'description': 'Fired when P&L report changes',
            'enabled': True,
            'configuration': {}
        }
    )
    print(f"Trigger '{trigger.name}' {'created' if created else 'already exists'}")
    
    # Create first process tree using decorator
    print("\n=== Creating First Process Tree (with decorator) ===")
    
    @register_to_trigger('p&l_report_changed')
    def create_pnl_tree_1():
        tree1 = ProcessTreeInstance('p&l_tree_1', overwrite=True, description='First P&L processing tree')
        
        def process_pnl_1(**context):
            print(f"[Tree 1] Processing P&L for {context.get('organisation').tenant_name}...")
            return {'status': 'success', 'tree': 'p&l_tree_1'}
        
        tree1.add_function('process_pnl_1', process_pnl_1)
        tree1.add_process('process_pnl', func=process_pnl_1, dependencies=[])
        return tree1.save()
    
    saved_tree1 = create_pnl_tree_1()
    print(f"Created tree: {saved_tree1.name}")
    
    # Create second process tree using decorator
    print("\n=== Creating Second Process Tree (with decorator) ===")
    
    @register_to_trigger('p&l_report_changed')
    def create_pnl_tree_2():
        tree2 = ProcessTreeInstance('p&l_tree_2', overwrite=True, description='Second P&L processing tree')
        
        def process_pnl_2(**context):
            print(f"[Tree 2] Processing P&L for {context.get('organisation').tenant_name}...")
            return {'status': 'success', 'tree': 'p&l_tree_2'}
        
        tree2.add_function('process_pnl_2', process_pnl_2)
        tree2.add_process('process_pnl', func=process_pnl_2, dependencies=[])
        return tree2.save()
    
    saved_tree2 = create_pnl_tree_2()
    print(f"Created tree: {saved_tree2.name}")
    
    # Verify subscriptions
    print("\n=== Verifying Subscriptions ===")
    subscriptions = get_trigger_subscriptions('p&l_report_changed')
    print(f"Trees subscribed to '{trigger.name}': {subscriptions}")
    
    # External process fires the trigger
    print("\n=== External Process Fires Trigger ===")
    print("Simulating external process (e.g., P&L service) firing trigger...")
    result = fire_trigger(
        'p&l_report_changed',
        context={'organisation': organisation, 'report_id': 123},
        fired_by='p&l_service'
    )
    
    print(f"\nTrigger fire result:")
    print(f"Success: {result.get('success', False)}")
    print(f"Fired by: {result.get('fired_by', 'unknown')}")
    print(f"\nSubscribed tree results:")
    for tree_name, tree_result in result.get('subscribed_trees', {}).items():
        print(f"  - {tree_name}: success={tree_result.get('success', False)}")
    
    # Reset trigger for next time
    print("\n=== Resetting Trigger ===")
    reset_trigger('p&l_report_changed')
    print("Trigger reset to 'pending' state")
    
    # Demonstrate that scheduler can also run processes that change triggers
    print("\n=== Note: Scheduler Integration ===")
    print("The scheduler can run processes that fire triggers.")
    print("For example, a scheduled task could check for P&L changes and fire the trigger.")
    print("All subscribed trees would then execute automatically.")


def example_build_and_save_tree(execute: bool = False):
    """
    Example: Build a process tree and save it to the database.
    
    Args:
        execute: If True, execute the tree after saving (default: False)
    """
    from apps.xero.xero_core.models import XeroTenant
    from apps.xero.xero_data.models import XeroJournalsSource
    from apps.xero.xero_sync.models import XeroLastUpdate
    from apps.xero.xero_sync.process_manager.outdated_checkers import create_data_outdated_checker
    
    # Create ProcessTreeInstance with overwrite=True to allow overwriting existing trees
    # If overwrite=False (default) and tree exists, will raise ValueError
    tree = ProcessTreeInstance('my_workflow', overwrite=True, description='My workflow')
    
    # Get organisation (you would get this from context in real usage)
    # For example purposes, get first tenant
    organisation = XeroTenant.objects.first()
    if not organisation:
        print("No XeroTenant found. Please create one first.")
        return
    
    # Get or create XeroLastUpdate record
    last_update, created = XeroLastUpdate.objects.get_or_create(
        organisation=organisation,
        end_point='journals',
        defaults={'name': f'{organisation.tenant_name}_journals_update'}
    )
    
    # Create trigger for outdated check
    trigger = tree.create_trigger(
        name='journals_outdated_trigger',
        trigger_type='outdated_check',
        xero_last_update_id=last_update.id,
        configuration={'max_age_minutes': 60},
        description='Run if journals data is older than 60 minutes'
    )
    
    # Create data outdated checker using trigger name
    journals_outdated_check = create_data_outdated_checker('journals_outdated_trigger')
    
    # Create a wrapper function that accepts **context and passes organisation
    def create_journals_wrapper(**context):
        """Wrapper function that extracts organisation from context."""
        org = context.get('organisation', organisation)
        return XeroJournalsSource.objects.create_journals_from_xero(org)
    
    # Register the function so it can be resolved when executing
    tree.add_function('create_journals_wrapper', create_journals_wrapper)
    
    # Example of adding a process to the tree with outdated check
    # Process will only run if XeroLastUpdate record shows data is outdated
    tree.add_process(
        'getDataFromXero',
        func=create_journals_wrapper,
        dependencies=[],
        outdated_check=journals_outdated_check,  # Only run if data is outdated
        response_vars={
            'journals_processed': {'type': 'int', 'default': 0, 'key': 'journals_processed'},
            'status': {'type': 'str', 'default': '', 'key': 'status'}
        }
    )
    
    # Save to database first
    saved_tree = tree.save()
    print(f"Saved process tree: {saved_tree.name}")
    print(f"Tree ID: {saved_tree.id}")
    print(f"\nNote: The 'getDataFromXero' process will only execute if XeroLastUpdate")
    print(f"      record '{last_update.name}' (ID: {last_update.id}) shows data is outdated.")
    
    # Optional: Execute the tree with context
    if execute:
        print("\nExecuting process tree...")
        print("Note: This may take a long time if processing many journals...")
        results = tree.execute(context={'organisation': organisation})
        print("\nExecution Results:")
        print(f"Success: {results.get('success', False)}")
        print(f"Results: {results.get('results', {})}")
        print(f"Status: {results.get('status', {})}")
    else:
        print("\nTree saved successfully. Execution skipped.")
        print("To execute this tree manually, run:")
        print(f"  python manage.py run_xero_sync_tree --example execute_by_name")
        print(f"Or use ProcessTreeManager.execute_tree('{saved_tree.name}', context={{'organisation': organisation}})")
        print("\nTo execute during build, use: --execute flag")



    
    # # Define process functions
    # def step1(**context):
    #     print("Executing step 1...")
    #     return {'result': 'step1 done', 'value': 100}
    
    # def step2(step1=None, **context):
    #     print(f"Executing step 2 with step1 value: {step1.get('value') if step1 else None}")
    #     return {'result': 'step2 done', 'value': (step1.get('value') * 2) if step1 else 0}
    
    # def step3(step2=None, **context):
    #     print(f"Executing step 3 with step2 value: {step2.get('value') if step2 else None}")
    #     return {'result': 'step3 done', 'value': (step2.get('value') + 10) if step2 else 0}
    
    # # Add processes incrementally
    # tree.add_process('step1', func=step1, dependencies=[])
    # tree.add_process('step2', func=step2, dependencies=['step1'])
    # tree.add_process('step3', func=step3, dependencies=['step2'])
    
    # # Save to database
    # saved_tree = tree.save()
    # print(f"Saved process tree: {saved_tree.name}")
    # print(f"Tree ID: {saved_tree.id}")



    
    # Define process functions
    # def step1(**context):
    #     print("Executing step 1...")
    #     return {'result': 'step1 done', 'value': 100}
    
    # def step2(step1=None, **context):
    #     print(f"Executing step 2 with step1 value: {step1.get('value') if step1 else None}")
    #     return {'result': 'step2 done', 'value': (step1.get('value') * 2) if step1 else 0}
    
    # def step3(step2=None, **context):
    #     print(f"Executing step 3 with step2 value: {step2.get('value') if step2 else None}")
    #     return {'result': 'step3 done', 'value': (step2.get('value') + 10) if step2 else 0}
    
    # # Build process tree
    # builder = ProcessTreeBuilder(
    #     name='my_workflow',
    #     description='Example workflow with three steps',
    #     cache_enabled=True
    # )
    
    # # Add processes using method chaining
    # builder.add(
    #     'step1',
    #     func=step1,
    #     dependencies=[],
    #     cache_key='step1_cache',
    #     cache_ttl=3600,
    #     required=True,
    #     response_vars={
    #         'value': {'type': 'int', 'default': 0, 'key': 'value'}
    #     }
    # ).add(
    #     'step2',
    #     func=step2,
    #     dependencies=['step1'],
    #     cache_key='step2_cache',
    #     cache_ttl=1800,
    #     required=True,
    #     response_vars={
    #         'value': {'type': 'int', 'default': 0, 'key': 'value'}
    #     }
    # ).add(
    #     'step3',
    #     func=step3,
    #     dependencies=['step2'],
    #     cache_key='step3_cache',
    #     cache_ttl=900,
    #     required=True,
    #     response_vars={
    #         'value': {'type': 'int', 'default': 0, 'key': 'value'}
    #     }
    # )
    
    # # Save to database
    # tree = builder.save()
    # print(f"Saved process tree: {tree.name}")
    # print(f"Tree ID: {tree.id}")


def example_execute_by_name():
    """Example: Execute a process tree by name."""
    
    # Create function registry (maps function references to actual functions)
    func_registry = {
        'step1': lambda **ctx: {'result': 'step1 done', 'value': 100},
        'step2': lambda step1=None, **ctx: {'result': 'step2 done', 'value': (step1.get('value') * 2) if step1 else 0},
        'step3': lambda step2=None, **ctx: {'result': 'step3 done', 'value': (step2.get('value') + 10) if step2 else 0},
    }
    
    # Execute tree by name
    results = ProcessTreeManager.execute_tree(
        'my_workflow',
        context={'tenant_id': '123'},
        func_registry=func_registry
    )
    
    print(f"Execution successful: {results['success']}")
    print(f"Results: {results['results']}")


def example_xero_sync_tree():
    """Example: Build and save Xero sync tree."""
    
    tenant_id = 'your-tenant-id-here'
    
    # Build Xero sync tree
    builder = build_xero_sync_tree(tenant_id)
    
    # Save to database
    tree = builder.save()
    print(f"Saved Xero sync tree: {tree.name}")
    
    # Create function registry with Xero functions
    from apps.xero.xero_metadata.services import update_metadata
    from apps.xero.xero_core.services import XeroApiClient, XeroAccountingApi
    from apps.xero.xero_auth.models import XeroClientCredentials
    from apps.xero.xero_cube.services import process_xero_data, process_profit_loss
    
    credentials = XeroClientCredentials.objects.filter(active=True).first()
    user = credentials.user if credentials else None
    
    def fetch_metadata(**context):
        return update_metadata(tenant_id, user=user)
    
    def fetch_journals(**context):
        """Fetch manual journals (Journals API removed)."""
        api_client = XeroApiClient(user, tenant_id=tenant_id)
        xero_api = XeroAccountingApi(api_client, tenant_id)
        xero_api.manual_journals(load_all=False).get()
        return {'status': 'success', 'endpoint': 'manual_journals'}
    
    def fetch_manual_journals(**context):
        api_client = XeroApiClient(user, tenant_id=tenant_id)
        xero_api = XeroAccountingApi(api_client, tenant_id)
        xero_api.manual_journals(load_all=False).get()
        return {'status': 'success', 'endpoint': 'manual_journals'}
    
    func_registry = {
        'fetch_metadata': fetch_metadata,
        'fetch_journals': fetch_journals,
        'fetch_manual_journals': fetch_manual_journals,
        'process_data': lambda **ctx: process_xero_data(tenant_id),
        'process_pnl': lambda **ctx: process_profit_loss(tenant_id, user=user),
    }
    
    # Execute with sync check
    results = ProcessTreeManager.execute_tree(
        tree.name,
        context={'tenant_id': tenant_id},
        func_registry=func_registry,
        sync_check_func=lambda **ctx: check_xero_sync_status(tenant_id, **ctx),
        only_run_out_of_sync=True
    )
    
    print(f"Sync check: {results['sync_check']}")
    print(f"Execution success: {results['execution']['success']}")


def example_dependent_trees():
    """Example: Create dependent trees that run sequentially."""
    
    # Build main tree
    builder1 = ProcessTreeBuilder('main_workflow', 'Main workflow')
    builder1.add('process_a', func=lambda **ctx: {'result': 'A'}, dependencies=[])
    main_tree = builder1.save()
    
    # Build dependent tree
    builder2 = ProcessTreeBuilder('dependent_workflow', 'Runs after main')
    builder2.add('process_b', func=lambda **ctx: {'result': 'B'}, dependencies=[])
    dep_tree = builder2.save()
    
    # Link dependent tree
    main_tree.add_dependent_tree('dependent_workflow')
    
    # Execute main tree - dependent tree will run automatically
    func_registry = {
        'process_a': lambda **ctx: {'result': 'A'},
        'process_b': lambda **ctx: {'result': 'B'},
    }
    
    results = ProcessTreeManager.execute_with_dependents(
        'main_workflow',
        context={},
        func_registry=func_registry
    )
    
    print(f"Trees executed: {results['trees_executed']}")
    print(f"All successful: {results['success']}")


def example_sibling_trees():
    """Example: Create sibling trees that run in parallel."""
    
    # Build tree 1
    builder1 = ProcessTreeBuilder('workflow_1', 'First parallel workflow')
    builder1.add('process_1', func=lambda **ctx: {'result': '1'}, dependencies=[])
    tree1 = builder1.save()
    
    # Build tree 2
    builder2 = ProcessTreeBuilder('workflow_2', 'Second parallel workflow')
    builder2.add('process_2', func=lambda **ctx: {'result': '2'}, dependencies=[])
    tree2 = builder2.save()
    
    # Link as siblings
    tree1.add_sibling_tree('workflow_2')
    
    # Execute with siblings - both run in parallel
    func_registry = {
        'process_1': lambda **ctx: {'result': '1'},
        'process_2': lambda **ctx: {'result': '2'},
    }
    
    results = ProcessTreeManager.execute_with_siblings(
        'workflow_1',
        context={},
        func_registry=func_registry
    )
    
    print(f"Sibling trees: {results['sibling_trees']}")
    print(f"All successful: {results['success']}")

def example_create_instance_from_stored_tree():
    """Example: Create ProcessManagerInstance from stored tree."""
    
    # Get stored tree and create instance
    instance = ProcessTreeManager.create_instance('my_workflow')
    
    # Execute using instance
    results = instance.execute_tree('my_workflow', context={'tenant_id': '123'})
    
    # Access attributes
    print(f"Step1 value: {instance.step1_value}")
    print(f"Step2 value: {instance.step2_value}")
    print(f"Step3 value: {instance.step3_value}")

def create_process_tree():
    print("=" * 70)
    print("ProcessTreeBuilder and ProcessTreeManager Examples")
    print("=" * 70)
    
    print("\n1. Build and Save Tree:")
    print("-" * 70)
    print("""
    builder = ProcessTreeBuilder('my_workflow', 'Description')
    builder.add('step1', func=my_func, dependencies=[])
    builder.add('step2', func=my_func2, dependencies=['step1'])
    tree = builder.save()  # Saves to database
    """)
    
    print("\n2. Execute by Name:")
    print("-" * 70)
    print("""
    results = ProcessTreeManager.execute_tree(
        'my_workflow',
        context={'tenant_id': '123'},
        func_registry={'step1': my_func, 'step2': my_func2}
    )
    """)
    
    print("\n3. With Sync Check:")
    print("-" * 70)
    print("""
    results = ProcessTreeManager.execute_tree(
        'xero_sync_tenant_id',
        sync_check_func=lambda **ctx: check_xero_sync_status(tenant_id, **ctx),
        only_run_out_of_sync=True
    )
    """)
    
    print("\n4. Dependent Trees:")
    print("-" * 70)
    print("""
    main_tree.add_dependent_tree('dependent_workflow')
    results = ProcessTreeManager.execute_with_dependents('main_workflow')
    """)
    
    print("\n5. Sibling Trees (Parallel):")
    print("-" * 70)
    print("""
    tree1.add_sibling_tree('workflow_2')
    results = ProcessTreeManager.execute_with_siblings('workflow_1')
    """)
    
    print("\n" + "=" * 70)
    print("Uncomment example functions above to see full working examples")
    print("=" * 70)


if __name__ == '__main__':
    """
    To run this script directly, Django must be initialized first.
    
    Recommended ways to run:
    1. Via management command: python manage.py example_command --run-example
    2. Via Django shell: python manage.py shell
       Then: from apps.xero.xero_sync.process_manager.examples import example_xero_sync_tree
             example_xero_sync_tree()
    3. Directly (this will setup Django automatically)
    """
    import os
    import sys
    import django
    
    # Get the project root (where manage.py is located)
    # From: apps/xero/xero_sync/process_manager/examples.py
    # To: project root (5 levels up)
    current_file = os.path.abspath(__file__)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(current_file)))))
    
    # Add project root to Python path
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    
    # Set Django settings module
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'klikk_business_intelligence.settings')
    
    # Setup Django
    django.setup()
    
    # Now you can run example functions
    print("=" * 70)
    print("Django initialized successfully!")
    print("=" * 70)
    print("\nAvailable example functions:")
    print("  - example_build_and_save_tree()")
    print("  - example_execute_by_name()")
    print("  - example_xero_sync_tree()")
    print("  - example_dependent_trees()")
    print("  - example_sibling_trees()")
    print("  - example_create_instance_from_stored_tree()")
    print("\nTo run an example, uncomment the function call below or import it:")
    print("  from apps.xero.xero_sync.process_manager.examples import example_xero_sync_tree")
    print("  example_xero_sync_tree()")
    print("\nOr use the management command:")
    print("  python manage.py example_command --run-example")
    print("=" * 70)
    
    # Uncomment one of these to run an example:
    # example_xero_sync_tree()
    # example_build_and_save_tree()
    # example_execute_by_name()
