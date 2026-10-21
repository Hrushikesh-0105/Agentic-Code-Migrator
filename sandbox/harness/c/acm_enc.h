/* Encoder of the generated C test driver (sandbox/drivers_c.py): prints runner.py's type-tagged JSON on stdout. */
#ifndef ACM_ENC_H
#define ACM_ENC_H
#include <math.h>
#include <stdio.h>
#include <string.h>

static void acm_q(const char *s) {
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        switch (*p) {
            case '"': fputs("\\\"", stdout); break;
            case '\\': fputs("\\\\", stdout); break;
            case '\n': fputs("\\n", stdout); break;
            case '\r': fputs("\\r", stdout); break;
            case '\t': fputs("\\t", stdout); break;
            default:
                if (*p < 0x20) printf("\\u%04x", *p);
                else putchar(*p);
        }
    }
    putchar('"');
}
static void acm_begin(void) { printf("__ACMOUT_BEGIN__\n"); fflush(stdout); }
static void acm_result_begin(void) { fflush(stdout); printf("\n__ACMOUT_END__\n__HARNESS_RESULT__{\"status\": \"ok\", \"value\": "); }
static void acm_after_begin(void) { printf(", \"args_after\": ["); }
static void acm_sep(void) { printf(", "); }
static void acm_end(void) { printf("], \"self_after\": null}\n"); fflush(stdout); }
static void acm_enc_none(void) { printf("{\"t\": \"none\"}"); }
static void acm_enc_int(long long v) { printf("{\"t\": \"int\", \"v\": \"%lld\"}", v); }
static void acm_enc_uint(unsigned long long v) { printf("{\"t\": \"int\", \"v\": \"%llu\"}", v); }
static void acm_enc_bool(int v) { printf("{\"t\": \"bool\", \"v\": %s}", v ? "true" : "false"); }
static void acm_enc_float(double v) {
    if (isnan(v)) { printf("{\"t\": \"float\", \"v\": \"nan\"}"); return; }
    if (isinf(v)) { printf("{\"t\": \"float\", \"v\": \"%s\"}", v > 0 ? "inf" : "-inf"); return; }
    char buf[64];
    snprintf(buf, sizeof buf, "%.17g", v);
    if (!strpbrk(buf, ".eE")) strcat(buf, ".0");
    printf("{\"t\": \"float\", \"v\": %s}", buf);
}
static void acm_enc_str(const char *s) {
    if (!s) { acm_enc_none(); return; }
    printf("{\"t\": \"text\", \"v\": "); acm_q(s); putchar('}');
}
static void acm_enc_char(char c) { char b[2] = {c, 0}; acm_enc_str(b); }
static void acm_enc_repr(const char *type) { printf("{\"t\": \"repr\", \"v\": \"<%s>\"}", type); }
#endif
