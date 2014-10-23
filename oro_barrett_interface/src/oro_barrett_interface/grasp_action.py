
from collections import deque

from math import pi, sin, cos, sqrt
import numpy

import rospy
import actionlib

from sensor_msgs.msg import JointState
from oro_barrett_msgs.msg import BHandGraspAction, BHandStatus, BHandCmd, BHandCmdMode

def area(a1, a2):
    l1 = 0.07 # inner finger
    l2 = 0.05 # outer finger
    lp = 0.10 # palm width
    # full outer link angle (including offset)
    a2 = a2 + pi/4.0
    # finger triangle
    area1 = 0.5 * l1*l2*sin(pi-a2)
    # palm triangle
    l3 = sqrt(l1*l1 + l2*l2 - 2*l1*l2*cos(pi-a2))
    area2 = 0.5 * lp * l3 * (sin(a1)*sqrt(1-pow(l2/l3*sin(a2),2.0)) + l2/l3*sin(a2)*cos(a1))
    # outer triangle
    area3 = 0.5 * (sin(a1) + sin(a1+a2)) * (lp+cos(a1)+cos(a1+a2))
    rospy.loginfo("---------\nA1: %g\nA2: %g\nA3: %g" % (area1, area2, area3))
    return area1 + area2 + area3

class GraspAction(object):
    """
    The Grasp Action performs a closed loop two-stage grasp. It first closes
    the fingers at a given speed until they encounter resistance and are
    stopped. Second, it applies a given effort with the desired fingers.

    Once it has completed, it will leave the hand executing an effort command.
    If it is preempted, it will idle the fingers that it is commanding.

    See the BHandGraspAction message for more details.
    """
    # Activity states
    PREGRASP = 0
    GRASPING = 1
    HOLDING = 2
    ABORTING = 3
    PREEMPTING = 4

    def __init__(self, name='grasp', parent=None):
        # Delegates
        self.server = None

        # Properties
        self.name = name
        self.parent = parent

        # State
        self.state = None
        self.done_moving = None
        # Buffers for determining when the fingers have stopped
        self.stamp_history = None
        self.velocity_history = None
        self.position_history = None

        self.inner_outer_indices = zip([0,1,2], [2,3,4], [5,6,7])
        self.areas = [0,0,0]

        # ROS parameters
        self.feedback_period = rospy.Duration(rospy.get_param('~feedback_period', 0.1))
        self.static_vel_threshold = rospy.get_param('~static_vel_threshold', 0.1)
        self.static_pos_threshold = rospy.get_param('~static_pos_threshold', 0.05)
        self.vel_filter_cutoff = rospy.get_param('~vel_filter_cutoff', 0.5)
        self.pos_filter_cutoff = rospy.get_param('~pos_filter_cutoff', 0.5)
        self.min_static_duration = rospy.Duration(rospy.get_param('~min_static_duration', 0.1))
        self.max_static_duration = self.min_static_duration * 2.0

        # ROS topics
        self.joint_states_sub = rospy.Subscriber(
            'hand/joint_states',
            JointState,
            self.joint_states_cb)
        self.status_pub = rospy.Subscriber(
            'hand/status',
            BHandStatus,
            self.status_cb)
        self.cmd_pub = rospy.Publisher(
            'hand/cmd',
            BHandCmd)

        # Create ROS action server
        self.server = actionlib.SimpleActionServer(
            self.name, BHandGraspAction,
            auto_start=False)

        self.server.register_goal_callback(self.goal_cb)
        self.server.register_preempt_callback(self.preempt_cb)

        self.server.start()

    def joint_states_cb(self, msg):
        """Determine when joints are done moving"""

        # Compute finger areas
        for i, inner, outer in self.inner_outer_indices:
            self.areas[i] = area(msg.position[inner], msg.position[outer])
            rospy.loginfo("Area %d: %g" % (i, self.areas[i]))

        # Return if not active
        if self.server and not self.server.is_active():
            return

        # Add the latest joint state
        now = rospy.Time.now()

        if (now-msg.header.stamp).to_sec() < -0.01:
            msg = "Hand state timestamp is in the future by %g seconds." % (now-msg.header.stamp).to_sec()
            rospy.logerr(msg)
            self.server.set_aborted(text=msg)
            return

        # Append to the history
        self.stamp_history.append(msg.header.stamp)
        for dof, (dof_hist, new_vel) in enumerate(zip(self.velocity_history, msg.velocity)):
            a = self.vel_filter_cutoff
            new_vel_filtered = a * dof_hist[-1] + (1.0 - a) * new_vel if len(dof_hist) > 0 else new_vel
            dof_hist.append(new_vel_filtered)
            #print("Joint %d vel: %g" % (dof, new_vel))

        for dof, (dof_hist, new_pos) in enumerate(zip(self.position_history, msg.position)):
            a = self.pos_filter_cutoff
            new_pos_filtered = a * dof_hist[-1] + (1.0 - a) * new_pos if len(dof_hist) > 0 else new_pos
            dof_hist.append(new_pos_filtered)
            #print("Joint %d pos: %g" % (dof, new_pos))

        # Pop off old data
        while len(self.stamp_history) > 0 and (now - self.stamp_history[0]) > self.max_static_duration:
            self.stamp_history.popleft()
            for dof_hist in self.velocity_history:
                dof_hist.popleft()
            for dof_hist in self.position_history:
                dof_hist.popleft()

        # Find the index of the relevant elements
        #start_index = min([i for i, s in enumerate(self.stamp_history) if (now-s) < self.min_static_duration])
        #print("Start index: %d" % start_index)

        if len(self.stamp_history) == 0 or now - self.stamp_history[0] < self.min_static_duration:
            return

        # Check the velocity
        for dof, (vel_hist, pos_hist)  in enumerate(zip(self.velocity_history, self.position_history)):
            # Mark the dof as done moving if it's below the static velocity threshold
            if 0:
                below_thresh = [abs(v) < self.static_vel_threshold for v in vel_hist]
                print("dof %d %d%% below threshold" % (dof, 100*len([b for b in below_thresh if b])/len(below_thresh)))
                if all(below_thresh):
                    self.done_moving[dof] = True
            else:
                below_thresh = (max(pos_hist) - min(pos_hist)) < self.static_pos_threshold
                self.done_moving[dof] = below_thresh

    def is_used(self, dof):
        if dof in [2, 3, 4]:
            return self.active_goal.grasp_mask[dof - 2]
        elif dof in [5, 6, 7]:
            return self.active_goal.grasp_mask[dof - 5]
        return False

    def status_cb(self, msg):
        """Interpret BHand status, send appropriate commands and update activity state"""

        # Return if not active
        if self.server and not self.server.is_active():
            return

        # Get the masked modes
        masked_modes = [m for i, m in enumerate(msg.mode) if i < 3 and self.active_goal.grasp_mask[i]]

        # Check the modes based on the activity states:
        if self.state == self.PREGRASP:
            rospy.loginfo("Sending grasp command...")
            self.cmd_pub.publish(self.grasp_cmd)
            # Check if all joints are in velocity mode
            if all([m == BHandCmdMode.MODE_TRAPEZOIDAL for m in masked_modes]):
                self.state = self.GRASPING
                self.grasp_start_time = rospy.Time.now()
                rospy.loginfo("Grasping...")

        elif self.state == self.GRASPING:
            # Check if all joints are in effort mode
            if not all([m == BHandCmdMode.MODE_TORQUE for m in masked_modes]):
                # Check if the hand is done moving, and change to effort command
                if all([dm for dof, dm in enumerate(self.done_moving) if self.is_used(dof)]):
                    rospy.loginfo("Sending hold command...")
                    self.cmd_pub.publish(self.hold_cmd)
            else:
                rospy.loginfo("Grasped.")
                self.state = self.HOLDING
                self.server.set_succeeded()

        elif self.state == self.HOLDING:
            pass

        elif self.state in [self.ABORTING, self.PREEMPTING]:
            # Check if all joints are in effort mode
            if not all([m == BHandCmdMode.MODE_IDLE for m in masked_modes]):
                rospy.logwarn("Aborting grasp.")
                self.cmd_pub.publish(self.abort_cmd)
            else:
                self.state = None
                if self.state == self.ABORTING:
                    self.server.set_aborted()
                elif self.state == self.PREEMPTING:
                    self.server.set_preempted()

    def goal_cb(self):
        """Construct a new command and reset the activity state"""
        # Tell the parent to preempt peers
        if self.parent:
            rospy.loginfo("Preempting peers...")
            self.parent.preempt_peers_of(self.name)

        # Accept the new goal
        rospy.loginfo("Accepting new goal...")
        self.active_goal = self.server.accept_new_goal()

        # Clear the grasp state
        self.state = self.PREGRASP
        self.stamp_history = deque()
        self.velocity_history = [deque() for i in range(8)]
        self.position_history = [deque() for i in range(8)]
        self.done_moving = [False] * 8
        self.grasp_start_time = rospy.Time.now()

        # Construct hand commands for grasping and holding
        self.grasp_cmd = BHandCmd()
        self.hold_cmd = BHandCmd()
        self.abort_cmd = BHandCmd()
        for f_id, use_finger in enumerate(self.active_goal.grasp_mask):
            rospy.loginfo("Grasp %s finger %d" % ('using' if use_finger else 'not using', 1+f_id))
            # Grasp command
            self.grasp_cmd.mode[f_id] = BHandCmd.MODE_TRAPEZOIDAL if use_finger else BHandCmd.MODE_SAME
            self.grasp_cmd.cmd[f_id] = self.active_goal.grasp_speed[f_id] if use_finger else 0.0

            # Hold command
            self.hold_cmd.mode[f_id] = BHandCmd.MODE_TORQUE if use_finger else BHandCmd.MODE_SAME
            self.hold_cmd.cmd[f_id] = self.active_goal.grasp_effort[f_id] if use_finger else 0.0

            # Abort command
            self.abort_cmd.mode[f_id] = BHandCmd.MODE_IDLE if use_finger else BHandCmd.MODE_SAME
            self.abort_cmd.cmd[f_id] = 0.0

        self.grasp_cmd.mode[3] = -1
        self.hold_cmd.mode[3] = -1
        self.abort_cmd.mode[3] = -1

    def preempt_cb(self):
        """Idle the hand"""
        rospy.logwarn("Preemption requested!")
        self.state = self.PREEMPTING

    def peer_preempt_cb(self):
        """Abort the goal without idling"""
        rospy.logwarn("%s: Preemption requested by peer!" % self.name)
        if self.server and self.server.is_active():
            self.server.set_aborted(text="Peer command received")
