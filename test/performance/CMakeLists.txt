find_package(Python3 3.8 REQUIRED COMPONENTS Interpreter)

add_executable(entwine-benchmark benchmark.cpp)
compiler_options(entwine-benchmark)
target_link_libraries(entwine-benchmark PRIVATE entwine pdalcpp
    OpenSSL::applink OpenSSL::Crypto)
if (CURL_FOUND)
    target_link_libraries(entwine-benchmark PRIVATE CURL::libcurl)
endif()
target_compile_definitions(entwine-benchmark PRIVATE
    ENTWINE_BENCHMARK_BUILD_TYPE="$<CONFIG>"
    ENTWINE_BENCHMARK_LIBRARY="$<TARGET_FILE:entwine>"
    ENTWINE_BENCHMARK_FLAGS="${CMAKE_CXX_FLAGS} $<$<CONFIG:Debug>:${CMAKE_CXX_FLAGS_DEBUG}>$<$<CONFIG:Release>:${CMAKE_CXX_FLAGS_RELEASE}>$<$<CONFIG:RelWithDebInfo>:${CMAKE_CXX_FLAGS_RELWITHDEBINFO}>$<$<CONFIG:MinSizeRel>:${CMAKE_CXX_FLAGS_MINSIZEREL}>"
    ENTWINE_BENCHMARK_COMPILER="${CMAKE_CXX_COMPILER_ID}-${CMAKE_CXX_COMPILER_VERSION}")

set(ENTWINE_BENCHMARK_ARGS "" CACHE STRING
    "Additional benchmark runner arguments (a CMake semicolon-separated list)")
add_custom_target(benchmark
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run.py
        --executable $<TARGET_FILE:entwine-benchmark>
        --output ${CMAKE_CURRENT_BINARY_DIR}/results.json
        ${ENTWINE_BENCHMARK_ARGS}
    DEPENDS entwine-benchmark
    USES_TERMINAL
    VERBATIM)

# These check the harness, not performance. No timing thresholds in CTest.
if (WITH_TESTS)
    add_test(NAME benchmark-runner
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/test_runner.py)
    add_test(NAME benchmark-smoke
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run.py
            --executable $<TARGET_FILE:entwine-benchmark>
            --output ${CMAKE_CURRENT_BINARY_DIR}/smoke.json
            --points 4000 --files 4 --span 16 --repetitions 1 --warmups 0
            --cases analyze-shallow analyze-deep build-binary build-laszip build-reproject pack-binary)
    set_tests_properties(benchmark-runner benchmark-smoke PROPERTIES
        LABELS "benchmark-harness" TIMEOUT 120)
    set_tests_properties(benchmark-smoke PROPERTIES RUN_SERIAL TRUE)
endif()
