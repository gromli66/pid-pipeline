@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d "%~dp0adaptagrams\cola"
set FLAGS=/nologo /std:c++17 /EHsc /O2 /MD /DNDEBUG /DLIBAVOID_NO_DLL /DUSE_ASSERT_EXCEPTIONS /D_USE_MATH_DEFINES /DNOMINMAX /I.
for %%L in (libvpsc libcola libtopology libdialect) do (
  if not exist build_msvc\%%L mkdir build_msvc\%%L
  echo ===== %%L =====
  cl %FLAGS% /c %%L\*.cpp /Fobuild_msvc\%%L\ 2>&1 | findstr /R /C:"error" /C:"====="
  lib /nologo /OUT:build_msvc\%%L.lib build_msvc\%%L\*.obj 2>&1 | findstr /R /C:"error"
  if exist build_msvc\%%L.lib (echo %%L LIB_OK) else (echo %%L LIB_FAIL)
)
