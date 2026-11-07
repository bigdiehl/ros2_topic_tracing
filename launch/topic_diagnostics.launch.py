"""topic_diagnostics node, configured by a YAML file.

    ros2 launch ros2_topic_tracing topic_diagnostics.launch.py config:=/path/to/checks.yaml

Tracing must already be running (ros2 run ros2_topic_tracing trace_ctl start, before the stack).
Include it from your own launch file:

    IncludeLaunchDescription(
        PythonLaunchDescriptionSource(PathJoinSubstitution(
            [FindPackageShare("ros2_topic_tracing"), "launch", "topic_diagnostics.launch.py"])),
        launch_arguments={"config": "/path/to/checks.yaml"}.items())

From a source checkout (package not installed) it runs ../topic_diagnostics.py.
"""
from pathlib import Path

from ament_index_python.packages import PackageNotFoundError, get_package_prefix
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

PACKAGE = "ros2_topic_tracing"
SOURCE = Path(__file__).resolve().parent.parent / "topic_diagnostics.py"


def launch_setup(context):
    config = LaunchConfiguration("config").perform(context)
    name = LaunchConfiguration("name").perform(context)
    if not Path(config).is_file():
        raise RuntimeError(f"topic_diagnostics: config file not found: {config}")
    try:
        get_package_prefix(PACKAGE)
    except PackageNotFoundError:
        if not SOURCE.is_file():
            raise
        return [ExecuteProcess(cmd=["python3", str(SOURCE), "--config", config,
                                    "--ros-args", "-r", f"__node:={name}"],
                               name=name, output="screen")]
    return [Node(package=PACKAGE, executable="topic_diagnostics", name=name,
                 arguments=["--config", config], output="screen")]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("config", description="YAML file with the checks"),
        DeclareLaunchArgument("name", default_value="topic_diagnostics", description="node name"),
        OpaqueFunction(function=launch_setup),
    ])
