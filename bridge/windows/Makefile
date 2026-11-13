# SmartSoft bridge shim: a 32-bit hid.dll for SmartSoft under CrossOver/Wine.
# Needs the mingw-w64 cross compiler (macOS: brew install mingw-w64).

CC      = i686-w64-mingw32-gcc
CFLAGS  = -O2 -std=c11 -Wall -Wextra -Wno-cast-function-type
# --kill-at: export HidD_GetHidGuid, not HidD_GetHidGuid@4 (SmartSoft imports by plain name).
LDFLAGS = -shared -static-libgcc -Wl,--kill-at
LIBS    = -lsetupapi -lws2_32

SRCS = shim.c backend_stub.c backend_tcp.c
OUT  = build/hid.dll

$(OUT): $(SRCS) bridge.h Makefile
	@mkdir -p build
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(SRCS) $(LIBS)

clean:
	rm -rf build

.PHONY: clean
