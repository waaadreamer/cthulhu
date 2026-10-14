"""Roles and the authority matrix (v3, see docs/design-v3.md).

Two tiers: azathoth (and nyarlathotep) talk to the temple; old_ones talk only to
their galaxy's hub. Every permission decision is made here, by the temple or a
hub, from the sender identity bound to its websocket. Skills never carry authority rules.
"""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    AZATHOTH = "azathoth"
    NYARLATHOTEP = "nyarlathotep"
    OLD_ONE = "old_one"


# Singletons are declared in temple config and kept alive by their galaxy.
GODS = (Role.AZATHOTH, Role.NYARLATHOTEP)

DISPLAY = {
    Role.AZATHOTH: "阿撒托斯",
    Role.NYARLATHOTEP: "奈亚拉托提普",
    Role.OLD_ONE: "旧日支配者",
}

OWNER = "owner"
SYSTEM = "system"      # the temple's and hubs' own notices

# sender -> receivers it may message. Old_ones never reach azathoth by message: only through
# report / finish_task, which the hub forwards (design §3.1).
SEND = {
    OWNER: {Role.AZATHOTH, Role.NYARLATHOTEP, Role.OLD_ONE},
    Role.AZATHOTH: {Role.OLD_ONE, Role.NYARLATHOTEP},
    Role.NYARLATHOTEP: {Role.AZATHOTH},
    Role.OLD_ONE: {Role.OLD_ONE},
}

DEPLOY = {Role.AZATHOTH}                  # start deploys (the owner can too, from the web UI)

# Message priority: 0..URGENT, handled high to low. URGENT interrupts the receiver's running turn.
URGENT = 10
NUDGE = 9                                  # hub nudges, task briefs, continue instructions
PEER_MAX = 8                               # old_one -> old_one
# Direct deliveries to azathoth (design §3.4); checkpoint progress goes into the 10-minute digest.
FINISH_PRIORITY = 8
BLOCKER_PRIORITY = 8
REPORT_PRIORITY = 5
DIGEST_PRIORITY = 6
OWNER_PRIORITY = 9

# Rate limits enforced by the hub, two independent buckets per old_one (design §3.3).
PEER_INTERVAL = 600        # per (sender, receiver) pair
REPORT_INTERVAL = 600      # per old_one, report() to azathoth


def can_send(sender: str, receiver: str) -> bool:
    return receiver in SEND.get(sender, set())


class Denied(Exception):
    pass


def require_send(sender: str, receiver: str) -> None:
    if not can_send(sender, receiver):
        raise Denied(f"{sender} may not message {receiver}")


def require_priority(sender: str | None, priority: int) -> None:
    if not 0 <= priority <= URGENT:
        raise Denied(f"priority must be between 0 and {URGENT}")
    if sender == Role.OLD_ONE and priority > PEER_MAX:
        raise Denied(f"old_ones may send at most priority {PEER_MAX}")
