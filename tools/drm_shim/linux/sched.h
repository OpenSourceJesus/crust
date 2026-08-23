#ifndef _SHIM_LINUX_SCHED_H
#define _SHIM_LINUX_SCHED_H
#include <linux/kernel.h>
/* Scheduler primitives, for a system with no scheduler.
 *
 * in_atomic() reports 0 -- "not in atomic context" -- which is the answer
 * that lets callers take the sleeping path. Since nothing here can actually
 * sleep, the sleeping path degenerates to the non-sleeping one anyway, so
 * either answer works; 0 is chosen because it is what a plain kernel thread
 * would report and so keeps the common branch live.
 *
 * cond_resched() is a genuine no-op: there is nothing to reschedule to. A
 * long-running loop that relies on it to yield will simply not yield. */
#define in_atomic()             (0)
#define in_interrupt()          (0)
#define in_irq()                (0)
#define cond_resched()          (0)
#define schedule()              do { } while (0)
#define set_current_state(s)    do { } while (0)
#define signal_pending(t)       (0)
#define TASK_RUNNING            0
#define TASK_INTERRUPTIBLE      1
#define TASK_UNINTERRUPTIBLE    2
struct task_struct { int unused; };
#endif
