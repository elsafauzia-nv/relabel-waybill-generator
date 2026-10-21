"""Terminal prompts: country, vendor and tracking ID, all asked before the browser opens."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

from errors import Failure, RunError

# Vendors the relabel tool offers per country (cross-border-relabel-tool page bundle:
# {ph: [FLASH, JNT], th: [FLASH], my: [JNT]}); this script supports PH and MY.
COUNTRY_VENDORS: dict[str, tuple[str, ...]] = {"PH": ("JNT", "FLASH"), "MY": ("JNT",)}
_TRACKING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{4,63}$")
_QUIT = {"q", "quit", "exit"}


@dataclass(frozen=True)
class RunInputs:
    country: str  # "PH" | "MY"
    vendor: str  # "JNT" | "FLASH"
    tracking_id: str


def validate_combination(country: str, vendor: str) -> None:
    if country not in COUNTRY_VENDORS:
        raise RunError(Failure.INPUT, f"country {country} is not supported; choose {' or '.join(COUNTRY_VENDORS)}")
    if vendor not in COUNTRY_VENDORS[country]:
        allowed = " or ".join(COUNTRY_VENDORS[country])
        raise RunError(Failure.INPUT, f"vendor {vendor} is not available for {country}; choose {allowed}")


def validate_tracking_id(tracking_id: str) -> str:
    tracking_id = tracking_id.strip()
    if not _TRACKING_ID.match(tracking_id):
        raise RunError(
            Failure.INPUT,
            f"{tracking_id!r} does not look like a tracking ID (5-64 letters, digits, '-' or '_', no spaces)",
        )
    return tracking_id


def _ask(prompt: str) -> str:
    answer = input(prompt).strip()
    if answer.lower() in _QUIT:
        raise KeyboardInterrupt
    return answer


def _choose(question: str, options: tuple[str, ...]) -> str:
    print(f"\n{question}")
    for i, option in enumerate(options, 1):
        print(f"  {i}. {option}")
    while True:
        answer = _ask(f"Enter 1-{len(options)} or a name (q to quit)> ").upper()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        if answer in options:
            return answer
        print(f"  Please enter 1-{len(options)} or one of: {', '.join(options)}")


def collect_inputs(country: str | None, vendor: str | None, tracking_id: str | None) -> RunInputs:
    """Ask for whatever was not given on the command line, validating as we go."""
    interactive = sys.stdin.isatty()
    if not interactive and not (country and vendor and tracking_id):
        raise RunError(Failure.INPUT, "not running in a terminal: pass --country, --vendor and --tid")

    country = country.upper() if country else _choose("Which country do you want to test?", tuple(COUNTRY_VENDORS))
    if country not in COUNTRY_VENDORS:
        raise RunError(Failure.INPUT, f"country {country} is not supported; choose {' or '.join(COUNTRY_VENDORS)}")

    vendors = COUNTRY_VENDORS[country]
    if vendor:
        vendor = vendor.upper()
    elif len(vendors) == 1:
        vendor = vendors[0]
        print(f"\nVendor: {vendor} (the only vendor for {country})")
    else:
        vendor = _choose(f"Which vendor do you want to use for {country}?", vendors)
    validate_combination(country, vendor)

    while not tracking_id:
        candidate = _ask("\nEnter the tracking ID to scan (q to quit)> ")
        try:
            tracking_id = validate_tracking_id(candidate)
        except RunError as e:
            print(f"  {e.message}")
    return RunInputs(country=country, vendor=vendor, tracking_id=validate_tracking_id(tracking_id))


@dataclass(frozen=True)
class DuplicatePlan:
    inputs: RunInputs  # what this run scans (the new tracking ID when replacing)
    replaces: str | None  # tracking ID whose record is replaced on success
    summary: str


def resolve_duplicate(
    inputs: RunInputs,
    existing: list[str],
    is_taken,
    replace_with: str | None,
    regenerate: bool,
    no_print: bool,
) -> DuplicatePlan:
    """Decide what to do when ``inputs.tracking_id`` already has a waybill (``existing`` describes it)."""
    if not existing:
        return DuplicatePlan(inputs, None, "no duplicate")
    tid = inputs.tracking_id
    print(f"\nDuplicate: {tid} already has a waybill in the managed output:")
    for line in existing:
        print(f"  - {line}")

    if replace_with:
        choice = "1"
    elif regenerate:
        choice = "2"
    elif not sys.stdin.isatty():
        raise RunError(Failure.DUPLICATE, f"{tid} already has a waybill", "pass --replace-with <NEW TID> or --force")
    else:
        again = "scans it again" + ("" if no_print else " and PRINTS another physical label")
        print("\nWhat do you want to do?")
        print("  1. Replace it with a new tracking ID")
        print(f"  2. Generate {tid} again ({again})")
        choice = ""
        while choice not in ("1", "2"):
            choice = _ask("Enter 1 or 2 (q to quit)> ")

    if choice == "2":
        return DuplicatePlan(inputs, None, f"regenerating {tid}; its previous files are replaced once the new waybill is valid")

    while True:
        candidate = replace_with or _ask("\nEnter the new tracking ID that replaces it (q to quit)> ")
        try:
            new_tid = validate_tracking_id(candidate)
            if new_tid.upper() == tid.upper():
                raise RunError(Failure.INPUT, "the new tracking ID is the same as the old one")
            if is_taken(new_tid):
                raise RunError(Failure.INPUT, f"{new_tid} already has its own waybill; choose another tracking ID")
        except RunError as e:
            if replace_with:
                raise
            print(f"  {e.message}")
            continue
        new_inputs = RunInputs(inputs.country, inputs.vendor, new_tid)
        return DuplicatePlan(new_inputs, tid, f"replacing {tid} with {new_tid} once {new_tid}'s waybill is valid")


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return _ask(f"{question} [y/N] ").lower() in ("y", "yes")
