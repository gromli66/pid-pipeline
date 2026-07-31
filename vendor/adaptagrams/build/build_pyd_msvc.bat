@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d "%~dp0adaptagrams\cola"
set PYHOME=C:\Users\Maksim\AppData\Local\Programs\Python\Python311
cl /nologo /std:c++17 /EHsc /O2 /MD /DLIBAVOID_NO_DLL /DUSE_ASSERT_EXCEPTIONS /DSWIG_PYTHON_SILENT_MEMLEAK /D_USE_MATH_DEFINES /DNOMINMAX ^
  /I. /I"%PYHOME%\include" ^
  /LD adaptagrams_wrap.cxx /Fe_adaptagrams.pyd ^
  /link /LIBPATH:"%PYHOME%\libs" ^
  build_msvc\libavoid.lib build_msvc\libvpsc.lib build_msvc\libcola.lib build_msvc\libtopology.lib build_msvc\libdialect.lib 2>&1 | findstr /C:"error" /C:"warning C4996"
if exist _adaptagrams.pyd (echo PYD_OK) else (echo PYD_FAIL)
