"""Shared retrieval transform base (used by ``azure_ai_search``).

Plugins are accessed via PluginManager, not direct imports. This package
imports nothing eagerly: plugin discovery executes each module file under its
own name, and a package import that pulled in a plugin module would load a
second copy of it while discovery is still binding the first.
"""
