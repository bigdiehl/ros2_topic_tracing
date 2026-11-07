// Load generator for tracing overhead measurements.
//
// pubsub_bench load  FIRST NUM TOTAL TOPICS SUBS RATE_HZ DURATION_S PAYLOAD
//   Hosts nodes [FIRST, FIRST+NUM) out of TOTAL. Node i publishes TOPICS topics
//   /bench/n<i>/t<j> at RATE_HZ and subscribes to all topics of nodes i-1..i-SUBS
//   (mod TOTAL), so each topic has SUBS subscribers. Prints sent/received at exit.
//
// pubsub_bench tight ITERATIONS PAYLOAD
//   One publisher, no subscribers, publish in a loop. Prints ns per publish.

#include <chrono>
#include <cstdio>
#include <memory>
#include <string>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "std_msgs/msg/string.hpp"

using std_msgs::msg::String;
using namespace std::chrono;

static std::string topic(int node, int t)
{
  return "/bench/n" + std::to_string(node) + "/t" + std::to_string(t);
}

class LoadNode : public rclcpp::Node
{
public:
  LoadNode(int idx, int total, int topics, int subs, double rate, size_t payload)
  : Node("bench_" + std::to_string(idx))
  {
    msg_.data.assign(payload, 'x');
    for (int t = 0; t < topics; ++t) {
      pubs_.push_back(create_publisher<String>(topic(idx, t), 10));
    }
    for (int s = 1; s <= subs; ++s) {
      int src = ((idx - s) % total + total) % total;
      for (int t = 0; t < topics; ++t) {
        subs_.push_back(create_subscription<String>(
            topic(src, t), 10, [this](String::ConstSharedPtr) {++received;}));
      }
    }
    timer_ = create_wall_timer(
      duration_cast<nanoseconds>(duration<double>(1.0 / rate)),
      [this]() {
        for (auto & p : pubs_) {p->publish(msg_); ++sent;}
      });
  }
  uint64_t sent = 0, received = 0;

private:
  String msg_;
  std::vector<rclcpp::Publisher<String>::SharedPtr> pubs_;
  std::vector<rclcpp::Subscription<String>::SharedPtr> subs_;
  rclcpp::TimerBase::SharedPtr timer_;
};

int main(int argc, char ** argv)
{
  auto args = rclcpp::init_and_remove_ros_arguments(argc, argv);
  std::string mode = args.size() > 1 ? args[1] : "";

  if (mode == "tight" && args.size() == 4) {
    long iters = std::stol(args[2]);
    auto node = std::make_shared<rclcpp::Node>("bench_tight");
    auto pub = node->create_publisher<String>("/bench/tight", 10);
    String msg; msg.data.assign(std::stoul(args[3]), 'x');
    for (int i = 0; i < 10000; ++i) {pub->publish(msg);}  // warm up
    auto t0 = steady_clock::now();
    for (long i = 0; i < iters; ++i) {pub->publish(msg);}
    double ns = duration<double, std::nano>(steady_clock::now() - t0).count() / iters;
    std::printf("ns_per_publish %.1f\n", ns);
    rclcpp::shutdown();
    return 0;
  }

  if (mode == "load" && args.size() == 10) {
    int first = std::stoi(args[2]), num = std::stoi(args[3]), total = std::stoi(args[4]);
    int topics = std::stoi(args[5]), subs = std::stoi(args[6]);
    double rate = std::stod(args[7]), dur = std::stod(args[8]);
    size_t payload = std::stoul(args[9]);
    rclcpp::executors::SingleThreadedExecutor exec;
    std::vector<std::shared_ptr<LoadNode>> nodes;
    for (int i = first; i < first + num; ++i) {
      nodes.push_back(std::make_shared<LoadNode>(i, total, topics, subs, rate, payload));
      exec.add_node(nodes.back());
    }
    exec.spin_until_future_complete(std::promise<void>().get_future(), duration<double>(dur));
    uint64_t sent = 0, recv = 0;
    for (auto & n : nodes) {sent += n->sent; recv += n->received;}
    std::printf("sent %lu received %lu\n", sent, recv);
    rclcpp::shutdown();
    return 0;
  }

  std::fprintf(stderr, "usage: see header of pubsub_bench.cpp\n");
  return 1;
}
