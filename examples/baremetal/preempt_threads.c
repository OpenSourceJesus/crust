/* Thread declarations for kernel_preempt.c, alongside it.
 *
 * The left/right partition is a whole-program property, so it is declared in
 * a `main` ShivyCX can see. The bare-metal image has no main -- boot_arm64.S
 * calls kmain -- so this file exists only to carry the assertions, and is not
 * linked into the image. */
void worker_left(void);
void worker_right(void);

int main()
assert worker_left in threads.left( core=0 )
assert worker_right in threads.right( core=0 )
{
    worker_left();
    worker_right();
    return 0;
}
