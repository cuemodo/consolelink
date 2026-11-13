/*
 * SmartSoft bridge -- stub backend: a minimal fake console, for testing the shim without
 * hardware (SMARTSOFT_BRIDGE=stub).
 *
 * It only keeps SmartSoft's poll loop alive: each idle poll header (msgType 0 or 2) gets the
 * console's idle reply, a 12-byte header with msgType 4, payloadLen 0 and state [0,0,0,0].
 * Requests (msgType 1 + payload) are accepted and never answered, so SmartSoft opens the
 * device and polls but never finishes its connect-time download.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdlib.h>
#include <string.h>
#include "bridge.h"

struct bridge_session {
    CRITICAL_SECTION cs;
    unsigned char reply[12];
    int has_reply;
    unsigned pending_payload;   /* bytes of a msgType 1 payload still to come */
    unsigned long polls;
};

static int stub_present(void)
{
    return 1;
}

static bridge_session *stub_open(bridge_identity *id)
{
    bridge_session *s = calloc(1, sizeof(*s));
    if (!s)
        return NULL;
    InitializeCriticalSection(&s->cs);
    id->vid = 0x14d5;
    id->pid = 0x0201;
    id->bcd = 0x0001;
    strcpy(id->product, "ETC Smartfade");
    return s;
}

static void stub_close(bridge_session *s)
{
    bridge_log("stub: closed after %lu polls", s->polls);
    DeleteCriticalSection(&s->cs);
    free(s);
}

static int stub_write(bridge_session *s, const void *buf, unsigned n)
{
    const unsigned char *b = buf;
    EnterCriticalSection(&s->cs);
    if (s->pending_payload) {
        s->pending_payload = 0;   /* the payload write that follows a msgType 1 header */
    } else if (n == 12) {
        unsigned type = b[0] | b[1] << 8, len = b[2] | b[3] << 8;
        if (type == 1) {
            s->pending_payload = len;
        } else if (type == 0 || type == 2) {
            memset(s->reply, 0, sizeof(s->reply));
            s->reply[0] = 4;
            s->has_reply = 1;
            s->polls++;
        }
    }
    LeaveCriticalSection(&s->cs);
    return (int)n;
}

static int stub_read(bridge_session *s, void *buf, unsigned n, unsigned timeout_ms)
{
    EnterCriticalSection(&s->cs);
    if (s->has_reply && n >= sizeof(s->reply)) {
        memcpy(buf, s->reply, sizeof(s->reply));
        s->has_reply = 0;
        LeaveCriticalSection(&s->cs);
        return sizeof(s->reply);
    }
    LeaveCriticalSection(&s->cs);
    Sleep(timeout_ms < 20 ? timeout_ms : 20);
    return 0;
}

const struct bridge_backend stub_backend = {
    "stub", stub_present, stub_open, stub_close, stub_write, stub_read,
};
