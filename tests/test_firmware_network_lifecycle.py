"""Run the production worker lifecycle on the host; never contacts a device."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "ha_jarvis/firmware/components/jarvis_stream"


def compiler_command():
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler:
        return [compiler]
    if importlib.util.find_spec("ziglang"):
        return [sys.executable, "-m", "ziglang", "c++"]
    return None


class FirmwareNetworkLifecycleTests(unittest.TestCase):
    def test_production_lifecycle_with_stub_network_driver(self):
        compiler = compiler_command()
        if compiler is None:
            self.skipTest("A host C++ compiler is required for firmware worker regression tests")
        source = r'''
#include <cassert>
#include <cstdio>
#include "network_lifecycle.h"
using namespace esphome::jarvis_stream;
using A = NetworkAction;
struct Driver {
  NetworkLifecycle lifecycle;
  unsigned init = 0, start = 0, stop = 0, destroy = 0;
  A tick(bool wifi, bool reset, bool shutdown, unsigned time, bool success=true) {
    const A action = lifecycle.next(wifi, reset, shutdown, time);
    if (action == A::INITIALIZE) { ++init; lifecycle.initialized(success, time); }
    if (action == A::START) { ++start; lifecycle.started(success, time); }
    if (action == A::STOP) { ++stop; lifecycle.stopped(time); }
    if (action == A::DESTROY) { ++destroy; lifecycle.destroyed(); }
    return action;
  }
};
int main() {
  // Boot without Wi-Fi never initializes, starts, stops or destroys a client.
  Driver boot;
  for (unsigned t=0;t<60000;t+=10) assert(boot.tick(false,false,false,t)==A::WAIT);
  assert(boot.tick(false,false,true,60000)==A::EXIT);
  assert(boot.init==0 && boot.start==0 && boot.stop==0 && boot.destroy==0);

  Driver d;
  assert(d.tick(true,false,false,0)==A::INITIALIZE);
  assert(d.tick(true,false,false,10)==A::START);
  // Endpoint offline/clean close/connection loss: the library owns reconnect, not a five-second stop loop.
  for (unsigned t=20;t<60000;t+=10) assert(d.tick(true,false,false,t)==A::WAIT);
  assert(d.start==1 && d.stop==0);

  // Wi-Fi-down requests reset, including a fast flap where old TCP still appears connected.
  assert(d.tick(false,true,false,60000)==A::STOP);
  assert(d.tick(false,false,false,65000)==A::WAIT);
  assert(d.tick(true,false,false,65000)==A::START);
  assert(d.start==2 && d.stop==1);
  assert(d.tick(true,false,false,70000)==A::WAIT);
  // Protocol error and unexpected task exit are recovered by the worker with bounded retry rate.
  assert(d.tick(true,true,false,70010)==A::STOP);
  assert(d.tick(true,false,false,75009)==A::WAIT);
  assert(d.tick(true,false,false,75010)==A::START);
  assert(d.tick(true,false,false,80010)==A::WAIT);
  // Shutdown requests are worker actions; no double-stop or destroy-before-initialization.
  assert(d.tick(false,false,true,80020)==A::STOP);
  assert(d.tick(false,false,true,80030)==A::DESTROY);
  assert(d.tick(false,false,true,80040)==A::EXIT);

  Driver failed;
  assert(failed.tick(true,false,false,0)==A::INITIALIZE);
  assert(failed.tick(true,false,false,10,false)==A::START);
  assert(failed.tick(true,false,false,5009)==A::WAIT);
  assert(failed.tick(false,false,false,5010)==A::WAIT);
  assert(failed.tick(true,false,false,5010)==A::START);
  assert(failed.stop==0); // failed start must never provoke stop-before-start

  Driver init_failed;
  assert(init_failed.tick(true,false,false,0,false)==A::INITIALIZE);
  assert(init_failed.tick(true,false,false,4999)==A::WAIT);
  assert(init_failed.tick(true,false,false,5000)==A::INITIALIZE);
  assert(init_failed.tick(false,false,true,5010)==A::DESTROY);
  assert(init_failed.tick(false,false,true,5020)==A::EXIT);
  std::puts("no-WiFi, offline, flap, reset, start failure and shutdown regressions passed");
}
'''
        with tempfile.TemporaryDirectory(prefix="jarvis-network-test-") as folder:
            tmp = Path(folder)
            cpp, exe = tmp / "lifecycle.cpp", tmp / "lifecycle.exe"
            cpp.write_text(source)
            subprocess.run([*compiler, "-std=c++17", "-I", str(COMPONENT), str(cpp), "-o", str(exe)],
                           check=True, capture_output=True, timeout=180)
            result = subprocess.run([str(exe)], check=True, capture_output=True, text=True, timeout=10)
        self.assertIn("regressions passed", result.stdout)

    def test_main_loop_shutdown_and_json_enqueue_do_not_call_websocket(self):
        source = (COMPONENT / "jarvis_stream.cpp").read_text()
        for function in ("void JarvisStream::loop()", "void JarvisStream::on_shutdown()", "bool JarvisStream::send_json_("):
            start = source.index("{", source.index(function))
            depth, end = 1, start + 1
            while depth:
                depth += (source[end] == "{") - (source[end] == "}")
                end += 1
            body = source[start:end]
            self.assertNotIn("esp_websocket_", body, function)
            self.assertNotIn("portMAX_DELAY", body, function)
        self.assertIn("config.enable_close_reconnect = true", source)
        self.assertIn("config.disable_auto_reconnect = false", source)
        self.assertIn("request_transport_reset_();", source.split("network_ready_.exchange(false)", 1)[1].split("else if", 1)[0])
        self.assertIn("b2b86d7388de0d7335ff21df1cc15e6ea278caf1", (COMPONENT / "__init__.py").read_text())


if __name__ == "__main__":
    unittest.main()
