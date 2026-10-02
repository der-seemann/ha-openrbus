"""Reviewed, exact register classifications for the global opt-in filters.

Register identities are CANopen index/subindex pairs. Core's device catalog
continues to decide which rows exist on each node; this table only classifies
those projected rows. It intentionally does not use name fragments or infer
members from manufacturer categories.
"""

from openrbus.protocol.canip import ObjectAddress


def _addresses(*values: str) -> frozenset[ObjectAddress]:
    return frozenset(ObjectAddress.parse(value) for value in values)


_COOLING = _addresses(
    "2303:00",
    "234f:00",
    "2350:00",
    "2354:00",
    "2355:00",
    "23a7:00",
    "23b3:00",
    "23b4:00",
    "23cb:00",
    "23cc:00",
    "23d8:00",
    "23d9:00",
    "3011:00",
    "301e:00",
    "301f:00",
    "303c:00",
    "30f3:00",
    "30f4:00",
    "30f5:00",
    "30f9:00",
    "30fa:00",
    "30ff:00",
    "3103:00",
    "3218:00",
    "3219:00",
    "321a:00",
    "321d:00",
    "3411:00",
    "3412:00",
    "341a:00",
    "341b:00",
    "3460:00",
    "3466:00",
    "346b:00",
    "3504:00",
    "370a:00",
    "370e:00",
    "384b:00",
    "384c:00",
    "384d:00",
    "384e:00",
    "430f:00",
    "4321:00",
    "5046:00",
    "5087:00",
    "512e:00",
    "5132:00",
    "513c:00",
    "5140:00",
    "5149:00",
    "530f:00",
    "5724:00",
)

_SCREED = _addresses(
    "344d:00",
    "344e:00",
    "344f:00",
    "3483:00",
    "3484:00",
    "3485:00",
    "3486:00",
    "3487:00",
    "3488:00",
    "3489:00",
    "348a:00",
    "348b:00",
    "348c:00",
    "5447:00",
    "5448:00",
    "5449:00",
    "544a:00",
)

OPTIONAL_REGISTER_FILTERS: dict[ObjectAddress, frozenset[str]] = {
    **{address: frozenset({"cooling"}) for address in _COOLING},
    **{address: frozenset({"screed"}) for address in _SCREED},
}
