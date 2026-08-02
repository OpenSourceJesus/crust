/* scene_host.c -- run scene.c hosted and print its summary.
 *
 * The other half of the comparison; the bare-metal half is
 * ../../baremetal/kernel_mingine.c. Both compile the same scene.c with the
 * same compiler, so any difference between the two lines of output is a
 * difference between running as a process and running on the metal, which is
 * exactly what the test is looking for.
 *
 * Output is four numbers on stdout and nothing else, so it can be diffed
 * directly against what the kernel writes to serial.
 */
#include "scene.c"

int printf(const char *, ...);

int main(void) {
    unsigned int sum;

    sum = scene_render();

    printf("scene %dx%d frames %d\n", scene_width(), scene_height(), 24);
    printf("ball %d,%d\n", scene_ball_x(), scene_ball_y());
    printf("foe %d,%d\n", scene_foe_x(), scene_foe_y());
    printf("score %d\n", scene_score());
    printf("pixels %08x\n", sum);
    return 0;
}
