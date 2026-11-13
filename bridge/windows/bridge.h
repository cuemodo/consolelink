/*
 * SmartSoft bridge -- shared declarations for the shim (shim.c) and its backends.
 *
 * A backend is where the fake ETC device's bulk transfers go: the built-in stub console
 * (backend_stub.c, for testing without hardware) or ConsoleLink on the Mac over TCP.
 */
#ifndef SMARTSOFT_BRIDGE_H
#define SMARTSOFT_BRIDGE_H

void bridge_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void bridge_log_hex(const char *tag, const void *buf, unsigned n);

/* What the ETC driver reports about the device (its identification IOCTLs). */
typedef struct {
    unsigned short vid, pid, bcd;
    char product[64];
} bridge_identity;

typedef struct bridge_session bridge_session;

struct bridge_backend {
    const char *name;
    /* Is a console reachable right now? Decides whether the fake devices are listed. */
    int (*present)(void);
    /* One session per opened handle; NULL if the console is not reachable. */
    bridge_session *(*open)(bridge_identity *id);
    void (*close)(bridge_session *s);
    /* One call = one bulk transfer. write: bytes written, or -1 on error.
       read: bytes read (> 0), 0 on timeout, or -1 on error. */
    int (*write)(bridge_session *s, const void *buf, unsigned n);
    int (*read)(bridge_session *s, void *buf, unsigned n, unsigned timeout_ms);
};

extern const struct bridge_backend stub_backend;
extern const struct bridge_backend tcp_backend;

/* SMARTSOFT_BRIDGE after "tcp": "", ":PORT" or ":HOST:PORT". */
void tcp_backend_configure(const char *spec);
const char *tcp_backend_address(void);

#endif
