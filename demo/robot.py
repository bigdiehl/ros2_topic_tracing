#!/usr/bin/env python3
"""Dummy robot: several nodes in one process with a sensor -> planner -> controller pipeline.

    python3 robot.py --ros-args -r __ns:=/robot1 -p fault:=none

fault:
  none           everything nominal
  lidar_dropout  lidar stops publishing for 5 s out of every 15 s
  slow_camera    camera publishes at 6 Hz instead of 15 Hz
  dead_imu       imu never publishes (odometry receives nothing)
"""
import signal
import sys
import time

import rclpy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import BatteryState, Image, Imu, LaserScan

# node name -> (publishers [(topic, type, Hz)], subscriptions [(topic, type)])
NODES = {
    "camera":     ([("camera/image", Image, 15.0)], []),
    "lidar":      ([("scan", LaserScan, 10.0)], []),
    "imu":        ([("imu", Imu, 50.0)], []),
    "odometry":   ([("odom", Odometry, 25.0)], [("imu", Imu)]),
    "planner":    ([("cmd_vel", Twist, 10.0)], [("scan", LaserScan), ("odom", Odometry),
                                                ("camera/image", Image)]),
    "controller": ([], [("cmd_vel", Twist)]),
    "dashboard":  ([], [("battery", BatteryState)]),  # nobody publishes battery
}


class DummyNode(Node):
    def __init__(self, name, pubs, subs, fault, t0):
        super().__init__(name)
        self.received = 0
        for topic, mtype in subs:
            self.create_subscription(mtype, topic, self.on_msg, 10)
        for topic, mtype, rate in pubs:
            if fault == "slow_camera" and name == "camera":
                rate = 6.0
            if fault == "dead_imu" and name == "imu":
                continue
            pub = self.create_publisher(mtype, topic, 10)
            msg = mtype()
            if isinstance(msg, Image):
                msg.height, msg.width, msg.encoding, msg.step = 120, 160, "mono8", 160
                msg.data = bytes(120 * 160)
            if isinstance(msg, LaserScan):
                msg.ranges = [1.0] * 360

            def tick(pub=pub, msg=msg, name=name):
                if fault == "lidar_dropout" and name == "lidar" and (time.time() - t0) % 15 > 10:
                    return
                pub.publish(msg)
            self.create_timer(1.0 / rate, tick)

    def on_msg(self, _):
        self.received += 1


def main():
    rclpy.init(args=sys.argv)
    probe = Node("_robot_param_probe")
    fault = probe.declare_parameter("fault", "none").value
    probe.destroy_node()

    t0 = time.time()
    nodes = [DummyNode(n, p, s, fault, t0) for n, (p, s) in NODES.items()]
    ex = SingleThreadedExecutor()
    for n in nodes:
        ex.add_node(n)
    nodes[0].get_logger().info(f"{len(nodes)} nodes up, fault={fault}")
    try:
        ex.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
