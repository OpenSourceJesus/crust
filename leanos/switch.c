/* LeanOS: the switcher's decisions, in C.
 *
 * BAREMETAL_THREADS.md computes each thread's register footprint from the
 * whole-program call graph and splits the callee-saved range x19..x28 into
 * a `left` and a `right` budget, disjoint.  The switcher saves what the
 * partition says.  The decisions it makes -- which side a register belongs
 * to, where that register's saved value lives, how many a side saves, which
 * thread runs next -- are integer functions of the partition, and *those*
 * are what this file is: every function here is arithmetic and guards, so
 * `shivyc/ilproof.py` lifts it from the compiled IL and the theorems are
 * about the binary, not a transcription.
 *
 * The saving itself -- the stores and loads of register values into the
 * thread's slots -- is memory access, and the lift refuses it today.  It is
 * exactly the access `memmap.py` was built to make liftable: each slot is a
 * field of a region the list names.  That is the next step, not this one.
 *
 * The partition here is the first configuration: left = x19..x24 (six
 * registers, the IO thread's footprint in the measured case), right =
 * x25..x28.  It is constants rather than a parameter because the contract
 * front end takes `len(x)` bounds only, and a `split` parameter would need
 * `assert split >= 19` to be a contract; that is a compiler item.
 */

#define REG_LO    19    /* first callee-saved register, x19 */
#define REG_SPLIT 25    /* first register of the right side  */
#define REG_HI    29    /* one past the last, x28            */

#define SIDE_LEFT  0
#define SIDE_RIGHT 1
#define SIDE_NONE  2    /* not an allocatable register       */

/* Which side owns register r.  The guards are the theorem: a register on
 * the left is below the split because that is what was tested. */
int reg_side(int r) {
    if (r < REG_LO)    return SIDE_NONE;
    if (r < REG_SPLIT) return SIDE_LEFT;
    if (r < REG_HI)    return SIDE_RIGHT;
    return SIDE_NONE;
}

/* The slot in its side's save area where register r is kept: registers are
 * numbered from the side's first, so two sides' slots start at zero. */
int save_slot(int r) {
    if (r < REG_SPLIT) return r - REG_LO;
    return r - REG_SPLIT;
}

/* How many registers a side saves on a switch. */
int saves(int side) {
    if (side == SIDE_LEFT)  return REG_SPLIT - REG_LO;
    if (side == SIDE_RIGHT) return REG_HI - REG_SPLIT;
    return 0;
}

/* The thread that runs after `cur`: two threads, one core, alternating. */
int next_thread(int cur) {
    if (cur == 0) return 1;
    return 0;
}
