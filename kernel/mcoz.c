// SPDX-License-Identifier: GPL-2.0
#include <linux/module.h>
#include <linux/miscdevice.h>
#include <linux/fs.h>
#include <linux/uaccess.h>
#include <linux/ktime.h>
#include <linux/preempt.h>
#include <linux/capability.h>
#include <linux/smp.h>
#include <linux/interrupt.h>
#include <asm/processor.h> /* cpu_relax */
#include "uapi/mcoz_ioctl.h"

/* Module version for visibility and rollout control */
#define MCOZ_VERSION "3.0.0"

/* 안전 상한: 처음엔 보수적으로. 필요시 조정 */
// #define MCOZ_MAX_HOG_NS (1000000ULL) /* 1ms */
#define MCOZ_DEV_MODE   0666         /* 운영에선 0660 + group 권장 */

static inline void mcoz_hog_on_this_cpu(u64 ns, bool block_bh)
{
    u64 start = ktime_get_ns();

    preempt_disable();
    migrate_disable();
    if (block_bh)
        local_bh_disable();

    for (;;) {
        cpu_relax();
        if (ktime_get_ns() - start >= ns)
            break;
    }

    if (block_bh)
        local_bh_enable();
    migrate_enable();
    preempt_enable();
}

static long mcoz_ioctl(struct file *filp, unsigned int cmd, unsigned long arg)
{
    struct mcoz_delay_req req;

    if (cmd != MCOZ_IOC_DELAY)
        return -ENOTTY;

    if (copy_from_user(&req, (void __user *)arg, sizeof(req)))
        return -EFAULT;

    if (!req.ns)  // || req.ns > MCOZ_MAX_HOG_NS)
        return -EINVAL;

    /* 과도 사용 방지: 권한 요구(컨테이너엔 SYS_NICE 부여 권장) */
    if (!capable(CAP_SYS_NICE) && !capable(CAP_SYS_ADMIN))
        return -EPERM;

    mcoz_hog_on_this_cpu(req.ns, req.flags & 0x1);
    return 0;
}

static const struct file_operations mcoz_fops = {
    .owner          = THIS_MODULE,
    .unlocked_ioctl = mcoz_ioctl,
#ifdef CONFIG_COMPAT
    .compat_ioctl   = mcoz_ioctl,
#endif
};

static struct miscdevice mcoz_dev = {
    .minor = MISC_DYNAMIC_MINOR,
    .name  = "mcoz",
    .fops  = &mcoz_fops,
    .mode  = MCOZ_DEV_MODE,
};

static int __init mcoz_init(void)
{
    pr_info("mcoz: init version %s (no ns upper limit)\n", MCOZ_VERSION);
    return misc_register(&mcoz_dev);
}
static void __exit mcoz_exit(void)
{
    pr_info("mcoz: exit version %s\n", MCOZ_VERSION);
    misc_deregister(&mcoz_dev);
}
module_init(mcoz_init);
module_exit(mcoz_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("you");
MODULE_DESCRIPTION("Per-CPU busy delay via ioctl");
MODULE_VERSION(MCOZ_VERSION);
