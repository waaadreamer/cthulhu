"""Roles and the authority matrix.

Every permission decision in the harness is made here, by the temple, from the
sender identity bound to its websocket. Skills never carry authority rules.
"""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    AZATHOTH = "azathoth"
    SHUB = "shub"
    YOG = "yog"
    GHROTH = "ghroth"
    NYARLATHOTEP = "nyarlathotep"
    OLD_ONE = "old_one"


# Singletons are declared in temple config and kept alive by their galaxy.
GODS = (Role.AZATHOTH, Role.SHUB, Role.YOG, Role.GHROTH, Role.NYARLATHOTEP)

DISPLAY = {
    Role.AZATHOTH: "阿撒托斯",
    Role.SHUB: "黑山羊",
    Role.YOG: "尤格索托斯",
    Role.GHROTH: "格赫罗斯",
    Role.NYARLATHOTEP: "奈亚拉托提普",
    Role.OLD_ONE: "旧日支配者",
}

OWNER = "owner"

# sender -> receivers it may message. The owner talks only through the web UI
# and may reach azathoth and ghroth.
SEND = {
    OWNER: {Role.AZATHOTH, Role.GHROTH, Role.NYARLATHOTEP},
    Role.AZATHOTH: {Role.SHUB, Role.YOG, Role.GHROTH, Role.NYARLATHOTEP},
    Role.SHUB: {Role.AZATHOTH, Role.OLD_ONE, Role.NYARLATHOTEP},
    Role.YOG: {Role.AZATHOTH, Role.OLD_ONE, Role.SHUB, Role.NYARLATHOTEP},
    Role.GHROTH: {Role.NYARLATHOTEP},
    Role.OLD_ONE: {Role.SHUB, Role.OLD_ONE, Role.NYARLATHOTEP},
    Role.NYARLATHOTEP: {Role.AZATHOTH, Role.SHUB, Role.YOG, Role.GHROTH, Role.OLD_ONE},
}

# Capabilities beyond messaging.
SPAWN = {Role.SHUB}                       # create / stop old_ones
LATEST_RESULT = {Role.YOG}                # remote latest_result calls
ADMIN_READ = {Role.AZATHOTH, Role.SHUB}   # full read access to the temple
DEPLOY = {Role.AZATHOTH}                  # start deploys (the owner can too, from the web UI)
DEPLOY_REQUEST = {Role.GHROTH}            # ask azathoth to deploy
# Agents whose latest_result yog inspects on each patrol.
SUPERVISED = {Role.OLD_ONE, Role.GHROTH, Role.NYARLATHOTEP}


def can_send(sender: str, receiver: str) -> bool:
    return receiver in SEND.get(sender, set())


class Denied(Exception):
    pass


def require_send(sender: str, receiver: str) -> None:
    if not can_send(sender, receiver):
        raise Denied(f"{sender} may not message {receiver}")
