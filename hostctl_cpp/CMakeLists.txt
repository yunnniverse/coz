cmake_minimum_required(VERSION 3.13)
project(hostctl_cpp)

# 1) C++17 활성화 ― Add_executable 전에 두는 편이 관례지만 기능상 차이는 없음
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(CheckCXXSourceCompiles)

find_package(CURL REQUIRED)
find_program(CLANG_EXECUTABLE clang)

# 2) 소스 목록
add_executable(hostctl_cpp
    main.cpp
    perf.cpp
    request_credit.cpp
)

check_cxx_source_compiles(
  "
  #include <linux/bpf.h>
  int main() {
    enum bpf_stats_type t = BPF_STATS_RUN_TIME;
    (void)t;
    return 0;
  }
  "
  HOSTCTL_HAS_BPF_STATS_TYPE
)
if(HOSTCTL_HAS_BPF_STATS_TYPE)
  target_compile_definitions(hostctl_cpp PRIVATE HOSTCTL_HAS_BPF_STATS_TYPE=1)
endif()

###) uapi 헤더 경로 추가
target_include_directories(hostctl_cpp PRIVATE
    ${CMAKE_SOURCE_DIR}/kernel/uapi
)

# 3) pthread 찾기
find_package(Threads REQUIRED)

# 4) 필요한 라이브러리 연결
target_link_libraries(hostctl_cpp
    PRIVATE
        Threads::Threads   # -lpthread
        rt                 # -lrt (timer_create·timer_settime)
        CURL::libcurl                       # ★ 추가
        bpf
        elf
        z
)

set(REQUEST_BPF_SRC ${CMAKE_CURRENT_SOURCE_DIR}/bpf/request_credit.bpf.c)
set(REQUEST_BPF_OBJ ${CMAKE_CURRENT_BINARY_DIR}/request_credit.bpf.o)
set(BPF_ARCH_INCLUDE "/usr/include/${CMAKE_SYSTEM_PROCESSOR}-linux-gnu")
if(NOT EXISTS "${BPF_ARCH_INCLUDE}/asm/types.h")
  set(BPF_ARCH_INCLUDE "/usr/include/x86_64-linux-gnu")
endif()

if(CLANG_EXECUTABLE)
  add_custom_command(
      OUTPUT ${REQUEST_BPF_OBJ}
      COMMAND ${CLANG_EXECUTABLE} -O2 -g -target bpf -D__TARGET_ARCH_x86 -D__BPF__
              -I${BPF_ARCH_INCLUDE}
              -I/usr/include
              -c ${REQUEST_BPF_SRC}
              -o ${REQUEST_BPF_OBJ}
      DEPENDS ${REQUEST_BPF_SRC}
      COMMENT "Building request-credit eBPF object"
      VERBATIM
  )
  add_custom_target(hostctl_cpp_request_bpf ALL DEPENDS ${REQUEST_BPF_OBJ})
  add_dependencies(hostctl_cpp hostctl_cpp_request_bpf)
  install(FILES ${REQUEST_BPF_OBJ} DESTINATION bin/hostctl_cpp)
endif()

# 5) GCC 7 / 8 전용 <filesystem> 추가 아카이브
if (CMAKE_CXX_COMPILER_ID STREQUAL "GNU" AND
    CMAKE_CXX_COMPILER_VERSION VERSION_LESS 9)
    target_link_libraries(hostctl_cpp PRIVATE stdc++fs)
endif()
