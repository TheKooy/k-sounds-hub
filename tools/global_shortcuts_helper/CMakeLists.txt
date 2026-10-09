cmake_minimum_required(VERSION 3.20)

project(ksound_global_shortcuts_helper LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 REQUIRED COMPONENTS Core DBus)

add_executable(ksound-global-shortcuts-helper
    main.cpp
)

target_link_libraries(ksound-global-shortcuts-helper
    PRIVATE
        Qt6::Core
        Qt6::DBus
)
