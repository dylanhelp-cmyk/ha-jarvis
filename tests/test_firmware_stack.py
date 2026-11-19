"""Exercise the production lifetime stack minimum across task readers."""
import unittest

import test_firmware_fault_history as helpers


class FirmwareStackTests(unittest.TestCase):
    compile_and_run = helpers.FirmwareFaultHistoryTests.compile_and_run

    def test_stack_minimum_is_monotonic_and_zero_is_a_real_measurement(self):
        self.compile_and_run(r'''
#include <cassert>
#include <thread>
#include "task_stack.h"
using namespace esphome::jarvis_stream;
int main() {
  TaskStackMinimum minimum;
  assert(minimum.value() == TaskStackMinimum::UNKNOWN);
  std::thread first([&] { for (uint32_t n = 8000; n >= 1000; --n) minimum.observe(n); });
  std::thread second([&] { for (uint32_t n = 7000; n >= 500; --n) minimum.observe(n); });
  uint32_t previous = minimum.value();
  for (int i = 0; i < 10000; ++i) {
    const auto current = minimum.value();
    assert(current <= previous);
    previous = current;
  }
  first.join(); second.join();
  assert(minimum.value() == 500);
  minimum.observe(0);
  minimum.observe(8000);
  assert(minimum.value() == 0);
  TaskStackMinimum independent;
  assert(independent.value() == TaskStackMinimum::UNKNOWN);
}
''')
