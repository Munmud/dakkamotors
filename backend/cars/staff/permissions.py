"""What each staff group may do.

Replaces `ensure_inventory_group`, and keeps its discipline: the list is written out in
full rather than derived from an app label, so what a role can do is readable in one
place instead of reconstructed from Django's permission naming.

**This is stronger than the arrangement it replaces.** The reconcile-on-every-deploy step
existed because permissions could drift -- somebody could tick a box in the admin and
grant something. There is no UI that can grant a permission now, so drift is impossible
by construction: this module is the only place a role is defined, and changing one means
changing code and passing review.

"""

from ..cognito import INVENTORY_GROUP, OWNERS_GROUP

#: Group -> the actions its members may take. An owner may do anything.
GROUP_ACTIONS = {
    OWNERS_GROUP: {"*"},
    INVENTORY_GROUP: {
        "car.view", "car.add", "car.change", "car.delete",
        "image.view", "image.add", "image.change", "image.delete",
        "schedule.view", "schedule.change",
        "slot.view", "slot.change",
        # Change and view only. Deleting a booking would erase the record of an
        # appointment somebody was told about; cancelling is the reversible verb.
        "booking.view", "booking.change",
        "question.view", "question.add", "question.change", "question.delete",
        "customer.view",
        # Staff administration is deliberately *not* here. See below.
    },
}

#: Actions only an owner may take, whatever else a group is given.
#
# The reasoning is the one `StaffAccountAdmin` spelled out: handing somebody the ability
# to edit staff accounts is a complete privilege escalation. They could open the owner's
# account, reset its password, or add themselves to `owners`. That is not a bug in the
# permission, it is what the permission means - so it belongs to owners alone.
OWNER_ONLY = {
    "staff.view", "staff.add", "staff.change", "staff.delete",
    "group.change",
}

#: Groups a non-owner may put somebody into. Anything more powerful added later is
#: invisible here, so it cannot be handed out by somebody who should not have it.
ASSIGNABLE_GROUPS = (INVENTORY_GROUP,)


def actions_for(groups):
    """Everything this set of groups may do."""
    allowed = set()
    for group in groups:
        allowed |= GROUP_ACTIONS.get(group, set())
    return allowed


def may(groups, action):
    if action in OWNER_ONLY:
        return OWNERS_GROUP in groups
    allowed = actions_for(groups)
    return "*" in allowed or action in allowed
