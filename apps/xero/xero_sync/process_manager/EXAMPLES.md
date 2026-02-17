# ProcessTreeBuilder and ProcessTreeManager Examples

This document provides examples of how to use `ProcessTreeBuilder` and `ProcessTreeManager` to build, store, and execute process trees.

## Table of Contents

1. [Basic Usage](#basic-usage)
2. [ProcessTreeInstance (Recommended)](#processtreeinstance-recommended)
3. [Build and Save Tree](#build-and-save-tree)
4. [Execute by Name](#execute-by-name)
5. [Xero Sync Tree](#xero-sync-tree)
6. [Dependent Trees](#dependent-trees)
7. [Sibling Trees](#sibling-trees)
8. [Running Examples](#running-examples)

---

## Basic Usage

### Import Required Classes

```python
from apps.xero.xero_sync.process_manager.tree_builder import (
    ProcessTreeBuilder, 
    ProcessTreeManager,
    ProcessTreeInstance  # Recommended for incremental building
)
from apps.xero.xero_sync.process_manager.xero_builder import build_xero_sync_tree
from apps.xero.xero_sync.process_manager.xero import check_xero_sync_status
```

---

## ProcessTreeInstance (Recommended)

**ProcessTreeInstance** is the recommended way to build process trees incrementally. You can:
- Create an instance and add processes as you go
- Pass the instance around to different parts of your code
- Add validation functions and other configuration dynamically
- Save to database when ready

### Basic Example

```python
from apps.xero.xero_sync.process_manager.tree_builder import ProcessTreeInstance

# Create an instance
tree = ProcessTreeInstance('my_workflow', description='My custom workflow')

# Add processes incrementally
tree.add_process('step1', func=my_step1_func)
tree.add_process('step2', func=my_step2_func, dependencies=['step1'])
tree.add_process('step3', func=my_step3_func, dependencies=['step2'])

# Add validation functions later
def validate_step1(result):
    return result.get('status') == 'success'

tree.add_validation('step1', validate_step1)

# Pass the tree around to other functions
configure_tree(tree)
add_more_processes(tree)

# Save when ready
tree.save()
```

### Passing Tree Around

```python
def configure_tree(tree: ProcessTreeInstance):
    """Configure a tree instance passed in."""
    tree.set_cache_enabled(True)
    tree.add_process('preprocessing', func=preprocess_func)
    return tree

def add_validations(tree: ProcessTreeInstance, validations: dict):
    """Add multiple validations to a tree."""
    for process_name, validation_func in validations.items():
        if tree.has_process(process_name):
            tree.add_validation(process_name, validation_func)
    return tree

# Usage
tree = ProcessTreeInstance('my_tree')
tree.add_process('step1', func=step1_func)

# Pass to configuration function
tree = configure_tree(tree)

# Add validations
validations = {
    'step1': lambda r: r.get('value') > 0,
    'preprocessing': lambda r: 'data' in r
}
tree = add_validations(tree, validations)

# Save
tree.save()
```

### Loading Existing Tree

```python
# Load an existing tree from database
tree = ProcessTreeInstance('existing_tree_name').load('existing_tree_name')

# Modify it
tree.add_process('new_step', func=new_func, dependencies=['step3'])
tree.add_validation('new_step', validate_new_step)

# Save changes
tree.save()
```

### Execute Directly

```python
tree = ProcessTreeInstance('my_tree')
tree.add_process('step1', func=my_func)
tree.add_process('step2', func=my_func2, dependencies=['step1'])

# Execute without saving first (will auto-save)
results = tree.execute(context={'tenant_id': '123'})

# Or save first, then execute
tree.save()
results = tree.execute(context={'tenant_id': '123'})
```

### Complete Example with Validation

```python
def fetch_data(**context):
    return {'data': [1, 2, 3], 'status': 'success'}

def process_data(fetch_data=None, **context):
    if fetch_data:
        return {'processed': len(fetch_data.get('data', []))}
    return {'processed': 0}

def validate_fetch(result):
    return result.get('status') == 'success' and 'data' in result

def validate_process(result):
    return result.get('processed', 0) > 0

# Create and configure tree
tree = ProcessTreeInstance('data_pipeline', description='Data processing pipeline')

# Add processes
tree.add_process('fetch_data', func=fetch_data)
tree.add_process('process_data', func=process_data, dependencies=['fetch_data'])

# Add validations
tree.add_validation('fetch_data', validate_fetch)
tree.add_validation('process_data', validate_process)

# Configure
tree.set_cache_enabled(True)

# Save
tree.save()

# Execute
results = tree.execute(context={'tenant_id': '123'})
print(f"Success: {results['success']}")
```

### Getting Help on Methods

The `add_process`, `add_validation`, and `add_function` methods have built-in help functionality:

```python
tree = ProcessTreeInstance('my_tree')

# Show help for add_process
tree.add_process.help()

# List parameters for add_process
tree.add_process.list_parameters()

# Get signature
print(tree.add_process.signature)

# Get parameters as dict
params = tree.add_process.parameters
print(params)

# Show help for add_validation
tree.add_validation.help()

# Show help for add_function
tree.add_function.help()

# Show all available methods
tree.help()  # Shows all methods
tree.help('add_process')  # Shows help for specific method

# Or use static methods
ProcessTreeInstance.show_add_process_help()
ProcessTreeInstance.show_add_validation_help()
ProcessTreeInstance.show_add_function_help()
ProcessTreeInstance.show_all_methods()
```

**Example Output:**
```
>>> tree.add_process.help()
======================================================================
ProcessTreeInstance.add_process()
======================================================================

Signature:
add_process(process_name: str, func: Callable, dependencies: List[str] = None, cache_key: str = None, cache_ttl: int = None, validation: Callable = None, required: bool = True, metadata: Dict[str, Any] = None, response_vars: Dict[str, Dict[str, Any]] = None) -> 'ProcessTreeInstance'

Documentation:
Add a process to the tree.

Args:
    process_name: Name of the process
    func: Function to execute
    dependencies: List of process names this depends on
    cache_key: Optional cache key
    cache_ttl: Optional cache TTL in seconds
    validation: Optional validation function
    required: Whether process is required (default True)
    metadata: Optional metadata dict
    response_vars: Optional response variable definitions for this process

Returns:
    self (for method chaining)
======================================================================

>>> tree.add_process.list_parameters()
Parameters for add_process():
----------------------------------------------------------------------
  process_name: REQUIRED -> <class 'str'>
  func: REQUIRED -> typing.Callable
  dependencies: OPTIONAL (default: None) -> typing.List[str]
  cache_key: OPTIONAL (default: None) -> <class 'str'>
  cache_ttl: OPTIONAL (default: None) -> <class 'int'>
  validation: OPTIONAL (default: None) -> typing.Callable
  required: OPTIONAL (default: True) -> <class 'bool'>
  metadata: OPTIONAL (default: None) -> typing.Dict[str, typing.Any]
  response_vars: OPTIONAL (default: None) -> typing.Dict[str, typing.Dict[str, typing.Any]]
----------------------------------------------------------------------
```

---

## Build and Save Tree

Build a process tree programmatically and save it to the database:

```python
def example_build_and_save_tree():
    """Example: Build a process tree and save it to the database."""
    
    # Define process functions
    def step1(**context):
        print("Executing step 1...")
        return {'result': 'step1 done', 'value': 100}
    
    def step2(step1=None, **context):
        print(f"Executing step 2 with step1 value: {step1.get('value') if step1 else None}")
        return {'result': 'step2 done', 'value': (step1.get('value') * 2) if step1 else 0}
    
    def step3(step2=None, **context):
        print(f"Executing step 3 with step2 value: {step2.get('value') if step2 else None}")
        return {'result': 'step3 done', 'value': (step2.get('value') + 10) if step2 else 0}
    
    # Build process tree
    builder = ProcessTreeBuilder(
        name='my_workflow',
        description='Example workflow with three steps',
        cache_enabled=True
    )
    
    # Add processes using method chaining
    builder.add(
        'step1',
        func=step1,
        dependencies=[],
        cache_key='step1_cache',
        cache_ttl=3600,
        required=True,
        response_vars={
            'value': {'type': int, 'default': 0, 'key': 'value'}
        }
    ).add(
        'step2',
        func=step2,
        dependencies=['step1'],
        cache_key='step2_cache',
        cache_ttl=1800,
        required=True,
        response_vars={
            'value': {'type': int, 'default': 0, 'key': 'value'}
        }
    ).add(
        'step3',
        func=step3,
        dependencies=['step2'],
        cache_key='step3_cache',
        cache_ttl=900,
        required=True,
        response_vars={
            'value': {'type': int, 'default': 0, 'key': 'value'}
        }
    )
    
    # Save to database
    tree = builder.save()
    print(f"Saved process tree: {tree.name}")
    print(f"Tree ID: {tree.id}")
```

---

## Execute by Name

Execute a stored process tree by its name:

```python
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
```

---

## Xero Sync Tree

Build and execute a Xero synchronization tree:

```python
def example_xero_sync_tree(tenant_id: str):
    """Example: Build and save Xero sync tree."""
    
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
```

---

## Dependent Trees

Create trees that run sequentially (one after another):

```python
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
```

---

## Sibling Trees

Create trees that run in parallel:

```python
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
```

---

## Running Examples

### Option 1: Management Command (Recommended)

```bash
python manage.py run_xero_sync_tree
```

Or with tenant ID:
```bash
python manage.py run_xero_sync_tree --tenant-id your-tenant-id
```

### Option 2: Django Shell

```bash
python manage.py shell
```

Then import and run:
```python
from apps.xero.xero_sync.process_manager.examples import example_xero_sync_tree
example_xero_sync_tree()
```

### Option 3: Direct Script Execution

The `examples.py` file can be run directly (Django will be auto-initialized):

```bash
python apps/xero/xero_sync/process_manager/examples.py
```

### Option 4: Import in Your Code

```python
from apps.xero.xero_sync.process_manager.examples import (
    example_build_and_save_tree,
    example_execute_by_name,
    example_xero_sync_tree,
    example_dependent_trees,
    example_sibling_trees,
)

# Run any example
example_build_and_save_tree()
```

---

## Key Concepts

### ProcessTreeBuilder

- **Purpose**: Build process trees programmatically
- **Methods**:
  - `add()`: Add a process to the tree
  - `build()`: Build the process tree dictionary
  - `save()`: Save the tree to the database

### ProcessTreeManager

- **Purpose**: Manage and execute stored process trees
- **Methods**:
  - `get_tree()`: Get a tree by name
  - `create_instance()`: Create a ProcessManagerInstance from a stored tree
  - `execute_tree()`: Execute a tree by name
  - `execute_with_dependents()`: Execute a tree and its dependent trees
  - `execute_with_siblings()`: Execute a tree and its sibling trees

### Function Registry

When executing stored trees, you need to provide a `func_registry` that maps function references to actual callable functions. This is because functions can't be serialized to JSON, so only references are stored.

---

## Notes

- Process trees are stored in the `ProcessTree` model
- Functions are stored as references (module + name) since they can't be serialized
- The function registry must be provided when executing stored trees
- Dependent trees run sequentially after the main tree completes
- Sibling trees run in parallel (currently simplified - production would use async)

---

## See Also

- `apps/xero/xero_sync/process_manager/tree_builder.py` - Implementation
- `apps/xero/xero_sync/process_manager/xero_builder.py` - Xero-specific tree builder
- `apps/xero/xero_sync/models.py` - ProcessTree model definition

