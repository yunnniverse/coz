#ifndef HOSTCTL_REQUEST_CREDIT_H
#define HOSTCTL_REQUEST_CREDIT_H

#include <cstdint>
#include <string>

struct RequestArmRequest {
    std::string ns;
    std::string pod;
    std::string container;
    std::string source;
    uint64_t delay_ns;
    int64_t count;
};

struct SyscallProfileRequest {
    std::string ns;
    std::string pod;
    std::string container;
    uint64_t duration_ms;
    uint32_t top_k;
    bool apply_policy;
};

struct ConsumePolicyApplyRequest {
    std::string ns;
    std::string pod;
    std::string container;
    uint32_t raw_flags;
};

bool request_credit_start(uint64_t default_delay_ns,
                          bool refund_on_fail,
                          bool enable_read_hook,
                          std::string& err);
void request_credit_stop();
bool request_credit_is_active();
bool request_credit_arm(const RequestArmRequest& req, std::string& response_json);
bool request_credit_profile_syscalls(const SyscallProfileRequest& req, std::string& response_json);
bool request_credit_apply_consume_policy(const ConsumePolicyApplyRequest& req, std::string& response_json);
std::string request_credit_status_json();
void request_credit_clear(bool clear_credits);
std::string request_credit_rearm_message();

#endif  // HOSTCTL_REQUEST_CREDIT_H
