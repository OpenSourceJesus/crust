#ifndef _SHIM_LINUX_STRING_H
#define _SHIM_LINUX_STRING_H
#include <linux/kernel.h>
/* Prototypes only; the freestanding runtime supplies the definitions (mbos:
 * libmini, CrustOS: rlibc).
 *
 * This is the one asymmetry here that is not an oracle defect. gcc knows
 * memset, memcpy and friends as builtins and will accept a call to them with
 * no declaration in scope even under -nostdinc; ShivyCX has no builtins and
 * asks for the prototype, which is the stricter and more portable position.
 * kernel.h already includes this header for exactly that reason -- it just
 * had nothing to include. */
void *memset(void *s, int c, size_t n);
void *memcpy(void *dest, const void *src, size_t n);
void *memmove(void *dest, const void *src, size_t n);
int memcmp(const void *a, const void *b, size_t n);
void *memchr(const void *s, int c, size_t n);

size_t strlen(const char *s);
size_t strnlen(const char *s, size_t max);
char *strcpy(char *dest, const char *src);
char *strncpy(char *dest, const char *src, size_t n);
int strcmp(const char *a, const char *b);
int strncmp(const char *a, const char *b, size_t n);
char *strchr(const char *s, int c);
char *strrchr(const char *s, int c);
char *strstr(const char *haystack, const char *needle);
size_t strlcpy(char *dest, const char *src, size_t size);
size_t strscpy(char *dest, const char *src, size_t size);
#endif
