/* Minimal CrustOS guest: returns a checksum; built as a relocatable object
 * linked into a tiny ET_DYN / PIE static image for elf_load_path demos.
 *
 *   gcc -O0 -fPIC -nostdlib -shared -Wl,-e,guest_entry \
 *       examples/crustos/hello_guest.c -o build/crustos/hello_guest.so
 *
 * Or a plain object called as a function after load when linked with
 * -Wl,--entry=guest_entry and no libc (entry = guest_entry).
 */
int guest_entry(void) {
    int acc = 0;
    int i;
    for (i = 0; i < 100; i++)
        acc += i * 3;
    return acc % 256; /* 14850 % 256 == 2 */
}

/* Alias some toolchains expect. */
int _start(void) {
    return guest_entry();
}
