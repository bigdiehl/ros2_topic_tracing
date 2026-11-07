# ament_cmake package: trace_daemon, the Python commands, launch file, example config.
# collector/CMakeLists.txt builds trace_daemon alone (no ROS).
cmake_minimum_required(VERSION 3.16)
project(ros2_topic_tracing CXX)
set(CMAKE_CXX_STANDARD 17)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(BABELTRACE2 REQUIRED IMPORTED_TARGET babeltrace2)

add_executable(trace_daemon collector/trace_daemon.cpp)
target_compile_options(trace_daemon PRIVATE -Wall -Wextra)
target_link_libraries(trace_daemon PkgConfig::BABELTRACE2)
install(TARGETS trace_daemon DESTINATION lib/${PROJECT_NAME})

# ros2 run ros2_topic_tracing {trace_ctl,topic_stats,topic_diagnostics}
foreach(cmd trace_ctl topic_stats topic_diagnostics)
  install(PROGRAMS ${cmd}.py DESTINATION lib/${PROJECT_NAME} RENAME ${cmd})
endforeach()
ament_python_install_module(trace_stats.py)

install(DIRECTORY launch config DESTINATION share/${PROJECT_NAME})

ament_package()
