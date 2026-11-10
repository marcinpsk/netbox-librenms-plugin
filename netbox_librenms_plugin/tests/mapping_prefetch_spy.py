"""
A spy on the mapping read path: it proves that a collection producer prefetches before it reads.

The custom-field storage loads with the object, so a missing ``prefetch_mappings`` call changes no
query count today. The spy wraps the real ``read_mapping`` and ``prefetch_mappings`` (both still
run) in every plugin module that imported them. A read of a saved instance in the watched scope is
a violation when that instance was not passed to ``prefetch_mappings`` before the read. The scope
is the named producers, or the whole block when no producer is named.
"""

import functools
import importlib
import pkgutil
import sys
import traceback
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from unittest import mock

from netbox_librenms_plugin import server_mappings

_PLUGIN = "netbox_librenms_plugin"
WHOLE_BLOCK = "<block>"


@dataclass
class MappingReadSpy:
    """The reads and prefetches seen while the spy is active."""

    prefetched: dict = field(default_factory=dict)
    prefetch_calls: int = 0
    reads: list = field(default_factory=list)
    scopes: list = field(default_factory=list)

    def saved_reads(self):
        """Return the saved instances read in the scope."""
        return [obj for obj, _scope, _site in self.reads if obj.pk is not None]

    def reading_scopes(self):
        """Return the names of the producers in which a saved instance was read."""
        return {scope for obj, scope, _site in self.reads if obj.pk is not None}

    def unprefetched(self, *, single=(), models=None):
        """
        Return one line per violation: the instance and the call site of its read.

        *single* names the objects the scope reads one at a time, such as the page object; a read
        of their rows is no violation. With *models*, only reads of those models count.
        """
        allowed = {(type(obj), obj.pk) for obj in single}
        return [
            f"{type(obj).__name__} pk={obj.pk} read in {scope} at {site}"
            for obj, scope, site in self.reads
            if site is not None and (type(obj), obj.pk) not in allowed and (models is None or isinstance(obj, models))
        ]


def _plugin_modules():
    # A module imported while the spy runs would bind the spy's wrapper for good, so import every module first.
    import netbox_librenms_plugin

    for info in pkgutil.walk_packages(netbox_librenms_plugin.__path__, f"{_PLUGIN}."):
        if ".tests" not in info.name and ".migrations" not in info.name:
            importlib.import_module(info.name)
    return [
        module
        for name, module in list(sys.modules.items())
        if module is not None and (name == _PLUGIN or name.startswith(f"{_PLUGIN}.")) and ".tests" not in name
    ]


def _patch_everywhere(stack, original, replacement):
    """Replace *original* in every plugin module and class namespace that binds it."""
    patched = 0
    for module in _plugin_modules():
        for name, value in list(vars(module).items()):
            if value is original:
                stack.enter_context(mock.patch.object(module, name, replacement))
                patched += 1
            elif isinstance(value, type) and value.__module__ == module.__name__:
                for attr, member in list(vars(value).items()):
                    target = member.__func__ if isinstance(member, staticmethod) else member
                    if target is original:
                        wrapped = staticmethod(replacement) if isinstance(member, staticmethod) else replacement
                        stack.enter_context(mock.patch.object(value, attr, wrapped))
                        patched += 1
    if not patched:
        raise AssertionError(f"The spy found no binding of {original.__qualname__}.")


def _read_site():
    frames = traceback.extract_stack(limit=6)[:-3]
    return " <- ".join(
        f"{frame.name} ({frame.filename.rsplit('/', 1)[-1]}:{frame.lineno})" for frame in reversed(frames)
    )


@contextmanager
def spy_mapping_reads(*producers):
    """
    Record the mapping reads and prefetches while the block runs.

    Each producer is a ``(owner, name)`` pair: a plugin module or class and the function or method
    it defines. The block yields a :class:`MappingReadSpy`.
    """
    spy = MappingReadSpy(scopes=[] if producers else [WHOLE_BLOCK])
    real_read = server_mappings.read_mapping
    real_prefetch = server_mappings.prefetch_mappings

    def read_mapping(obj):
        if spy.scopes:
            # A site is recorded only for a violation: a saved instance that no prefetch loaded.
            violation = obj.pk is not None and id(obj) not in spy.prefetched
            spy.reads.append((obj, spy.scopes[-1], _read_site() if violation else None))
        return real_read(obj)

    def prefetch_mappings(objects):
        result = real_prefetch(objects)
        spy.prefetch_calls += 1
        # The dict keeps each instance alive, so its id() cannot be reused while the spy runs.
        spy.prefetched.update((id(obj), obj) for obj in result)
        return result

    with ExitStack() as stack:
        _patch_everywhere(stack, real_read, read_mapping)
        _patch_everywhere(stack, real_prefetch, prefetch_mappings)
        for owner, name in producers:
            member = vars(owner)[name]
            is_static = isinstance(member, staticmethod)
            original = member.__func__ if is_static else member

            def producer(*args, _original=original, _name=name, **kwargs):
                spy.scopes.append(_name)
                try:
                    return _original(*args, **kwargs)
                finally:
                    spy.scopes.pop()

            functools.update_wrapper(producer, original)
            replacement = staticmethod(producer) if is_static else producer
            if isinstance(owner, type):
                stack.enter_context(mock.patch.object(owner, name, replacement))
            else:
                _patch_everywhere(stack, original, producer)
        yield spy
