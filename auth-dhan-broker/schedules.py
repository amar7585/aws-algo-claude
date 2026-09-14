"""
EventBridge Scheduler — the session schedules this function switches.

UPDATE IS A REPLACE, NOT A PATCH. Scheduler has no EnableSchedule /
DisableSchedule pair — that is EventBridge Rules, a different service with a
different API. Every field not sent back to UpdateSchedule is dropped, so a
hand-built payload silently discards whatever the schedule was created with:
its timezone, its retry policy, its flexible time window. The definition is
therefore read with GetSchedule and returned whole, with only State changed.
"""

import logging

import boto3

from config import MANAGED_SCHEDULES, SCHEDULE_GROUP

logger = logging.getLogger()

# Reused across warm invocations; creating a boto3 client is not cheap.
_scheduler = boto3.client("scheduler")

# Present in a GetSchedule response and rejected by UpdateSchedule.
_READ_ONLY_SCHEDULE_KEYS = (
    "Arn",
    "CreationDate",
    "LastModificationDate",
    "ResponseMetadata",
)


def set_schedule_state(name, state):
    """Switch one schedule to ENABLED or DISABLED. Returns what it did."""
    definition = _scheduler.get_schedule(Name=name, GroupName=SCHEDULE_GROUP)
    current = definition.get("State")
    if current == state:
        logger.info("schedule %s already %s", name, state)
        return "unchanged"

    payload = {
        key: value
        for key, value in definition.items()
        if key not in _READ_ONLY_SCHEDULE_KEYS
    }
    payload["State"] = state
    _scheduler.update_schedule(**payload)
    logger.info("schedule %s %s -> %s", name, current, state)
    return "updated"


def set_managed_schedules(state):
    """Switch every managed schedule.

    Any failure propagates. A schedule left in the wrong state is invisible,
    and the two directions are not equally bad: enabled-when-it-should-be-off
    wastes a few no-op invocations, while disabled-when-it-should-be-on costs
    the entire session and alarms nothing, because no alarm here can see a
    function that was never invoked. Raising is the only thing that reports it.
    """
    return {name: set_schedule_state(name, state) for name in MANAGED_SCHEDULES}
