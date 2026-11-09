"""RAG retrieval transform plugin.

Plugins are accessed via PluginManager, not direct imports. This package
imports nothing eagerly: plugin discovery executes each module file under its
own name, and a package import that pulled in ``transform`` would load a
second copy of ``config`` while discovery is still binding the first.
"""
