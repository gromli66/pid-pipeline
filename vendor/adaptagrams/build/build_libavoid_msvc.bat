@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d "%~dp0adaptagrams\cola"
if not exist build_msvc mkdir build_msvc
cl /nologo /std:c++17 /EHsc /O2 /MD /DNDEBUG /DLIBAVOID_NO_DLL /D_USE_MATH_DEFINES /DNOMINMAX /I. /c libavoid\*.cpp /Fobuild_msvc\ 2>&1
if errorlevel 1 exit /b 1
lib /nologo /OUT:build_msvc\libavoid.lib build_msvc\*.obj
echo BUILD_OK
