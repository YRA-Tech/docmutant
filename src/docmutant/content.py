"""Content streams as nested units, and delta debugging over them.

A page's content stream is split into units that can be removed without
breaking the stream: a ``q``...``Q`` group, a ``BT``...``ET`` text object, a
marked-content sequence, one path with its painting operator, one ``Do``, one
``sh``, one inline image, or one state operator. Groups and marked content keep
their children, so the reducer can first drop whole groups and then work
inside the ones that remain.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

import pikepdf

Instruction = pikepdf.ContentStreamInstruction | pikepdf.ContentStreamInlineImage

PATH_CONSTRUCTION = frozenset({"m", "l", "c", "v", "y", "h", "re"})
PATH_CLIP = frozenset({"W", "W*"})
PATH_PAINT = frozenset({"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"})

T = TypeVar("T")


@dataclass
class Unit:
    """One removable piece of a content stream."""

    kind: str
    """group, marked, text, path, xobject, shading, inline-image or state."""

    instructions: list[Instruction] = field(default_factory=list)
    """For leaf units: the instructions. For groups: the opening instruction."""

    children: list[Unit] = field(default_factory=list)
    closing: Instruction | None = None

    @property
    def is_container(self) -> bool:
        return self.kind in ("group", "marked")

    def flatten(self) -> list[Instruction]:
        if not self.is_container:
            return list(self.instructions)
        out = list(self.instructions)
        for child in self.children:
            out.extend(child.flatten())
        if self.closing is not None:
            out.append(self.closing)
        return out


def operator_name(instruction: Instruction) -> str:
    if isinstance(instruction, pikepdf.ContentStreamInlineImage):
        return "INLINE IMAGE"
    return str(instruction.operator)


def parse_units(instructions: Sequence[Instruction]) -> list[Unit]:
    """Split instructions into units. Unbalanced streams fall back to flat units."""
    position = 0

    def parse_until(closers: frozenset[str]) -> tuple[list[Unit], Instruction | None]:
        nonlocal position
        units: list[Unit] = []
        pending_path: list[Instruction] = []
        while position < len(instructions):
            instruction = instructions[position]
            op = operator_name(instruction)
            position += 1

            if pending_path:
                if op in PATH_PAINT:
                    pending_path.append(instruction)
                    units.append(Unit("path", pending_path))
                    pending_path = []
                    continue
                if op in PATH_CONSTRUCTION or op in PATH_CLIP:
                    pending_path.append(instruction)
                    continue
                # A path that is never painted: close it and read this operator again.
                units.append(Unit("path", pending_path))
                pending_path = []
                position -= 1
                continue

            if op in closers:
                return units, instruction
            if op == "q":
                children, closing = parse_until(frozenset({"Q"}))
                units.append(Unit("group", [instruction], children, closing))
            elif op in ("BMC", "BDC"):
                children, closing = parse_until(frozenset({"EMC"}))
                units.append(Unit("marked", [instruction], children, closing))
            elif op == "BT":
                text = [instruction]
                while position < len(instructions):
                    text.append(instructions[position])
                    position += 1
                    if operator_name(text[-1]) == "ET":
                        break
                units.append(Unit("text", text))
            elif op in PATH_CONSTRUCTION:
                pending_path = [instruction]
            elif op == "Do":
                units.append(Unit("xobject", [instruction]))
            elif op == "sh":
                units.append(Unit("shading", [instruction]))
            elif op == "INLINE IMAGE":
                units.append(Unit("inline-image", [instruction]))
            else:
                units.append(Unit("state", [instruction]))
        if pending_path:
            units.append(Unit("path", pending_path))
        return units, None

    units, stray = parse_until(frozenset({"Q", "EMC"}))
    if stray is not None or position < len(instructions):
        # A closing operator without its opener: do not guess the structure.
        return [Unit("state", [instruction]) for instruction in instructions]
    return units


def flatten(units: Sequence[Unit]) -> list[Instruction]:
    out: list[Instruction] = []
    for unit in units:
        out.extend(unit.flatten())
    return out


def count_kinds(units: Sequence[Unit]) -> dict[str, int]:
    counts: dict[str, int] = {}

    def visit(unit: Unit) -> None:
        counts[unit.kind] = counts.get(unit.kind, 0) + 1
        for child in unit.children:
            visit(child)

    for unit in units:
        visit(unit)
    return counts


def ddmin(items: Sequence[T], still_fails: Callable[[list[T]], bool]) -> list[T]:
    """Zeller's ddmin: a 1-minimal subsequence of ``items`` for which ``still_fails`` holds.

    The caller guarantees that ``still_fails(list(items))`` is true.
    """
    current = list(items)
    granularity = 2
    while len(current) >= 2:
        size = len(current)
        chunk = max(1, size // granularity)
        chunks = [current[i : i + chunk] for i in range(0, size, chunk)]
        reduced = False

        for part in chunks:
            if still_fails(part):
                current, granularity, reduced = part, 2, True
                break
        if not reduced:
            for index in range(len(chunks)):
                complement = [item for i, c in enumerate(chunks) if i != index for item in c]
                if still_fails(complement):
                    current = complement
                    granularity = max(granularity - 1, 2)
                    reduced = True
                    break
        if not reduced:
            if granularity >= size:
                break
            granularity = min(size, granularity * 2)
    if len(current) == 1 and still_fails([]):
        return []
    return current


def reduce_units(units: list[Unit], still_fails: Callable[[list[Unit]], bool]) -> list[Unit]:
    """Remove units top-down: whole units, then inside each remaining container.

    After a container's children are reduced, the container itself is unwrapped
    (replaced by its children) if the oracle still fires without it.
    """
    units = ddmin(units, still_fails)
    index = 0
    while index < len(units):
        unit = units[index]
        if not unit.is_container:
            index += 1
            continue

        def with_children(
            children: list[Unit], index: int = index, unit: Unit = unit, units: list[Unit] = units
        ) -> list[Unit]:
            replaced = Unit(unit.kind, unit.instructions, children, unit.closing)
            return [*units[:index], replaced, *units[index + 1 :]]

        def nested_fails(children: list[Unit], with_children=with_children) -> bool:
            return still_fails(with_children(children))

        kept_children = reduce_units(list(unit.children), nested_fails) if unit.children else []
        unwrapped = [*units[:index], *kept_children, *units[index + 1 :]]
        if still_fails(unwrapped):
            units = unwrapped
            index += len(kept_children)
        else:
            units[index] = Unit(unit.kind, unit.instructions, kept_children, unit.closing)
            index += 1
    return units
