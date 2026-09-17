"""The recurring weekly availability rules.

"Every Friday 18:30-19:00, 2 people" is one item. It describes intent; the slots it
produces are what actually get booked.

`unique_test_drive_rule` becomes a guard item rather than the schedule's own key. The
natural key is tempting -- it would make uniqueness free -- but editing a rule's time
would then change its identity, and a slot's id embeds its schedule's, so every future
slot would be orphaned by a time correction.
"""

from pynamodb.exceptions import PutError

from . import keys
from .errors import ConditionFailed, NotFound
from .models import RuleGuard, Schedule
from .txn import Txn, failed

SCHEDULE, GUARD = "schedule", "guard"


def _is_conditional_failure(exc):
    cause = getattr(exc, "cause", None)
    code = (cause.response.get("Error", {}).get("Code")
            if cause is not None and hasattr(cause, "response") else None)
    return code == "ConditionalCheckFailedException"


def create(*, weekday, start_time, end_time, capacity=1, is_active=True,
           starts_on=None, ends_on=None, note="", now=None):
    schedule_id = keys.new_id()
    schedule = Schedule(
        pk=keys.schedule_pk(schedule_id),
        sk=keys.META,
        schedule_id=schedule_id,
        weekday=int(weekday),
        start_time=start_time,
        end_time=end_time,
        capacity=int(capacity),
        is_active=bool(is_active),
        starts_on=starts_on,
        ends_on=ends_on,
        note=note or "",
        gsi1pk=keys.SCHEDULE_GSI1PK,
        gsi1sk=keys.schedule_gsi1sk(weekday, start_time, schedule_id),
    )

    tx = Txn()
    tx.save(SCHEDULE, schedule, condition=Schedule.pk.does_not_exist())
    tx.save(GUARD,
            RuleGuard(pk=keys.rule_guard_pk(weekday, start_time, end_time),
                      sk=keys.GUARD, schedule_id=schedule_id),
            condition=RuleGuard.pk.does_not_exist())
    order = tx.labels_in_wire_order()
    try:
        tx.commit()
    except Exception as exc:
        if getattr(exc, "reasons", None) is not None and failed(exc, GUARD, order):
            raise ConditionFailed(
                "There is already a rule for that weekday and time."
            ) from exc
        raise
    return schedule


def get(schedule_id):
    try:
        return Schedule.get(keys.schedule_pk(schedule_id), keys.META)
    except Schedule.DoesNotExist:
        raise NotFound(f"no schedule {schedule_id!r}") from None


def find(schedule_id):
    try:
        return get(schedule_id)
    except NotFound:
        return None


def all_rules():
    """Every rule, ordered weekday then start time -- the order staff read them in."""
    return list(Schedule.gsi1.query(keys.SCHEDULE_GSI1PK))


def active():
    return [s for s in all_rules() if s.is_active]


def update(schedule, *, weekday, start_time, end_time, capacity, is_active,
           starts_on=None, ends_on=None, note=""):
    """Change a rule, moving its uniqueness guard if the time changed."""
    moved = (int(schedule.weekday) != int(weekday)
             or schedule.start_time != start_time
             or schedule.end_time != end_time)

    actions = [
        Schedule.weekday.set(int(weekday)),
        Schedule.start_time.set(start_time),
        Schedule.end_time.set(end_time),
        Schedule.capacity.set(int(capacity)),
        Schedule.is_active.set(bool(is_active)),
        Schedule.note.set(note or ""),
        Schedule.gsi1sk.set(
            keys.schedule_gsi1sk(weekday, start_time, schedule.schedule_id)),
    ]
    actions.append(Schedule.starts_on.set(starts_on) if starts_on
                   else Schedule.starts_on.remove())
    actions.append(Schedule.ends_on.set(ends_on) if ends_on
                   else Schedule.ends_on.remove())

    if not moved:
        schedule.update(actions=actions)
        schedule.refresh()
        return schedule

    old_guard = keys.rule_guard_pk(schedule.weekday, schedule.start_time,
                                   schedule.end_time)
    tx = Txn()
    tx.update(SCHEDULE, Schedule(pk=schedule.pk, sk=keys.META), actions=actions)
    tx.save(GUARD,
            RuleGuard(pk=keys.rule_guard_pk(weekday, start_time, end_time),
                      sk=keys.GUARD, schedule_id=schedule.schedule_id),
            condition=RuleGuard.pk.does_not_exist())
    tx.delete(GUARD + "_old", RuleGuard(pk=old_guard, sk=keys.GUARD))
    order = tx.labels_in_wire_order()
    try:
        tx.commit()
    except Exception as exc:
        if getattr(exc, "reasons", None) is not None and failed(exc, GUARD, order):
            raise ConditionFailed(
                "There is already a rule for that weekday and time."
            ) from exc
        raise
    schedule.refresh()
    return schedule


def delete(schedule):
    """Remove a rule and free its weekday/time.

    Slots already generated are deliberately left alone -- the ones people have booked
    must survive. `store.slots.clear_schedule` detaches the future ones so they stop
    claiming a rule that no longer exists.
    """
    tx = Txn()
    tx.delete(SCHEDULE, Schedule(pk=schedule.pk, sk=keys.META))
    tx.delete(GUARD, RuleGuard(
        pk=keys.rule_guard_pk(schedule.weekday, schedule.start_time,
                              schedule.end_time),
        sk=keys.GUARD))
    tx.commit()
