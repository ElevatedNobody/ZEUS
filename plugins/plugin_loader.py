"""
ZEUS Plugin Loader
Discovers plugin modules in plugins/, validates their declared metadata
and permissions, and registers their tools into the shared tool registry.
Plugins are NOT trusted blindly - each declared tool's risk level is
enforced the same way built-in tools are.
"""

import importlib
import pkgutil
from typing import Dict, List, Set

from core.logger import get_logger
from core.tool_registry import registry

logger = get_logger("plugins")

REQUIRED_FIELDS = {"name", "version", "author", "permissions"}

# Tools each plugin module registered. discover_plugins() is called from more
# than one place at startup; a plugin whose tools are all still registered is
# not registered (and logged) a second time.
_REGISTERED: Dict[str, Set[str]] = {}


def _tool_names() -> Set[str]:
    try:
        return {tool.name for tool in registry.list_tools()}
    except Exception:
        return set()


def discover_plugins(package_name: str = "plugins") -> List[str]:
    loaded = []
    try:
        package = importlib.import_module(package_name)
    except ImportError:
        return loaded

    for _finder, module_name, is_pkg in pkgutil.iter_modules(package.__path__):
        if module_name in ("plugin_loader",) or is_pkg:
            continue
        full_name = f"{package_name}.{module_name}"
        try:
            module = importlib.import_module(full_name)
        except Exception as e:
            logger.error(f"Plugin '{module_name}' failed to import: {e}")
            continue

        info = getattr(module, "PLUGIN_INFO", None)
        if not info:
            continue

        missing = REQUIRED_FIELDS - set(info.keys())
        if missing:
            logger.warning(f"Plugin '{module_name}' missing metadata fields: {missing} - skipped")
            continue

        register_fn = getattr(module, "register", None)
        if callable(register_fn):
            done = _REGISTERED.get(full_name)
            if done and done <= _tool_names():
                loaded.append(info["name"])        # already registered and still there
                continue
            try:
                before = _tool_names()
                register_fn(registry)
                _REGISTERED[full_name] = _tool_names() - before
                loaded.append(info["name"])
                logger.info(f"Loaded plugin: {info['name']} v{info.get('version')}")
            except Exception as e:
                logger.error(f"Plugin '{info['name']}' registration failed: {e}")

    return loaded
