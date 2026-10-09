cmake_minimum_required(VERSION 3.20)

project(ksound_kglobalaccel_helper LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 REQUIRED COMPONENTS Core Gui)
find_package(KF6GlobalAccel REQUIRED)

add_executable(ksound-kglobalaccel-helper
    main.cpp
)

target_link_libraries(ksound-kglobalaccel-helper
    PRIVATE
        Qt6::Core
        Qt6::Gui
        KF6::GlobalAccel
)
