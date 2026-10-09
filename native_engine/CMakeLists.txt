cmake_minimum_required(VERSION 3.16)
project(ksound_native_engine LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(PIPEWIRE QUIET IMPORTED_TARGET libpipewire-0.3)
endif()

function(ksound_native_target target)
  target_include_directories(${target} PRIVATE include)
  target_compile_options(${target} PRIVATE -O2 -pipe -Wall -Wextra -Wpedantic)
endfunction()

add_executable(ksound_native_engine
  src/main.cpp
  src/files.cpp
  src/realtime.cpp
  src/engine.cpp
)

ksound_native_target(ksound_native_engine)

add_executable(ksound_native_micro_engine
  src/micro_engine.cpp
  src/voice_fx_dsp.cpp
  src/micro_tune_dsp.cpp
  src/hard_gate_dsp.cpp
)
ksound_native_target(ksound_native_micro_engine)
target_link_libraries(ksound_native_micro_engine PRIVATE ${CMAKE_DL_LIBS})

add_executable(ksound_native_meter_engine
  src/meter_engine.cpp
)
ksound_native_target(ksound_native_meter_engine)

# The three runtime engines above use standard command-line PipeWire/Pulse
# clients and can be built without development headers. The direct PipeWire
# probes are diagnostics only, so missing libpipewire headers must not block a
# normal K-Sounds install.
if(PIPEWIRE_FOUND)
  add_executable(ksound_pipewire_probe src/pipewire_probe.cpp)
  ksound_native_target(ksound_pipewire_probe)
  target_link_libraries(ksound_pipewire_probe PRIVATE PkgConfig::PIPEWIRE)

  add_executable(ksound_pipewire_playback_probe src/pipewire_playback_probe.cpp)
  ksound_native_target(ksound_pipewire_playback_probe)
  target_link_libraries(ksound_pipewire_playback_probe PRIVATE PkgConfig::PIPEWIRE)

  add_executable(ksound_pipewire_capture_probe src/pipewire_capture_probe.cpp)
  ksound_native_target(ksound_pipewire_capture_probe)
  target_link_libraries(ksound_pipewire_capture_probe PRIVATE PkgConfig::PIPEWIRE)
else()
  message(STATUS "libpipewire-0.3 development files not found; diagnostic probes skipped")
endif()
