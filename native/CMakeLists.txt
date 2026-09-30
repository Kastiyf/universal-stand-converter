cmake_minimum_required(VERSION 3.21)
project(UniversalStandNative LANGUAGES CXX)

set(OPENSKP_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(OPENSKP_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(OPENSKP_BUILD_WASM OFF CACHE BOOL "" FORCE)

if(NOT DEFINED OPENSKP_SOURCE_DIR)
    message(FATAL_ERROR "OPENSKP_SOURCE_DIR is required")
endif()

add_subdirectory("${OPENSKP_SOURCE_DIR}" "${CMAKE_BINARY_DIR}/openskp" EXCLUDE_FROM_ALL)

add_executable(skp2glb skp2glb.cpp)
target_compile_features(skp2glb PRIVATE cxx_std_17)
target_link_libraries(skp2glb PRIVATE OpenSkp::OpenSkp)
