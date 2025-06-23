#include "perf.h"
#include "../libcoz/perf.h"
#include <linux/perf_event.h>
#include <sys/poll.h>
#include <unistd.h>
#include <vector>
#include <iostream>

using namespace std::chrono;

static std::vector<int> online_cpus() {
    int n = sysconf(_SC_NPROCESSORS_ONLN);
    std::vector<int> cpus;
    for(int i=0;i<n;i++) cpus.push_back(i);
    return cpus;
}

int perf_sampler_sync(int cg_fd, milliseconds period, double delta,
                      const std::vector<cgroup>& others, const std::string& mode) {
    (void)period; (void)delta; (void)others; (void)mode;

    struct perf_event_attr attr{};
    attr.type = PERF_TYPE_HARDWARE;
    attr.config = PERF_COUNT_HW_INSTRUCTIONS;
    attr.sample_period = 1000;
    attr.sample_type = PERF_SAMPLE_IP;
    attr.wakeup_events = 1;

    std::vector<perf_event> events;
    std::vector<struct pollfd> fds;
    for(int cpu : online_cpus()) {
        perf_event pe(attr, cg_fd, cpu, PERF_FLAG_PID_CGROUP|PERF_FLAG_FD_CLOEXEC);
        pe.start();
        fds.push_back({static_cast<int>(pe.get_fd()), POLLIN, 0});
        events.push_back(std::move(pe));
    }

    while(true) {
        int ret = poll(fds.data(), fds.size(), 1000);
        if(ret <= 0) continue;
        for(size_t i=0;i<fds.size();i++) {
            if(fds[i].revents & POLLIN) {
                std::cerr << "cpu " << i << " -> activity detected" << std::endl;
            }
        }
    }
    return 0;
}
