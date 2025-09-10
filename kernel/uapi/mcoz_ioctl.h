#ifndef _UAPI_MCOZ_IOCTL_H
#define _UAPI_MCOZ_IOCTL_H

#include <linux/ioctl.h>
#include <linux/types.h>

#define MCOZ_IOC_MAGIC 'm'

struct mcoz_delay_req {
    __u64 ns;     /* 점유 시간 (나노초) */
    __u32 flags;  /* bit0: block_bh(소프트IRQ 지연) */
    __u32 pad;
};

#define MCOZ_IOC_DELAY _IOW(MCOZ_IOC_MAGIC, 1, struct mcoz_delay_req)

#endif
