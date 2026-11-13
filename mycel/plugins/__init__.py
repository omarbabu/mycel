"""Plugins: tools the model can ask to use. They always run on the *user's* node, never on
the remote node that generated the tool call, and side-effecting tools need the user's
approval for each call unless the user chooses otherwise."""
from .manager import PluginManager, PluginError

__all__ = ["PluginManager", "PluginError"]
