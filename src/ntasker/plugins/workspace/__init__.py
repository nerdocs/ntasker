"""Workspace plugin package.

Holds the filesystem scanners and helpers in :mod:`ntasker.plugins.workspace.scan`.
The plugin itself (page, sidebar sections, settings) is registered here once
it lands; :mod:`ntasker.plugins.task_context` imports the helpers directly and
does not require the workspace plugin to be enabled.
"""
