#ifndef MEDIA_MICROSERVICES_SPIN_DELAY_H
#define MEDIA_MICROSERVICES_SPIN_DELAY_H

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <mutex>

namespace media_service {

inline int64_t ParsePositiveDelayMicros(const char *raw_value) {
  if (!raw_value || raw_value[0] == '\0') {
    return 0;
  }
  errno = 0;
  char *end = nullptr;
  long long parsed = std::strtoll(raw_value, &end, 10);
  if (errno != 0 || end == raw_value || *end != '\0' || parsed <= 0) {
    return 0;
  }
  return static_cast<int64_t>(parsed);
}

inline int64_t GetConfiguredSpinDelayMicros(const char *service_env_name) {
  int64_t delay = ParsePositiveDelayMicros(std::getenv(service_env_name));
  if (delay > 0) {
    return delay;
  }
  return ParsePositiveDelayMicros(std::getenv("FANOUT_TARGET_SPIN_US"));
}

inline uint64_t SaturatingMultiply(uint64_t lhs, uint64_t rhs) {
  if (lhs == 0 || rhs == 0) {
    return 0;
  }
  if (lhs > std::numeric_limits<uint64_t>::max() / rhs) {
    return std::numeric_limits<uint64_t>::max();
  }
  return lhs * rhs;
}

inline void RunSyntheticWorkIterations(uint64_t iterations) {
  static thread_local uint64_t state = 0x4d595df4d0f33173ULL;
  for (uint64_t i = 0; i < iterations; ++i) {
    state += 0x9e3779b97f4a7c15ULL + i;
    state ^= state >> 30;
    state *= 0xbf58476d1ce4e5b9ULL;
    state ^= state >> 27;
    state *= 0x94d049bb133111ebULL;
    state ^= state >> 31;
  }
  std::atomic_signal_fence(std::memory_order_seq_cst);
}

inline uint64_t CalibrateIterationsPerMicrosecond() {
  constexpr uint64_t kWarmupIterations = 1ULL << 14;
  constexpr uint64_t kInitialIterations = 1ULL << 16;
  constexpr int64_t kTargetCalibrationMicros = 2000;

  RunSyntheticWorkIterations(kWarmupIterations);

  uint64_t iterations = kInitialIterations;
  for (int attempt = 0; attempt < 8; ++attempt) {
    auto start = std::chrono::steady_clock::now();
    RunSyntheticWorkIterations(iterations);
    auto elapsed_us =
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - start)
            .count();
    if (elapsed_us > 0) {
      uint64_t calibrated = static_cast<uint64_t>(iterations / elapsed_us);
      if (calibrated == 0) {
        calibrated = 1;
      }
      if (elapsed_us >= kTargetCalibrationMicros) {
        return calibrated;
      }
    }
    iterations <<= 1;
  }

  return 256;
}

inline uint64_t GetIterationsPerMicrosecond() {
  static std::once_flag once;
  static uint64_t iterations_per_us = 0;
  std::call_once(once, []() {
    iterations_per_us = CalibrateIterationsPerMicrosecond();
  });
  return iterations_per_us;
}

inline void SpinDelayMicros(int64_t delay_micros) {
  if (delay_micros <= 0) {
    return;
  }
  uint64_t iterations = SaturatingMultiply(
      static_cast<uint64_t>(delay_micros), GetIterationsPerMicrosecond());
  RunSyntheticWorkIterations(iterations);
}

inline void SpinDelayIfConfigured(const char *service_env_name) {
  SpinDelayMicros(GetConfiguredSpinDelayMicros(service_env_name));
}

}  // namespace media_service

#endif  // MEDIA_MICROSERVICES_SPIN_DELAY_H
