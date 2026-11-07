# Builds trace_daemon. With ament_cmake available (ROS sourced, colcon, the .deb) it is also
# the ament package: the Python commands, launch file and example config are installed.
#   cmake -S . -B build && make -C build        # build/trace_daemon, no ROS needed
cmake_minimum_required(VERSION 3.16)
project(ros2_topic_tracing CXX)
set(CMAKE_CXX_STANDARD 17)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(PkgConfig REQUIRED)
pkg_check_modules(BABELTRACE2 REQUIRED IMPORTED_TARGET babeltrace2)
add_executable(trace_daemon collector/trace_daemon.cpp)
target_compile_options(trace_daemon PRIVATE -Wall -Wextra)
target_link_libraries(trace_daemon PkgConfig::BABELTRACE2)

find_package(ament_cmake QUIET)
if(NOT ament_cmake_FOUND)
  message(STATUS "ament_cmake not found: building trace_daemon only")
  return()
endif()
find_package(ament_cmake_python REQUIRED)

install(TARGETS trace_daemon DESTINATION lib/${PROJECT_NAME})
# ros2 run ros2_topic_tracing {trace_ctl,topic_stats,topic_diagnostics}
foreach(cmd trace_ctl topic_stats topic_diagnostics)
  install(PROGRAMS ${cmd}.py DESTINATION lib/${PROJECT_NAME} RENAME ${cmd})
endforeach()
ament_python_install_module(trace_stats.py)
install(DIRECTORY launch config DESTINATION share/${PROJECT_NAME})

ament_package()
