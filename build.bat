@echo off
echo MemCell Build Script
echo ====================

set GXX=C:\msys64\mingw64\bin\g++.exe
set CXXFLAGS=-std=c++17 -O2 -Wall -Wextra -Iinclude -static
set SRCDIR=src
set SOURCES=%SRCDIR%\utils.cpp %SRCDIR%\cell.cpp %SRCDIR%\analyzer.cpp %SRCDIR%\chunker.cpp %SRCDIR%\dedup.cpp %SRCDIR%\pattern_codec.cpp %SRCDIR%\delta.cpp %SRCDIR%\lz_compress.cpp %SRCDIR%\entropy.cpp %SRCDIR%\shuffle.cpp %SRCDIR%\bitpack.cpp %SRCDIR%\file_format.cpp %SRCDIR%\memalgo.cpp

if not exist build mkdir build

echo Building memalgo.exe...
C:\msys64\usr\bin\bash.exe -lc "cd /c/memalgo && /mingw64/bin/g++ -std=c++17 -O2 -Wall -Wextra -Iinclude src/utils.cpp src/cell.cpp src/analyzer.cpp src/chunker.cpp src/dedup.cpp src/pattern_codec.cpp src/delta.cpp src/lz_compress.cpp src/entropy.cpp src/shuffle.cpp src/bitpack.cpp src/file_format.cpp src/memalgo.cpp src/main.cpp -o build/memalgo.exe -static"
if errorlevel 1 goto :fail

echo Building memalgo_test.exe...
C:\msys64\usr\bin\bash.exe -lc "cd /c/memalgo && /mingw64/bin/g++ -std=c++17 -O2 -Wall -Wextra -Iinclude src/utils.cpp src/cell.cpp src/analyzer.cpp src/chunker.cpp src/dedup.cpp src/pattern_codec.cpp src/delta.cpp src/lz_compress.cpp src/entropy.cpp src/shuffle.cpp src/bitpack.cpp src/file_format.cpp src/memalgo.cpp tests/test_roundtrip.cpp -o build/memalgo_test.exe -static"
if errorlevel 1 goto :fail

echo.
echo Build Complete!
echo   build\memalgo.exe      - Main executable
echo   build\memalgo_test.exe - Test runner
exit /b 0

:fail
echo BUILD FAILED
exit /b 1
