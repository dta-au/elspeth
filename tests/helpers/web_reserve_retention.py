"""Finite, non-recursive retention evaluation for the reserve denial proof.

The graph is static. Final components are cached; an unfinished component
never publishes a partial answer. Origin sets preserve cycle-cut alternatives
instead of enumerating the paths that produced them.
"""

from __future__ import annotations

from collections import ChainMap, deque
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping
from typing import NamedTuple

type Origin = tuple[str, str] | None


class RetentionState(NamedTuple):
    origins: frozenset[Origin]
    retained: bool
    acyclic: bool


class RetentionGraph[Key: Hashable]:
    def __init__(
        self,
        dependencies: Callable[[Key], Iterable[Key]],
        transfer: Callable[[Key, Mapping[Key, RetentionState], bool], RetentionState],
        *,
        reverse: bool = False,
    ) -> None:
        self.dependencies = dependencies
        self.transfer = transfer
        self.reverse = reverse
        self.final: dict[Key, RetentionState] = {}
        self.edges: dict[Key, tuple[Key, ...]] = {}
        self.evaluations = 0

    def evaluate(self, root: Key, charge: Callable[[], None]) -> RetentionState:
        if root in self.final:
            return self.final[root]

        # Iterative Tarjan. Edges point from a value to the dependencies it
        # reads, so emitted components already have final outside inputs.
        indices: dict[Key, int] = {}
        low: dict[Key, int] = {}
        members: list[Key] = []
        active: set[Key] = set()

        def enter(key: Key) -> tuple[Key, Iterator[Key]]:
            indices[key] = low[key] = len(indices)
            members.append(key)
            active.add(key)
            if key not in self.edges:
                self.edges[key] = tuple(dict.fromkeys(self.dependencies(key)))
            return key, iter(self.edges[key])

        frames = [enter(root)]
        while frames:
            key, children = frames[-1]
            try:
                child = next(children)
            except StopIteration:
                pass
            else:
                if child in self.final:
                    continue
                if child not in indices:
                    frames.append(enter(child))
                elif child in active:
                    low[key] = min(low[key], indices[child])
                continue
            frames.pop()
            if frames:
                parent = frames[-1][0]
                low[parent] = min(low[parent], low[key])
            if low[key] != indices[key]:
                continue
            component = []
            while True:
                member = members.pop()
                active.remove(member)
                component.append(member)
                if member == key:
                    break
            cyclic = len(component) > 1 or key in self.edges[key]
            acyclic = not cyclic and all(self.final[child].acyclic for child in self.edges[key])
            if not cyclic:
                charge()
                self.evaluations += 1
                result = self.transfer(key, self.final, acyclic)
                self.final[key] = result
                continue

            local = {member: RetentionState(frozenset({None}), False, False) for member in component}
            # Only local values are mutable. Publishing the complete component
            # below is the commit point, including when charge() raises.
            values = ChainMap(local, self.final)
            readers: dict[Key, list[Key]] = {member: [] for member in component}
            for reader in component:
                for dependency in self.edges[reader]:
                    if dependency in readers:
                        readers[dependency].append(reader)
            order = list(reversed(component)) if self.reverse else component
            pending = deque(order)
            queued = set(order)
            while pending:
                member = pending.popleft()
                queued.remove(member)
                charge()
                self.evaluations += 1
                result = self.transfer(member, values, False)
                previous = values[member]
                joined = RetentionState(previous.origins | result.origins, previous.retained or result.retained, False)
                if joined == previous:
                    continue
                values[member] = joined
                dependents = reversed(readers[member]) if self.reverse else readers[member]
                for reader in dependents:
                    if reader not in queued:
                        pending.append(reader)
                        queued.add(reader)
            self.final.update((member, values[member]) for member in component)
        return self.final[root]


class _OriginChoice(Exception):
    def __init__(self, key: Hashable, origins: frozenset[Origin]) -> None:
        self.key = key
        self.origins = origins


def consumer_combinations(
    consumer: Callable[[Callable[..., Origin]], str | None],
    expressions: Callable[..., frozenset[Origin]],
    charge: Callable[[], None],
) -> tuple[str, ...]:
    """Apply every exemption to complete choices, preserving coupled arguments.

    Lazy branching also follows conditional reads: an input which this branch
    never reads does not multiply its combinations. Repeated reads are one
    factor. Every attempted combination, even a partial one, is charged.
    """
    pending: list[dict[Hashable, Origin]] = [{}]
    reasons: dict[str, None] = {}
    projected: dict[Hashable, frozenset[Origin]] = {}
    while pending:
        choices = pending.pop()

        def selected(value: object, use: object, *, read_target: bool = False, choices: Mapping[Hashable, Origin] = choices) -> Origin:
            key = (value, use, read_target)
            if key in choices:
                return choices[key]
            if key not in projected:
                projected[key] = expressions(value, use, read_target=read_target)
            origins = projected[key]
            if len(origins) == 1:
                return next(iter(origins))
            raise _OriginChoice(key, origins)

        charge()
        try:
            reason = consumer(selected)
        except _OriginChoice as choice:
            for origin in sorted(choice.origins, key=lambda origin: (origin is not None, origin or ("", "")), reverse=True):
                pending.append(choices | {choice.key: origin})
        else:
            if reason is not None:
                reasons[reason] = None
    return tuple(reasons)
