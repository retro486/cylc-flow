# THIS FILE IS PART OF THE CYLC WORKFLOW ENGINE.
# Copyright (C) NIWA & British Crown (Met Office) & Contributors.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Provide a class to represent a task proxy in a running workflow."""

from collections import Counter
from contextlib import suppress
from fnmatch import fnmatchcase
from time import time
from typing import Any, Dict, List, Tuple, Optional, TYPE_CHECKING

from metomi.isodatetime.timezone import get_local_time_zone

import cylc.flow.cycling.iso8601
from cylc.flow.cycling.loader import standardise_point_string
from cylc.flow.exceptions import PointParsingError
from cylc.flow.platforms import get_platform
from cylc.flow.task_id import TaskID
from cylc.flow.task_action_timer import TimerFlags
from cylc.flow.task_state import TaskState, TASK_STATUS_WAITING
from cylc.flow.taskdef import generate_graph_children
from cylc.flow.wallclock import get_unix_time_from_time_string as str2time

if TYPE_CHECKING:
    from cylc.flow.cycling import PointBase
    from cylc.flow.task_action_timer import TaskActionTimer
    from cylc.flow.taskdef import TaskDef


class TaskProxy:
    """Represent an instance of a cycling task in a running workflow.

    Attributes:
        .clock_trigger_time:
            Clock trigger time in seconds since epoch.
        .expire_time:
            Time in seconds since epoch when this task is considered expired.
        .identity:
            Task ID in NAME.POINT syntax.
        .is_late:
            Is the task late?
        .is_manual_submit:
            Is the latest job submission due to a manual trigger?
        .job_vacated:
            Is the latest job pre-empted (or vacated)?
        .jobs:
            A list of job ids associated with the task proxy.
        .local_job_file_path:
            Path on workflow host to the latest job script for the task.
        .late_time:
            Time in seconds since epoch, beyond which the task is considered
            late if it is never active.
        .non_unique_events (collections.Counter):
            Count non-unique events (e.g. critical, warning, custom).
        .point:
            Cycle point of the task.
        .point_as_seconds:
            Cycle point as seconds since epoch.
        .poll_timer:
            Schedule for polling submitted or running jobs.
        .reload_successor:
            The task proxy object that replaces the current instance on reload.
            This attribute provides a useful link to the latest replacement
            instance while the current object may still be referenced by a job
            manipulation command.
        .submit_num:
            Number of times the task has attempted job submission.
        .summary (dict):
            job_runner_name (str):
                Name of job runner where latest job is submitted.
            description (str):
                Same as the .tdef.rtconfig['meta']['description'] attribute.
            execution_time_limit (float):
                Execution time limit of latest job.
            finished_time (float):
                Latest job exit time.
            finished_time_string (str):
                Latest job exit time as string.
            platforms_used (dict):
                Jobs' platform by submit number.
            label (str):
                The .point attribute as string.
            logfiles (list):
                List of names of (extra) known job log files.
            name (str):
                Same as the .tdef.name attribute.
            started_time (float):
                Latest job execution start time.
            started_time_string (str):
                Latest job execution start time as string.
            submit_method_id (str):
                Latest ID of job in job runner.
            submit_num (int):
                Same as the .submit_num attribute.
            submitted_time (float):
                Latest job submission time.
            submitted_time_string (str):
                Latest job submission time as string.
            title (str):
                Same as the .tdef.rtconfig['meta']['title'] attribute.
        .state:
            Object representing the state of this task.
        .platform:
            Dict containing info for platform where latest job is submitted.
        .tdef:
            The definition object of this task.
        .timeout:
            Timeout value in seconds since epoch for latest job
            submission/execution.
        .try_timers:
            Retry schedules as cylc.flow.task_action_timer.TaskActionTimer
            objects.
        .graph_children (dict)
            graph children: {msg: [(name, point), ...]}
        .flow_label:
            flow label
        .reflow:
            flow on from outputs
        .waiting_on_job_prep:
            task waiting on job prep

    Args:
        tdef: The definition object of this task.
        start_point: Start point to calculate the task's cycle point on
            start-up or the cycle point for subsequent tasks.
        flow_label: Which flow within the scheduler this task belongs to.
        status: Task state string.
        is_held: True if the task is held, else False.
        submit_num: Number of times the task has attempted job submission.
        is_late: Is the task late?
        reflow: Flow on from outputs. TODO: better description for arg?
    """

    # Memory optimization - constrain possible attributes to this list.
    __slots__ = [
        'clock_trigger_time',
        'expire_time',
        'identity',
        'is_late',
        'is_manual_submit',
        'job_vacated',
        'jobs',
        'late_time',
        'local_job_file_path',
        'non_unique_events',
        'point',
        'point_as_seconds',
        'poll_timer',
        'reload_successor',
        'submit_num',
        'tdef',
        'state',
        'summary',
        'platform',
        'timeout',
        'try_timers',
        'graph_children',
        'flow_label',
        'reflow',
        'waiting_on_job_prep',
    ]

    def __init__(
        self,
        tdef: 'TaskDef',
        start_point: 'PointBase',
        flow_label: Optional[str],
        status: str = TASK_STATUS_WAITING,
        is_held: bool = False,
        submit_num: int = 0,
        is_late: bool = False,
        reflow: bool = True
    ) -> None:

        self.tdef = tdef
        if submit_num is None:
            submit_num = 0
        self.submit_num = submit_num
        self.jobs: List[str] = []
        self.flow_label = flow_label
        self.reflow = reflow
        self.point = start_point
        self.identity: str = TaskID.get(self.tdef.name, self.point)

        self.reload_successor: Optional['TaskProxy'] = None
        self.point_as_seconds: Optional[int] = None

        self.is_manual_submit = False
        self.summary: Dict[str, Any] = {
            'submitted_time': None,
            'submitted_time_string': None,
            'started_time': None,
            'started_time_string': None,
            'finished_time': None,
            'finished_time_string': None,
            'logfiles': [],
            'platforms_used': {},
            'execution_time_limit': None,
            'job_runner_name': None,
            'submit_method_id': None,
            'flow_label': None
        }

        self.local_job_file_path: Optional[str] = None

        self.platform = get_platform()

        self.job_vacated = False
        self.poll_timer: Optional['TaskActionTimer'] = None
        self.timeout: Optional[float] = None
        self.try_timers: Dict[str, 'TaskActionTimer'] = {}
        self.non_unique_events = Counter()  # type: ignore # TODO: figure out

        self.clock_trigger_time: Optional[float] = None
        self.expire_time: Optional[float] = None
        self.late_time: Optional[float] = None
        self.is_late = is_late
        self.waiting_on_job_prep = True

        self.state = TaskState(tdef, self.point, status, is_held)

        # Determine graph children of this task (for spawning).
        self.graph_children = generate_graph_children(tdef, self.point)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} '{self.identity}'>"

    def __str__(self) -> str:
        """Stringify using "self.identity"."""
        return self.identity

    def copy_to_reload_successor(self, reload_successor):
        """Copy attributes to successor on reload of this task proxy."""
        self.reload_successor = reload_successor
        reload_successor.submit_num = self.submit_num
        reload_successor.is_manual_submit = self.is_manual_submit
        reload_successor.summary = self.summary
        reload_successor.local_job_file_path = self.local_job_file_path
        reload_successor.try_timers = self.try_timers
        reload_successor.platform = self.platform
        reload_successor.job_vacated = self.job_vacated
        reload_successor.poll_timer = self.poll_timer
        reload_successor.timeout = self.timeout
        reload_successor.state.outputs = self.state.outputs
        reload_successor.state.is_held = self.state.is_held
        reload_successor.state.is_runahead = self.state.is_runahead
        reload_successor.state.is_updated = self.state.is_updated
        reload_successor.state.prerequisites = self.state.prerequisites

    @staticmethod
    def get_offset_as_seconds(offset):
        """Return an ISO interval as seconds."""
        iso_offset = cylc.flow.cycling.iso8601.interval_parse(str(offset))
        return int(iso_offset.get_seconds())

    def get_late_time(self):
        """Compute and store late time as seconds since epoch."""
        if self.late_time is None:
            if self.tdef.rtconfig['events']['late offset']:
                self.late_time = (
                    self.get_point_as_seconds() +
                    self.tdef.rtconfig['events']['late offset'])
            else:
                # Not used, but allow skip of the above "is None" test
                self.late_time = 0
        return self.late_time

    def get_point_as_seconds(self):
        """Compute and store my cycle point as seconds since epoch."""
        if self.point_as_seconds is None:
            iso_timepoint = cylc.flow.cycling.iso8601.point_parse(
                str(self.point))
            self.point_as_seconds = int(iso_timepoint.get(
                'seconds_since_unix_epoch'))
            if iso_timepoint.time_zone.unknown:
                utc_offset_hours, utc_offset_minutes = (
                    get_local_time_zone())
                utc_offset_in_seconds = (
                    3600 * utc_offset_hours + 60 * utc_offset_minutes)
                self.point_as_seconds += utc_offset_in_seconds
        return self.point_as_seconds

    def get_try_num(self):
        """Return the number of automatic tries (try number)."""
        try:
            return self.try_timers[TimerFlags.EXECUTION_RETRY].num + 1
        except (AttributeError, KeyError):
            return 0

    def next_point(self):
        """Return the next cycle point."""
        p_next = None
        adjusted = []
        for seq in self.tdef.sequences:
            nxt = seq.get_next_point(self.point)
            if nxt:
                # may be None if beyond the sequence bounds
                adjusted.append(nxt)
        if adjusted:
            p_next = min(adjusted)
        return p_next

    def is_ready_to_run(self) -> Tuple[bool, ...]:
        """Is this task ready to run?

        Takes account of all dependence: on other tasks, xtriggers, and
        old-style ext- and clock-triggers. Or, manual triggering.

        """
        if self.is_manual_submit:
            # Manually triggered, ignore unsatisfied prerequisites.
            return (True,)
        if self.state.is_held:
            # A held task is not ready to run.
            return (False,)
        if self.state.status in self.try_timers:
            # A try timer is still active.
            return (self.try_timers[self.state.status].is_delay_done(),)
        return (
            self.state(TASK_STATUS_WAITING),
            self.is_waiting_clock_done(),
            self.is_waiting_prereqs_done()
        )

    def set_summary_time(self, event_key, time_str=None):
        """Set an event time in self.summary

        Set values of both event_key + "_time" and event_key + "_time_string".
        """
        if time_str is None:
            self.summary[event_key + '_time'] = None
        else:
            self.summary[event_key + '_time'] = float(str2time(time_str))
        self.summary[event_key + '_time_string'] = time_str

    def is_waiting_clock_done(self):
        """Is this task done waiting for its clock trigger time?

        Return True if there is no clock trigger or when clock trigger is done.
        """
        if self.tdef.clocktrigger_offset is None:
            return True
        if self.clock_trigger_time is None:
            self.clock_trigger_time = (
                self.get_point_as_seconds() +
                self.get_offset_as_seconds(self.tdef.clocktrigger_offset))
        return time() >= self.clock_trigger_time

    def is_task_prereqs_not_done(self):
        """Are some task prerequisites not satisfied?"""
        return (not all(pre.is_satisfied()
                for pre in self.state.prerequisites))

    def is_waiting_prereqs_done(self):
        """Are ALL prerequisites satisfied?"""
        return (
            all(pre.is_satisfied() for pre in self.state.prerequisites)
            and all(tri for tri in self.state.external_triggers.values())
            and self.state.xtriggers_all_satisfied()
        )

    def reset_try_timers(self):
        # unset any retry delay timers
        for timer in self.try_timers.values():
            timer.timeout = None

    def point_match(self, point: Optional[str]) -> bool:
        """Return whether a string/glob matches the task's point.

        None is treated as '*'.
        """
        if point is None:
            return True
        with suppress(PointParsingError):  # point_str may be a glob
            point = standardise_point_string(point)
        return fnmatchcase(str(self.point), point)

    def status_match(self, status: Optional[str]) -> bool:
        """Return whether a string matches the task's status.

        None/an empty string is treated as a match.
        """
        return (not status) or self.state.status == status

    def name_match(self, name: str) -> bool:
        """Return whether a string/glob matches the task's name."""
        if fnmatchcase(self.tdef.name, name):
            return True
        return any(
            fnmatchcase(ns, name) for ns in self.tdef.namespace_hierarchy
        )
