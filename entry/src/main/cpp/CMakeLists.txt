cmake_minimum_required(VERSION 3.16)
project(binrunner C CXX ASM)

set(NATIVERENDER_ROOT_PATH ${CMAKE_CURRENT_SOURCE_DIR})
set(ELF_LOADER_DIR ${NATIVERENDER_ROOT_PATH}/../../../../third_party/elf/src)

include_directories(${NATIVERENDER_ROOT_PATH} ${ELF_LOADER_DIR})

# MikhailProg/elf 内存 loader（已打 HarmonyOS jit prctl 补丁），
# 以源码形式编入 libentry.so，fork 后在子进程内直接调用 z_entry 跳转执行目标 ELF
set(ELF_LOADER_SOURCES
    ${ELF_LOADER_DIR}/loader.c
    ${ELF_LOADER_DIR}/z_syscalls.c
    ${ELF_LOADER_DIR}/z_err.c
    ${ELF_LOADER_DIR}/z_printf.c
    ${ELF_LOADER_DIR}/z_utils.c
    ${ELF_LOADER_DIR}/aarch64/z_syscall.S
    ${ELF_LOADER_DIR}/aarch64/z_trampo.S
)

add_library(entry SHARED napi_init.cpp ${ELF_LOADER_SOURCES})

set_source_files_properties(${ELF_LOADER_SOURCES}
    PROPERTIES COMPILE_OPTIONS "-DELFCLASS=ELFCLASS64")

target_link_libraries(entry PUBLIC libace_napi.z.so libhilog_ndk.z.so)
