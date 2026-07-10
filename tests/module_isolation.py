import sys


def restore_modules(original_modules):
    """Restore module fakes without clearing Python's process-wide module table."""

    managed_roots = ("frappe", "erpnext", "stripe", "stripe_integration")

    def is_managed(module_name):
        return any(
            module_name == root or module_name.startswith(f"{root}.")
            for root in managed_roots
        )

    for module_name in list(sys.modules):
        if is_managed(module_name) and module_name not in original_modules:
            sys.modules.pop(module_name, None)
    for module_name, module in original_modules.items():
        if is_managed(module_name) and sys.modules.get(module_name) is not module:
            sys.modules[module_name] = module
