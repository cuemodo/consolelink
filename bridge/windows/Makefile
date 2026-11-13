# SmartSoft bridge shim: a 32-bit hid.dll for SmartSoft under CrossOver/Wine.
# Needs the mingw-w64 cross compiler (macOS: brew install mingw-w64).

CC      = i686-w64-mingw32-gcc
CFLAGS  = -O2 -std=c11 -Wall -Wextra -Wno-cast-function-type
# Windows 7 API level (InitOnceExecuteOnce needs Vista): set here, not left to the toolchain's default.
CPPFLAGS = -D_WIN32_WINNT=0x0601
# --kill-at: export HidD_GetHidGuid, not HidD_GetHidGuid@4 (SmartSoft imports by plain name).
LDFLAGS = -shared -static-libgcc -Wl,--kill-at
LIBS    = -lsetupapi -lws2_32

SRCS = shim.c backend_stub.c backend_tcp.c
OUT  = build/hid.dll

$(OUT): $(SRCS) bridge.h Makefile
	@mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $(SRCS) $(LIBS)

# Copies the DLL into the Python package, which ships it (src/consolelink/bridge_dll/, gitignored).
# The release workflow runs this before building the package.
REPO_ROOT := $(patsubst %/bridge/windows/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
PACKAGE_DLL = $(REPO_ROOT)/src/consolelink/bridge_dll/hid.dll

package: $(OUT)
	@mkdir -p $(dir $(PACKAGE_DLL))
	cp $(OUT) $(PACKAGE_DLL)

clean:
	rm -rf build $(PACKAGE_DLL)

.PHONY: package clean
