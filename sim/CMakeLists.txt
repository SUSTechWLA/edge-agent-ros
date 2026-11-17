cmake_minimum_required(VERSION 3.16)
project(edge_sim LANGUAGES CXX)

find_package(gz-sim8 REQUIRED)
find_package(gz-transport13 REQUIRED)
find_package(gz-plugin2 REQUIRED COMPONENTS register)

add_library(edge_velocity_watchdog SHARED velocity_watchdog.cpp)
target_compile_features(edge_velocity_watchdog PRIVATE cxx_std_20)
target_compile_options(edge_velocity_watchdog PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(edge_velocity_watchdog PRIVATE
  gz-sim8::gz-sim8 gz-transport13::gz-transport13 gz-plugin2::register)
install(TARGETS edge_velocity_watchdog LIBRARY DESTINATION lib/edge_sim)
