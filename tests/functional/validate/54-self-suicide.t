#!/usr/bin/env bash
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
#-------------------------------------------------------------------------------
# Test validation, graph for workflow with self-suiciding task
. "$(dirname "$0")/test_header"
#-------------------------------------------------------------------------------
set_test_number 3
#-------------------------------------------------------------------------------
init_workflow "${TEST_NAME_BASE}" << __FLOW__
[scheduler]
    allow implicit tasks = True
[scheduling]
    [[graph]]
        R1 = """
            foo? => bar => baz
            foo:fail? => qux => baz
            foo:fail? & baz => !foo
        """
__FLOW__
#-------------------------------------------------------------------------------
TEST_NAME="${TEST_NAME_BASE}-val"
run_ok "${TEST_NAME}" cylc validate "${WORKFLOW_NAME}"
#-------------------------------------------------------------------------------
TEST_NAME=${TEST_NAME_BASE}-graph-check
run_ok "${TEST_NAME}" cylc graph --reference "${WORKFLOW_NAME}"
cmp_ok "${TEST_NAME}.stdout" <<'__OUT__'
edge "bar.1" "baz.1"
edge "foo.1" "bar.1"
edge "foo.1" "qux.1"
edge "qux.1" "baz.1"
graph
node "bar.1" "bar\n1"
node "baz.1" "baz\n1"
node "foo.1" "foo\n1"
node "qux.1" "qux\n1"
stop
__OUT__
#-------------------------------------------------------------------------------
purge
