"""Static classes of a surviving mutant, read off its own `mutmut show` diff.

Only what the tier-2 shortlist needs (`gates.set_aside_kind`): the class a
survivor falls in, decided by fixed rules over the mutant's removed and
added lines, never by running anything, and the fixed phrase the shortlist
prints beside a survivor it sets aside.

Classes (`classify`), in precedence order:

- ``untested``: mutmut decided `no tests` -- no test runs the function.
- ``equivalent``: a known pattern that cannot change behaviour. Only one is
  known: `Decimal(1)` -> `Decimal(2)` used as a quantize exponent (both have
  exponent 0, so `quantize` and `scaleb` give the same result).
- ``text``: the change sits only in an exception's argument, in a message
  format line, or in a `_check_*` field-name argument.
- ``behaviour``: everything else.

The shortlist sets aside only ``equivalent`` and ``text``; ``untested`` and
``behaviour`` survivors always stay on it.
"""

from __future__ import annotations

import re

# The phrase printed beside a survivor set aside in each class. Fixed
# templates: nothing about a mutant is written that is not one of these.
PHRASES = {
    "text": "only message or argument text changes",
    "equivalent": "no behaviour can change (Decimal(1) and Decimal(2) share a quantize exponent)",
}

_NAME = re.compile(r"^(?P<mod>.+?)\.x(?:ǁ(?P<cls>[^ǁ]+)ǁ(?P<meth>.+)|_(?P<func>.+))__mutmut_\d+$")


def function_of(name: str) -> str:
    """`accounts.xǁAccountǁwithdraw__mutmut_3` -> `Account.withdraw`."""
    m = _NAME.match(name)
    if m is None:
        return name
    if m["cls"]:
        return f"{m['cls']}.{m['meth']}"
    return m["func"]


def parse_show(show: str) -> tuple[str, str, str]:
    """(file, before, after) from a `mutmut show` diff; a multi-line hunk joins with one space."""
    path, removed, added = "", [], []
    for line in show.splitlines():
        if line.startswith("--- "):
            path = line[4:].strip()
        elif line.startswith("+++ "):
            continue
        elif line.startswith("-"):
            removed.append(line[1:].strip())
        elif line.startswith("+"):
            added.append(line[1:].strip())
    return path, " ".join(removed), " ".join(added)


def _is_equivalent(before: str, after: str) -> bool:
    if before.replace("Decimal(1)", "Decimal(2)") != after or before == after:
        return False
    # Only where the value is used as a quantize exponent: an argument to
    # `quantize(`/`rounding=`, or a name that says it is one. A bare
    # `return Decimal(1).scaleb(...)` may be used for its value.
    return bool(re.search(r"quantize\(|rounding=|^exponent\w* = ", before))


def _swap_format(before: str) -> str:
    return before.replace(" % ", " / ", 1)


def _is_text(before: str, after: str) -> bool:
    raised = re.match(r"^raise (\w+)\((.*)\)( from \w+)?$", before)
    if raised:
        # The message argument nulled or dropped, its `%` format broken, or
        # only the values it formats changed. Other edits stay behaviour.
        tail = raised[3] or ""
        nulled = (f"raise {raised[1]}(None){tail}", f"raise {raised[1]}(){tail}")
        cut = before.find(" % ")
        return (
            after in nulled
            or after == _swap_format(before) != before
            or (cut > 0 and after[: cut + 3] == before[: cut + 3])
        )
    if re.match(r"""^[rbfu]?["']""", before):
        # A continuation line starting with a string: nulled or format broken.
        # A changed format argument stays behaviour (fail closed): the same
        # shape formats report output, not only exception messages.
        return after == "None" or after == _swap_format(before) != before
    m = re.match(r"^(_?check_\w+)\((.+?),\s*(\"[^\"]*\"|'[^']*')\)$", before)
    return m is not None and after.startswith(f"{m[1]}({m[2]},") and m[3] not in after


def classify(status: str, function: str, before: str, after: str) -> str:
    """The fixed classifier: `untested`, `equivalent`, `text` or `behaviour`."""
    if status == "no tests":
        return "untested"
    if _is_equivalent(before, after):
        return "equivalent"
    if _is_text(before, after):
        return "text"
    return "behaviour"
