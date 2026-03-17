#ifndef SOCIAL_NETWORK_MICROSERVICES_SPIN_DELAY_H
#define SOCIAL_NETWORK_MICROSERVICES_SPIN_DELAY_H

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <sys/stat.h>

namespace social_network {

inline bool IsAsciiWhitespace(char ch) {
  return ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r' || ch == '\f' ||
         ch == '\v';
}

inline int64_t ParsePositiveDelayMicros(const char *raw_value) {
  if (!raw_value || raw_value[0] == '\0') {
    return 0;
  }
  errno = 0;
  char *end = nullptr;
  long long parsed = std::strtoll(raw_value, &end, 10);
  while (end && *end != '\0' && IsAsciiWhitespace(*end)) {
    ++end;
  }
  if (errno != 0 || end == raw_value || (end && *end != '\0') || parsed <= 0) {
    return 0;
  }
  return static_cast<int64_t>(parsed);
}

inline const char *GetSpinOverrideDirectory() {
  const char *override_dir = std::getenv("MCOZ_SPIN_OVERRIDE_DIR");
  if (override_dir && override_dir[0] != '\0') {
    return override_dir;
  }
  return "/tmp/mcoz-spin-overrides";
}

inline bool SameFileState(const struct stat &lhs, const struct stat &rhs) {
  return lhs.st_dev == rhs.st_dev && lhs.st_ino == rhs.st_ino &&
         lhs.st_size == rhs.st_size && lhs.st_mtim.tv_sec == rhs.st_mtim.tv_sec &&
         lhs.st_mtim.tv_nsec == rhs.st_mtim.tv_nsec;
}

inline int64_t ReadSpinDelayOverrideMicros(const char *service_env_name) {
  struct OverrideCache {
    std::string service_env_name;
    std::string path;
    bool exists = false;
    bool has_stat = false;
    struct stat last_stat = {};
    int64_t last_value = 0;
  };

  static thread_local OverrideCache cache;
  if (cache.service_env_name != service_env_name) {
    cache = OverrideCache{};
    cache.service_env_name = service_env_name;
    cache.path = std::string(GetSpinOverrideDirectory()) + "/" + service_env_name;
  }

  struct stat current_stat = {};
  if (stat(cache.path.c_str(), &current_stat) != 0) {
    cache.exists = false;
    cache.has_stat = false;
    cache.last_value = 0;
    return 0;
  }

  if (cache.exists && cache.has_stat && SameFileState(cache.last_stat, current_stat)) {
    return cache.last_value;
  }

  FILE *handle = std::fopen(cache.path.c_str(), "r");
  if (!handle) {
    cache.exists = false;
    cache.has_stat = false;
    cache.last_value = 0;
    return 0;
  }

  char buffer[64] = {0};
  const char *line = std::fgets(buffer, sizeof(buffer), handle);
  std::fclose(handle);

  cache.exists = true;
  cache.has_stat = true;
  cache.last_stat = current_stat;
  cache.last_value = ParsePositiveDelayMicros(line);
  return cache.last_value;
}

inline int64_t GetConfiguredSpinDelayMicros(const char *service_env_name) {
  int64_t delay = ReadSpinDelayOverrideMicros(service_env_name);
  if (delay > 0) {
    return delay;
  }
  delay = ParsePositiveDelayMicros(std::getenv(service_env_name));
  if (delay > 0) {
    return delay;
  }
  return ParsePositiveDelayMicros(std::getenv("FANOUT_TARGET_SPIN_US"));
}

inline void RunSyntheticWorkTick(uint64_t iteration) {
  static thread_local uint64_t state = 0x4d595df4d0f33173ULL;
  state += 0x9e3779b97f4a7c15ULL + iteration;
  state ^= state >> 30;
  state *= 0xbf58476d1ce4e5b9ULL;
  state ^= state >> 27;
  state *= 0x94d049bb133111ebULL;
  state ^= state >> 31;
  std::atomic_signal_fence(std::memory_order_seq_cst);
}

inline void SpinDelayMicros(int64_t delay_micros) {
  if (delay_micros <= 0) {
    return;
  }
  auto deadline =
      std::chrono::steady_clock::now() + std::chrono::microseconds(delay_micros);
  uint64_t iteration = 0;
  while (std::chrono::steady_clock::now() < deadline) {
    RunSyntheticWorkTick(iteration++);
  }
}

inline void SpinDelayIfConfigured(const char *service_env_name) {
  SpinDelayMicros(GetConfiguredSpinDelayMicros(service_env_name));
}

inline int64_t GetConfiguredSpinPercent(const char *service_env_name) {
  (void)service_env_name;
  return 0;
}

inline void SpinDelayPercentOfElapsedMicros(const char *service_env_name,
                                            int64_t elapsed_micros) {
  (void)service_env_name;
  (void)elapsed_micros;
}

}  // namespace social_network

#endif  // SOCIAL_NETWORK_MICROSERVICES_SPIN_DELAY_H
