"""pytest plugin for verify.py: keep only the node ids listed in $BR_KEEP."""
import os


def pytest_collection_modifyitems(config, items):
    path = os.environ.get("BR_KEEP")
    if not path:
        return
    keep = set(open(path).read().split("\n"))
    selected = [i for i in items if i.nodeid in keep or i.nodeid.split("[")[0] in keep]
    deselected = [i for i in items if i not in selected]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected
