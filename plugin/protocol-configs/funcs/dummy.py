"""
Example custom functions for the Protocol Engine.

This file ships with the plugin (plugin/protocol-configs/funcs/) as a
reference. To create your own custom functions:

1. Copy this file (or create a new .py file) to:
   team-management/protocol-configs/custom/funcs/

2. Define public functions that accept a single `args` dict parameter
   and return a dict with at least a `success` key.

3. Reference them in protocol JSON configs as:
   "pre_funcs": ["custom(your_function_name)"]
   "post_funcs": ["custom(your_function_name)"]

Contract:
  - Input:  args (Dict[str, Any] or None) — passed from protocol_advance(args={...})
  - Output: Dict with at least {"success": True/False}
  - Functions starting with _ are ignored (treated as private helpers)
  - Files starting with _ are ignored entirely
  - Import errors in one file don't break other custom func files
"""


def hello_world(args=None):
    """A minimal working example that always succeeds.

    Usage in protocol config:
        "post_funcs": ["custom(hello_world)"]
    """
    return {
        "success": True,
        "message": "Hello from custom function!",
    }


def validate_task_name_prefix(args=None):
    """Validate that the task name uses an allowed prefix.

    Demonstrates a realistic validation function that checks args
    and can return failure with an error message.

    Usage in protocol config:
        "post_funcs": ["custom(validate_task_name_prefix)"]

    Expected args:
        task (str): The task name to validate
    """
    if not args:
        return {"success": False, "error": "No args provided. Expected 'task' key."}

    task = args.get("task", "")
    if not task:
        return {"success": False, "error": "Missing 'task' in args."}

    allowed_prefixes = ("h-", "m-", "l-", "r-", "o-", "b-")
    if not any(task.startswith(p) for p in allowed_prefixes):
        return {
            "success": False,
            "error": f"Task '{task}' must start with one of: {', '.join(allowed_prefixes)}",
        }

    return {
        "success": True,
        "task": task,
        "message": f"Task name '{task}' has a valid prefix.",
    }


def _private_helper():
    """This function is ignored by the custom func loader.

    Functions starting with _ are treated as private helpers and
    will NOT be available as custom(func_name) in protocol configs.
    """
    return "I'm a helper, not a custom func"
