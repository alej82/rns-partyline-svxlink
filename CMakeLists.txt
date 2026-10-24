cmake_minimum_required(VERSION 3.16)
project(rns-partyline-svx LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT DEFINED SVXLINK_SRC)
  if(DEFINED ENV{SVXLINK_SRC})
    set(SVXLINK_SRC "$ENV{SVXLINK_SRC}")
  else()
    set(SVXLINK_SRC "$ENV{HOME}/svx/svxlink-maint")
  endif()
endif()

if(NOT DEFINED SVXLINK_BUILD)
  if(DEFINED ENV{SVXLINK_BUILD})
    set(SVXLINK_BUILD "$ENV{SVXLINK_BUILD}")
  else()
    set(SVXLINK_BUILD "${SVXLINK_SRC}/build")
  endif()
endif()

set(SVXLINK_MODULE_HEADER "${SVXLINK_SRC}/src/svxlink/svxlink/Module.h")
set(SVXLINK_VERSION_HEADER "${SVXLINK_BUILD}/include/version/SVXLINK.h")

if(NOT EXISTS "${SVXLINK_MODULE_HEADER}")
  message(FATAL_ERROR
    "No encuentro ${SVXLINK_MODULE_HEADER}.\n"
    "Indica el árbol fuente de SvxLink con -DSVXLINK_SRC=... o SVXLINK_SRC=...")
endif()

if(NOT EXISTS "${SVXLINK_VERSION_HEADER}")
  message(FATAL_ERROR
    "No encuentro ${SVXLINK_VERSION_HEADER}.\n"
    "Indica el build de SvxLink con -DSVXLINK_BUILD=... o SVXLINK_BUILD=...")
endif()

file(READ "${SVXLINK_VERSION_HEADER}" _svxlink_version_header)
string(REGEX MATCH "SVXLINK_APP_VERSION \"([^\"]+)\"" _svxlink_version_match "${_svxlink_version_header}")
set(SVXLINK_APP_VERSION "${CMAKE_MATCH_1}")
if(NOT SVXLINK_APP_VERSION)
  message(FATAL_ERROR "No pude leer SVXLINK_APP_VERSION de ${SVXLINK_VERSION_HEADER}")
endif()

message(STATUS "SvxLink source:  ${SVXLINK_SRC}")
message(STATUS "SvxLink build:   ${SVXLINK_BUILD}")
message(STATUS "SvxLink version: ${SVXLINK_APP_VERSION}")

# La tasa interna del pipeline de audio de SvxLink no es CARD_SAMPLE_RATE:
# los dispositivos se abren a CARD_SAMPLE_RATE y el core trabaja a
# INTERNAL_SAMPLE_RATE. La obtenemos del build para no asumir 48 kHz.
set(SVX_INTERNAL_SAMPLE_RATE 16000 CACHE STRING "SvxLink INTERNAL_SAMPLE_RATE")
file(GLOB _svxlink_audio_flags
  "${SVXLINK_BUILD}/svxlink/modules/parrot/CMakeFiles/ModuleParrot.dir/flags.make")
if(_svxlink_audio_flags)
  file(READ "${_svxlink_audio_flags}" _svxlink_audio_flags_text)
  string(REGEX MATCH "INTERNAL_SAMPLE_RATE=([0-9]+)" _svxlink_rate_match
    "${_svxlink_audio_flags_text}")
  if(CMAKE_MATCH_1)
    set(SVX_INTERNAL_SAMPLE_RATE "${CMAKE_MATCH_1}")
  endif()
endif()
message(STATUS "SvxLink internal sample rate: ${SVX_INTERNAL_SAMPLE_RATE}")

find_package(PkgConfig REQUIRED)
pkg_check_modules(SIGC3 REQUIRED sigc++-3.0)
find_package(Threads REQUIRED)

add_library(ModuleRnsPartyline MODULE
  src/module/ModuleRnsPartyline.cpp
)

set_target_properties(ModuleRnsPartyline PROPERTIES
  PREFIX ""
  NO_SONAME 1
  OUTPUT_NAME "ModuleRnsPartyline"
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
)

target_include_directories(ModuleRnsPartyline PRIVATE
  "${SVXLINK_SRC}/src/svxlink/svxlink"
  "${SVXLINK_SRC}/src/svxlink/trx"
  "${SVXLINK_BUILD}/include"
  "${SVXLINK_BUILD}"
  "/usr/local/include/svxlink"
  ${SIGC3_INCLUDE_DIRS}
)

target_compile_options(ModuleRnsPartyline PRIVATE -Wall -Wpointer-arith)
target_compile_definitions(ModuleRnsPartyline PRIVATE
  INTERNAL_SAMPLE_RATE=${SVX_INTERNAL_SAMPLE_RATE})
target_link_libraries(ModuleRnsPartyline PRIVATE ${SIGC3_LIBRARIES} Threads::Threads)
