# Fanout Spin Manual

This file is kept as a compatibility pointer.

The old percentage-based `*_SPIN_PCT` mode was removed. Fanout delay is now configured only with `*_SPIN_US`, and each value is converted into calibrated synthetic CPU work rather than wall-clock spinning.

Use [`WORK_DELAY.md`](./WORK_DELAY.md) for the current build, Docker Compose, Helm, and verification steps.
