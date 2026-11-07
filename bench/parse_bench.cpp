// Parse-cost benchmark: C++ equivalent of trace_stats.py's hot path.
// Reads babeltrace --clock-seconds text on stdin, counts rmw_publish / rmw_take
// (taken=1) per (vpid, handle). Prints events/s. Used to size a C++ consumer.
//
//   babeltrace --clock-seconds TRACE > t.txt; ./parse_bench < t.txt
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <string_view>
#include <unordered_map>

struct Ep { uint64_t count = 0; double last = 0; };

static std::string_view field(std::string_view line, std::string_view key)
{
  auto p = line.find(key);
  if (p == std::string_view::npos) return {};
  p += key.size();
  auto e = line.find_first_of(", }", p);
  return line.substr(p, e - p);
}

int main()
{
  std::ios::sync_with_stdio(false);
  std::unordered_map<std::string, Ep> eps;
  eps.reserve(4096);
  std::string line, key;
  uint64_t lines = 0, hot = 0;
  auto t0 = std::chrono::steady_clock::now();
  while (std::getline(std::cin, line)) {
    ++lines;
    std::string_view l(line);
    bool pub = l.find(" ros2:rmw_publish: ") != std::string_view::npos;
    bool take = !pub && l.find(" ros2:rmw_take: ") != std::string_view::npos;
    if (!pub && !take) continue;  // init events: rare, would use a generic parser
    if (take && field(l, "taken = ") != "1") continue;
    double ts = std::strtod(line.c_str() + 1, nullptr);
    key.assign(field(l, "vpid = "));
    key += '/';
    key += field(l, pub ? "rmw_publisher_handle = " : "rmw_subscription_handle = ");
    auto & ep = eps[key];
    ++ep.count;
    ep.last = ts;
    ++hot;
  }
  double dt = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  std::printf("lines %lu hot %lu endpoints %zu  %.2f s  %.0fk lines/s\n",
              lines, hot, eps.size(), dt, lines / dt / 1000);
}
