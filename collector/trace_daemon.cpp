// trace_daemon: ROS 2 pub/sub counts from LTTng, with per-process trace selection.
//
//   trace_daemon live --meta URL --hot URL --hot-session NAME --socket PATH
//                     [--lag-ms 700] [--bucket-ms 100] [--prune-s 5] [--history-s 60]
//                     [--idle-ms 100]   idle poll of the hot session (meta: 2x)
//   trace_daemon fs DIR [--window S] [--bucket-ms 100]    offline: print stats JSON, exit
//   trace_daemon check                                    exit 0 if babeltrace2 plugins load
//
// Live mode consumes two LTTng live sessions:
//   meta  rcl/rmw *_init events from ALL processes  -> names every endpoint
//   hot   rmw_publish / rmw_take, vpid tracker      -> counts for selected processes only
// Rules (node/topic/proc globs) select processes; the daemon keeps the hot session's vpid
// tracker in sync by running `lttng track/untrack`. Rules added by a socket client are
// removed when it disconnects; persistent rules live until removed.
//
// Socket protocol: one request line, one JSON reply line. Values contain no spaces.
//   ping | status | shutdown
//   stats [window=S] [node=GLOB]... [topic=GLOB]... [tracked=1]
//   rule add [node=G] [topic=G] [proc=G] [dir=pub|sub] [persist=1]     -> {"id": N}
//   rule del id=N
//   rule clear                                                          (persistent rules)

#include <babeltrace2/babeltrace.h>

#include <fcntl.h>
#include <fnmatch.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cinttypes>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>
#include <string>
#include <tuple>
#include <unordered_map>
#include <vector>

extern char ** environ;

namespace {

volatile sig_atomic_t g_stop = 0;

int64_t wall_ns()
{
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
    std::chrono::system_clock::now().time_since_epoch()).count();
}

int64_t mono_ns()
{
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
    std::chrono::steady_clock::now().time_since_epoch()).count();
}

void log(const char * fmt, ...) __attribute__((format(printf, 1, 2)));
void log(const char * fmt, ...)
{
  std::fprintf(stderr, "trace_daemon: ");
  va_list ap;
  va_start(ap, fmt);
  std::vfprintf(stderr, fmt, ap);
  va_end(ap);
  std::fputc('\n', stderr);
  std::fflush(stderr);
}

[[noreturn]] void die(const std::string & what)
{
  log("%s", what.c_str());
  if (const bt_error * err = bt_current_thread_take_error()) {
    if (bt_error_get_cause_count(err) > 0) {
      log("  %s", bt_error_cause_get_message(bt_error_borrow_cause_by_index(err, 0)));
    }
    bt_error_release(err);
  }
  std::exit(1);
}

// ---------------------------------------------------------------- model

enum class Kind { Ignore, NodeInit, PubInit, SubInit, RmwPubInit, RmwSubInit, Publish, Take };

struct EventInfo {
  Kind kind = Kind::Ignore;
  int64_t node_handle = -1, node_name = -1, ns = -1, rmw_handle = -1, topic = -1, depth = -1,
    taken = -1;                       // payload member indices
  int64_t vpid = -1, procname = -1;   // common context member indices
};

struct Key {
  int64_t pid;
  uint64_t handle;
  bool operator==(const Key & o) const {return pid == o.pid && handle == o.handle;}
};
struct KeyHash {
  size_t operator()(const Key & k) const
  {
    return std::hash<uint64_t>()(k.handle * 0x9E3779B97F4A7C15ull ^ static_cast<uint64_t>(k.pid));
  }
};

struct Proc {
  std::string name;
  int64_t tracked_since = 0;  // ns; 0 = message events not traced
};

struct Endpoint {
  Proc * proc = nullptr;
  bool named = false, internal = false;
  std::string node, topic;
  uint64_t depth = 0;
  int64_t created = 0;
  // counts since tracked_since (reset when tracking (re)starts)
  uint64_t count = 0;
  int64_t first = 0, last = 0;
  std::deque<std::pair<int64_t, uint32_t>> buckets;  // (bucket start ns, n), time ordered

  void reset() {count = 0; first = last = 0; buckets.clear();}
};

struct Rule {
  uint64_t id = 0;
  std::string node = "*", topic = "*", proc = "*", dir;
  uint64_t owner = 0;  // client id; 0 = persistent
  bool all() const {return node == "*" && topic == "*" && proc == "*" && dir.empty();}
};

struct State {
  bool offline = false;
  int64_t bucket_ns = 100'000'000, history_ns = 60'000'000'000, lag_ns = 700'000'000;
  int64_t prune_ns = 5'000'000'000;
  std::string hot_session;

  std::unordered_map<const bt_event_class *, EventInfo> classes;
  std::unordered_map<Key, std::string, KeyHash> nodes;
  std::unordered_map<Key, Endpoint, KeyHash> pubs, subs;
  std::unordered_map<int64_t, Proc> procs;

  std::vector<Rule> rules;
  uint64_t next_rule = 1;

  // tracker: desired (from rules) vs applied (confirmed by lttng)
  bool desired_all = false, applied_all = false;
  int64_t all_since = 0;
  std::set<int64_t> desired, applied;

  uint64_t events = 0, meta_events = 0, dropped = 0, discarded = 0, discarded_meta = 0;
  uint64_t meta_msgs = 0, hot_msgs = 0;
  int64_t first_ts = 0, last_ts = 0, started = 0;
};

State g;

// ---------------------------------------------------------------- rules

inline bool glob(const std::string & pat, const std::string & s)
{
  return pat == "*" || fnmatch(pat.c_str(), s.c_str(), 0) == 0;
}

bool rule_matches(const Rule & r, const Endpoint & e, bool pub)
{
  return e.named && glob(r.node, e.node) && glob(r.topic, e.topic) &&
         glob(r.proc, e.proc->name) && (r.dir.empty() || r.dir == (pub ? "pub" : "sub"));
}

bool any_rule_matches(const Endpoint & e, bool pub)
{
  for (const Rule & r : g.rules) {
    if (!r.all() && rule_matches(r, e, pub)) {return true;}
  }
  return false;
}

void recompute_desired()
{
  g.desired_all = false;
  for (const Rule & r : g.rules) {g.desired_all |= r.all();}
  g.desired.clear();
  for (auto * t : {&g.pubs, &g.subs}) {
    bool pub = t == &g.pubs;
    for (auto & [k, e] : *t) {
      if (!g.desired.count(k.pid) && any_rule_matches(e, pub)) {g.desired.insert(k.pid);}
    }
  }
}

void start_tracking(int64_t pid, int64_t since)
{
  auto it = g.procs.find(pid);
  if (it == g.procs.end() || it->second.tracked_since) {return;}
  it->second.tracked_since = since;
  for (auto * t : {&g.pubs, &g.subs}) {
    for (auto & [k, e] : *t) {
      if (k.pid == pid) {e.reset();}
    }
  }
}

// ---------------------------------------------------------------- lttng tracker runner

struct Runner {
  static constexpr int64_t kRetryNs = 1'000'000'000;
  pid_t child = 0;
  int64_t retry_at = 0;  // mono ns; after a failed command
  enum class Op { None, TrackAll, UntrackAll, Track, Untrack } op = Op::None;
  std::vector<int64_t> pids;

  void spawn(Op o, std::vector<int64_t> p)
  {
    std::vector<std::string> args{"lttng", o == Op::Track || o == Op::TrackAll ? "track" : "untrack",
      "-u", "-s", g.hot_session, "--vpid"};
    if (o == Op::TrackAll || o == Op::UntrackAll) {
      args.push_back("--all");
    } else {
      std::string list;
      for (int64_t pid : p) {list += (list.empty() ? "" : ",") + std::to_string(pid);}
      args.push_back(list);
    }
    std::vector<char *> argv;
    for (auto & a : args) {argv.push_back(a.data());}
    argv.push_back(nullptr);
    posix_spawn_file_actions_t fa;
    posix_spawn_file_actions_init(&fa);
    posix_spawn_file_actions_addopen(&fa, 1, "/dev/null", O_WRONLY, 0);
    int rc = posix_spawnp(&child, "lttng", &fa, nullptr, argv.data(), environ);
    posix_spawn_file_actions_destroy(&fa);
    if (rc != 0) {
      log("cannot run lttng: %s", std::strerror(rc));
      child = 0;
      retry_at = mono_ns() + kRetryNs;
      return;
    }
    op = o;
    pids = std::move(p);
  }

  // Apply a completed command to the state. A failed one changes nothing and is retried:
  // recording it as applied would report processes as traced while they are not.
  void finish(Op o, const std::vector<int64_t> & p, bool ok)
  {
    if (!ok) {
      log("lttng %s failed, retrying in 1 s", o == Op::Track || o == Op::TrackAll ? "track" : "untrack");
      retry_at = mono_ns() + kRetryNs;
      return;
    }
    int64_t now = wall_ns();
    switch (o) {
      case Op::TrackAll:
        g.applied_all = true;
        g.all_since = now;
        for (auto & kv : g.procs) {start_tracking(kv.first, now);}
        break;
      case Op::UntrackAll:
        g.applied_all = false;
        g.applied.clear();
        for (auto & [pid, pr] : g.procs) {
          if (!g.desired.count(pid)) {pr.tracked_since = 0;}  // desired ones re-tracked next
        }
        break;
      case Op::Track:
        for (int64_t pid : p) {g.applied.insert(pid); start_tracking(pid, now);}
        break;
      case Op::Untrack:
        for (int64_t pid : p) {
          g.applied.erase(pid);
          auto it = g.procs.find(pid);
          if (it != g.procs.end()) {it->second.tracked_since = 0;}
        }
        break;
      default: break;
    }
  }

  void poll()
  {
    if (child) {
      int st = 0;
      pid_t r = waitpid(child, &st, WNOHANG);
      if (r == 0) {return;}
      child = 0;
      finish(op, pids, r > 0 && WIFEXITED(st) && WEXITSTATUS(st) == 0);
      op = Op::None;
    }
    if (g.offline || mono_ns() < retry_at) {return;}
    if (g.desired_all && !g.applied_all) {spawn(Op::TrackAll, {}); return;}
    if (!g.desired_all && g.applied_all) {spawn(Op::UntrackAll, {}); return;}
    if (g.applied_all) {return;}
    std::vector<int64_t> add, rem;
    std::set_difference(g.desired.begin(), g.desired.end(), g.applied.begin(), g.applied.end(),
      std::back_inserter(add));
    std::set_difference(g.applied.begin(), g.applied.end(), g.desired.begin(), g.desired.end(),
      std::back_inserter(rem));
    if (!add.empty()) {spawn(Op::Track, add);} else if (!rem.empty()) {spawn(Op::Untrack, rem);}
  }

  bool idle() const {return child == 0;}
};

Runner g_runner;

// ---------------------------------------------------------------- events

int64_t index_of(const bt_field_class * fc, const char * name)
{
  if (!fc) {return -1;}
  uint64_t n = bt_field_class_structure_get_member_count(fc);
  for (uint64_t i = 0; i < n; ++i) {
    auto m = bt_field_class_structure_borrow_member_by_index_const(fc, i);
    if (std::strcmp(bt_field_class_structure_member_get_name(m), name) == 0) {
      return static_cast<int64_t>(i);
    }
  }
  return -1;
}

const EventInfo & info_for(const bt_event_class * ec)
{
  auto it = g.classes.find(ec);
  if (it != g.classes.end()) {return it->second;}
  EventInfo info;
  const char * name = bt_event_class_get_name(ec);
  name = name ? name : "";
  static const std::pair<const char *, Kind> kinds[] = {
    {"ros2:rmw_publish", Kind::Publish}, {"ros2:rmw_take", Kind::Take},
    {"ros2:rcl_node_init", Kind::NodeInit}, {"ros2:rcl_publisher_init", Kind::PubInit},
    {"ros2:rcl_subscription_init", Kind::SubInit},
    {"ros2:rmw_publisher_init", Kind::RmwPubInit},
    {"ros2:rmw_subscription_init", Kind::RmwSubInit},
  };
  for (auto & [n, k] : kinds) {
    if (std::strcmp(name, n) == 0) {info.kind = k;}
  }
  auto pfc = bt_event_class_borrow_payload_field_class_const(ec);
  auto cfc = bt_stream_class_borrow_event_common_context_field_class_const(
    bt_event_class_borrow_stream_class_const(ec));
  info.vpid = index_of(cfc, "vpid");
  info.procname = index_of(cfc, "procname");
  switch (info.kind) {
    case Kind::Publish: case Kind::RmwPubInit:
      info.rmw_handle = index_of(pfc, "rmw_publisher_handle"); break;
    case Kind::Take:
      info.rmw_handle = index_of(pfc, "rmw_subscription_handle");
      info.taken = index_of(pfc, "taken"); break;
    case Kind::RmwSubInit:
      info.rmw_handle = index_of(pfc, "rmw_subscription_handle"); break;
    case Kind::NodeInit:
      info.node_handle = index_of(pfc, "node_handle");
      info.node_name = index_of(pfc, "node_name");
      info.ns = index_of(pfc, "namespace"); break;
    case Kind::PubInit: case Kind::SubInit:
      info.node_handle = index_of(pfc, "node_handle");
      info.rmw_handle = index_of(pfc, info.kind == Kind::PubInit ?
                                 "rmw_publisher_handle" : "rmw_subscription_handle");
      info.topic = index_of(pfc, "topic_name");
      info.depth = index_of(pfc, "queue_depth"); break;
    default: break;
  }
  if (info.kind != Kind::Ignore && (info.vpid < 0 || (info.kind != Kind::NodeInit &&
    info.rmw_handle < 0)))
  {
    log("%s lacks vpid context or handle field, ignored", name);
    info.kind = Kind::Ignore;
  }
  return g.classes.emplace(ec, info).first->second;
}

inline const bt_field * member(const bt_field * st, int64_t idx)
{
  return bt_field_structure_borrow_member_field_by_index_const(st, static_cast<uint64_t>(idx));
}

inline uint64_t get_uint(const bt_field * f)
{
  if (bt_field_class_type_is(bt_field_get_class_type(f), BT_FIELD_CLASS_TYPE_SIGNED_INTEGER)) {
    return static_cast<uint64_t>(bt_field_integer_signed_get_value(f));
  }
  return bt_field_integer_unsigned_get_value(f);
}

inline const char * get_str(const bt_field * st, int64_t idx)
{
  return idx < 0 ? "" : bt_field_string_get_value(member(st, idx));
}

std::string proc_comm(int64_t pid)
{
  std::ifstream f("/proc/" + std::to_string(pid) + "/comm");
  std::string s;
  std::getline(f, s);
  return s.empty() ? "?" : s;
}

Proc & ensure_proc(int64_t pid, const char * name)
{
  auto [it, fresh] = g.procs.try_emplace(pid);
  Proc & p = it->second;
  if (p.name.empty() && name && *name) {p.name = name;}
  if (fresh && g.applied_all) {p.tracked_since = g.all_since;}
  return p;
}

Endpoint & ensure_endpoint(std::unordered_map<Key, Endpoint, KeyHash> & t, const Key & k,
  Proc & p)
{
  Endpoint & e = t[k];
  if (!e.proc) {e.proc = &p;}
  return e;
}

void count(Endpoint & e, int64_t ts)
{
  int64_t b = ts - ts % g.bucket_ns;
  if (e.count == 0) {e.first = ts;}
  ++e.count;
  if (ts > e.last) {e.last = ts;}
  auto & q = e.buckets;
  if (!q.empty() && q.back().first == b) {
    ++q.back().second;
  } else if (q.empty() || b > q.back().first) {
    q.emplace_back(b, 1);
    while (q.front().first < b - g.history_ns) {q.pop_front();}
  } else {  // older bucket (rare: muxer keeps per-graph order)
    for (auto it = q.rbegin(); it != q.rend(); ++it) {
      if (it->first == b) {++it->second; return;}
      if (it->first < b) {q.insert(it.base(), {b, 1}); return;}
    }
    q.emplace_front(b, 1);
  }
}

void on_message_event(Endpoint * e, std::unordered_map<Key, Endpoint, KeyHash> & t,
  const Key & k, int64_t ts)
{
  ++g.events;
  if (!e) {
    // first message on an endpoint without a (processed) init event
    auto pit = g.procs.find(k.pid);
    Proc * p = pit != g.procs.end() ? &pit->second : nullptr;
    if (!p && (g.applied_all || g.offline)) {
      p = &ensure_proc(k.pid, g.offline ? "?" : proc_comm(k.pid).c_str());
    }
    if (!p) {++g.dropped; return;}
    e = &ensure_endpoint(t, k, *p);
  }
  if (!e->proc->tracked_since) {++g.dropped; return;}  // in flight after untrack
  count(*e, ts);
}

void handle_event(const bt_message * msg, bool meta)
{
  const bt_event * ev = bt_message_event_borrow_event_const(msg);
  const EventInfo & info = info_for(bt_event_borrow_class_const(ev));
  if (info.kind == Kind::Ignore) {return;}

  int64_t ts = 0;
  if (auto cs = bt_message_event_borrow_default_clock_snapshot_const(msg)) {
    bt_clock_snapshot_get_ns_from_origin(cs, &ts);
  }
  const bt_field * payload = bt_event_borrow_payload_field_const(ev);
  const bt_field * ctx = bt_event_borrow_common_context_field_const(ev);
  int64_t pid = static_cast<int64_t>(get_uint(member(ctx, info.vpid)));
  if (!g.first_ts || ts < g.first_ts) {g.first_ts = ts;}
  if (ts > g.last_ts) {g.last_ts = ts;}
  if (meta) {++g.meta_events;}

  switch (info.kind) {
    case Kind::Publish: case Kind::Take: {
      if (info.kind == Kind::Take && info.taken >= 0 &&
        get_uint(member(payload, info.taken)) == 0) {return;}
      auto & t = info.kind == Kind::Publish ? g.pubs : g.subs;
      Key k{pid, get_uint(member(payload, info.rmw_handle))};
      auto it = t.find(k);
      on_message_event(it != t.end() ? &it->second : nullptr, t, k, ts);
      break;
    }
    case Kind::RmwPubInit: case Kind::RmwSubInit: {
      Proc & p = ensure_proc(pid, get_str(ctx, info.procname));
      auto & t = info.kind == Kind::RmwPubInit ? g.pubs : g.subs;
      Endpoint & e = ensure_endpoint(t, Key{pid, get_uint(member(payload, info.rmw_handle))}, p);
      if (e.named) {e = Endpoint{}; e.proc = &p;}  // handle reused
      e.internal = true;
      break;
    }
    case Kind::NodeInit: {
      ensure_proc(pid, get_str(ctx, info.procname));
      std::string ns = get_str(payload, info.ns);
      while (!ns.empty() && ns.back() == '/') {ns.pop_back();}
      g.nodes[Key{pid, get_uint(member(payload, info.node_handle))}] =
        ns + "/" + get_str(payload, info.node_name);
      break;
    }
    case Kind::PubInit: case Kind::SubInit: {
      bool pub = info.kind == Kind::PubInit;
      Proc & p = ensure_proc(pid, get_str(ctx, info.procname));
      Endpoint & e = ensure_endpoint(pub ? g.pubs : g.subs,
          Key{pid, get_uint(member(payload, info.rmw_handle))}, p);
      if (e.named) {e.reset();}  // handle reused by a new endpoint
      auto nit = g.nodes.find(Key{pid, get_uint(member(payload, info.node_handle))});
      e.named = true;
      e.internal = false;
      e.node = nit != g.nodes.end() ? nit->second : "?";
      e.topic = get_str(payload, info.topic);
      e.depth = info.depth >= 0 ? get_uint(member(payload, info.depth)) : 0;
      e.created = ts;
      if (!g.offline && !g.desired_all && !g.desired.count(pid) && any_rule_matches(e, pub)) {
        g.desired.insert(pid);
      }
      break;
    }
    default: break;
  }
}

void prune()
{
  std::vector<int64_t> dead;
  for (auto & kv : g.procs) {
    if (access(("/proc/" + std::to_string(kv.first)).c_str(), F_OK) != 0) {
      dead.push_back(kv.first);
    }
  }
  if (dead.empty()) {return;}
  std::set<int64_t> d(dead.begin(), dead.end());
  auto drop = [&](auto & m) {
      for (auto it = m.begin(); it != m.end(); ) {
        it = d.count(it->first.pid) ? m.erase(it) : std::next(it);
      }
    };
  drop(g.pubs); drop(g.subs); drop(g.nodes);
  for (int64_t pid : dead) {g.procs.erase(pid); g.desired.erase(pid);}
  // applied keeps dead pids until the runner untracks them (keeps the tracker set small)
}

// ---------------------------------------------------------------- JSON

struct Json {
  std::string s;
  bool first = true;

  void sep() {if (!first) {s += ',';} first = false;}
  void key(const char * k) {sep(); s += '"'; s += k; s += "\":"; first = true;}
  void str(const std::string & v)
  {
    s += '"';
    for (unsigned char c : v) {
      if (c == '"' || c == '\\') {s += '\\'; s += static_cast<char>(c);}
      else if (c < 0x20) {char b[8]; std::snprintf(b, sizeof(b), "\\u%04x", c); s += b;}
      else {s += static_cast<char>(c);}
    }
    s += '"';
    first = false;
  }
  void raw(const char * v) {s += v; first = false;}
  void num(double v, int prec = 3) {char b[32]; std::snprintf(b, sizeof(b), "%.*f", prec, v); raw(b);}
  void num(int64_t v) {raw(std::to_string(v).c_str());}
  void num(uint64_t v) {raw(std::to_string(v).c_str());}
  void boolean(bool v) {raw(v ? "true" : "false");}
  void open(char c) {sep(); s += c; first = true;}
  void close(char c) {s += c; first = false;}

  template<class T> void kv(const char * k, const T & v) {key(k); put(v);}
  void put(const std::string & v) {str(v);}
  void put(const char * v) {str(v);}
  void put(bool v) {boolean(v);}
  void put(double v) {num(v);}
  void put(int64_t v) {num(v);}
  void put(uint64_t v) {num(v);}
  void put(int v) {num(static_cast<int64_t>(v));}
  void null(const char * k) {key(k); raw("null");}
};

std::string error_json(const std::string & msg)
{
  Json j;
  j.open('{');
  j.kv("error", msg);
  j.close('}');
  return j.s;
}

int64_t eval_time()
{
  int64_t now = g.offline ? g.last_ts + g.bucket_ns : wall_ns() - g.lag_ns;
  return now - now % g.bucket_ns;
}

struct StatsQuery {
  double window = 5.0;
  std::vector<std::string> nodes, topics;
  bool tracked_only = false;
};

std::string stats_json(const StatsQuery & q)
{
  int64_t now = eval_time();
  int64_t win = static_cast<int64_t>(q.window * 1e9);
  Json j;
  j.open('{');
  j.kv("time", now / 1e9);
  j.kv("window", q.window);
  j.kv("lag", g.offline ? 0.0 : g.lag_ns / 1e9);
  j.kv("mode", std::string(g.applied_all ? "all" : "selected"));
  j.kv("events", g.events);
  j.kv("discarded", g.discarded);
  j.kv("discarded_meta", g.discarded_meta);
  uint64_t traced = 0;
  for (auto & kv : g.procs) {traced += kv.second.tracked_since != 0;}
  j.kv("traced_procs", traced);
  j.kv("procs", static_cast<uint64_t>(g.procs.size()));

  uint64_t internal_msgs = 0;
  std::map<std::tuple<int64_t, std::string, std::string>, uint64_t> untracked;
  j.key("endpoints");
  j.open('[');
  for (auto * t : {&g.pubs, &g.subs}) {
    bool pub = t == &g.pubs;
    for (auto & [k, e] : *t) {
      if (!e.named) {
        if (e.internal) {internal_msgs += e.count;}
        else if (e.count) {untracked[{k.pid, e.proc->name, pub ? "pub" : "sub"}] += e.count;}
        continue;
      }
      bool tracked = e.proc->tracked_since != 0;
      if (q.tracked_only && !tracked) {continue;}
      auto in = [](const std::vector<std::string> & gl, const std::string & s) {
          if (gl.empty()) {return true;}
          for (auto & p : gl) {if (glob(p, s)) {return true;}}
          return false;
        };
      if (!in(q.nodes, e.node) || !in(q.topics, e.topic)) {continue;}

      j.open('{');
      j.kv("topic", e.topic);
      j.kv("dir", std::string(pub ? "pub" : "sub"));
      j.kv("node", e.node);
      j.kv("pid", k.pid);
      j.kv("proc", e.proc->name);
      j.kv("depth", e.depth);
      j.kv("tracked", tracked);
      if (!tracked) {
        for (auto f : {"count", "rate", "avg_rate", "last_age", "since"}) {j.null(f);}
        j.kv("from_start", false);
      } else {
        int64_t since = std::max(e.created, e.proc->tracked_since);
        int64_t lo = std::max(now - win, since - since % g.bucket_ns);
        uint64_t n = 0;
        for (auto it = e.buckets.rbegin(); it != e.buckets.rend() && it->first >= lo; ++it) {
          if (it->first < now) {n += it->second;}
        }
        double span = (now - lo) / 1e9;
        j.kv("count", e.count);
        j.kv("rate", span > 0 ? n / span : 0.0);
        j.kv("avg_rate", e.count > 1 && e.last > e.first ?
          (e.count - 1) / ((e.last - e.first) / 1e9) : 0.0);
        if (e.count) {j.kv("last_age", std::max(0.0, (now - e.last) / 1e9));}
        else {j.null("last_age");}
        j.kv("since", std::max(0.0, (now - since) / 1e9));
        j.kv("from_start", e.proc->tracked_since <= e.created);
      }
      j.close('}');
    }
  }
  j.close(']');
  j.kv("internal_msgs", internal_msgs);
  j.key("untracked");
  j.open('[');
  for (auto & [k, c] : untracked) {
    j.open('{');
    j.kv("pid", std::get<0>(k));
    j.kv("proc", std::get<1>(k));
    j.kv("dir", std::get<2>(k));
    j.kv("count", c);
    j.close('}');
  }
  j.close(']');
  j.close('}');
  return j.s;
}

void rule_json(Json & j, const Rule & r)
{
  j.open('{');
  j.kv("id", r.id);
  j.kv("node", r.node);
  j.kv("topic", r.topic);
  j.kv("proc", r.proc);
  j.kv("dir", r.dir.empty() ? std::string("*") : r.dir);
  j.kv("persist", r.owner == 0);
  j.kv("owner", r.owner);
  j.close('}');
}

std::string status_json(size_t clients)
{
  Json j;
  j.open('{');
  j.kv("pid", static_cast<int64_t>(getpid()));
  j.kv("uptime", (mono_ns() - g.started) / 1e9);
  j.kv("hot_session", g.hot_session);
  j.kv("mode", std::string(g.desired_all ? "all" : "selected"));
  j.kv("applied", g.applied_all == g.desired_all && (g.applied_all || g.applied == g.desired) &&
    g_runner.idle());
  j.key("rules");
  j.open('[');
  for (auto & r : g.rules) {rule_json(j, r);}
  j.close(']');
  j.key("traced");
  j.open('[');
  for (auto & [pid, p] : g.procs) {
    if (!p.tracked_since) {continue;}
    j.open('{');
    j.kv("pid", pid);
    j.kv("proc", p.name);
    j.kv("since", std::max(0.0, (wall_ns() - p.tracked_since) / 1e9));
    j.close('}');
  }
  j.close(']');
  uint64_t named = 0;
  for (auto * t : {&g.pubs, &g.subs}) {for (auto & kv : *t) {named += kv.second.named;}}
  j.kv("procs", static_cast<uint64_t>(g.procs.size()));
  j.kv("endpoints", named);
  j.kv("events", g.events);
  j.kv("meta_events", g.meta_events);
  j.kv("meta_msgs", g.meta_msgs);
  j.kv("dropped", g.dropped);
  j.kv("discarded", g.discarded);
  j.kv("discarded_meta", g.discarded_meta);
  j.kv("clients", static_cast<uint64_t>(clients));
  j.close('}');
  return j.s;
}

// ---------------------------------------------------------------- socket server

struct Client {
  uint64_t id;
  std::string in, out;
  bool close_after_write = false;
};

std::map<int, Client> g_clients;
uint64_t g_next_client = 1;

void drop_client_rules(uint64_t id)
{
  auto n = g.rules.size();
  g.rules.erase(std::remove_if(g.rules.begin(), g.rules.end(),
    [&](const Rule & r) {return r.owner == id;}), g.rules.end());
  if (g.rules.size() != n) {recompute_desired();}
}

std::string handle_request(Client & c, const std::string & line)
{
  std::vector<std::string> words;
  std::multimap<std::string, std::string> kv;
  size_t i = 0;
  while (i < line.size()) {
    while (i < line.size() && std::isspace(static_cast<unsigned char>(line[i]))) {++i;}
    size_t j = i;
    while (j < line.size() && !std::isspace(static_cast<unsigned char>(line[j]))) {++j;}
    if (j > i) {
      std::string w = line.substr(i, j - i);
      auto eq = w.find('=');
      if (eq == std::string::npos) {words.push_back(w);} else {kv.emplace(w.substr(0, eq), w.substr(eq + 1));}
    }
    i = j;
  }
  auto get = [&](const char * k, const std::string & d = "") {
      auto it = kv.find(k);
      return it == kv.end() ? d : it->second;
    };
  if (words.empty()) {return error_json("empty request");}
  const std::string & cmd = words[0];

  if (cmd == "ping") {return "{\"ok\":true}";}
  if (cmd == "status") {return status_json(g_clients.size());}
  if (cmd == "shutdown") {g_stop = 1; c.close_after_write = true; return "{\"ok\":true}";}
  if (cmd == "stats") {
    StatsQuery q;
    q.window = std::atof(get("window", "5").c_str());
    if (q.window <= 0) {return error_json("window must be > 0");}
    for (auto [a, b] = kv.equal_range("node"); a != b; ++a) {q.nodes.push_back(a->second);}
    for (auto [a, b] = kv.equal_range("topic"); a != b; ++a) {q.topics.push_back(a->second);}
    q.tracked_only = get("tracked") == "1";
    return stats_json(q);
  }
  if (cmd == "rule" && words.size() >= 2) {
    const std::string & sub = words[1];
    if (sub == "add") {
      Rule r;
      r.id = g.next_rule++;
      r.node = get("node", "*");
      r.topic = get("topic", "*");
      r.proc = get("proc", "*");
      r.dir = get("dir");
      if (!r.dir.empty() && r.dir != "pub" && r.dir != "sub") {return error_json("dir: pub|sub");}
      r.owner = get("persist") == "1" ? 0 : c.id;
      g.rules.push_back(r);
      recompute_desired();
      return "{\"ok\":true,\"id\":" + std::to_string(r.id) + "}";
    }
    if (sub == "del") {
      uint64_t id = std::strtoull(get("id", "0").c_str(), nullptr, 10);
      auto it = std::find_if(g.rules.begin(), g.rules.end(), [&](const Rule & r) {return r.id == id;});
      if (it == g.rules.end()) {return error_json("no rule " + std::to_string(id));}
      if (it->owner != 0 && it->owner != c.id) {return error_json("rule owned by another client");}
      g.rules.erase(it);
      recompute_desired();
      return "{\"ok\":true}";
    }
    if (sub == "clear") {
      drop_client_rules(0);
      return "{\"ok\":true}";
    }
  }
  return error_json("unknown request: " + line);
}

int make_server(const std::string & path)
{
  sockaddr_un addr{};
  addr.sun_family = AF_UNIX;
  if (path.size() >= sizeof(addr.sun_path)) {die("socket path too long");}
  std::strcpy(addr.sun_path, path.c_str());
  int probe = socket(AF_UNIX, SOCK_STREAM, 0);
  if (connect(probe, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) == 0) {
    die("another trace_daemon is listening on " + path);
  }
  close(probe);
  unlink(path.c_str());
  int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
  mode_t old = umask(077);
  if (bind(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) != 0) {
    die("bind " + path + ": " + std::strerror(errno));
  }
  umask(old);
  if (listen(fd, 16) != 0) {die("listen failed");}
  return fd;
}

void serve(int lfd, int timeout_ms)
{
  std::vector<pollfd> fds{{lfd, POLLIN, 0}};
  for (auto & [fd, c] : g_clients) {
    fds.push_back({fd, static_cast<short>(POLLIN | (c.out.empty() ? 0 : POLLOUT)), 0});
  }
  if (::poll(fds.data(), fds.size(), timeout_ms) <= 0) {return;}
  if (fds[0].revents & POLLIN) {
    int cfd;
    while ((cfd = accept4(lfd, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC)) >= 0) {
      g_clients[cfd] = Client{g_next_client++, {}, {}, false};
    }
  }
  for (size_t i = 1; i < fds.size(); ++i) {
    int fd = fds[i].fd;
    auto it = g_clients.find(fd);
    if (it == g_clients.end()) {continue;}
    Client & c = it->second;
    bool dead = fds[i].revents & (POLLERR | POLLNVAL);
    if (!dead && (fds[i].revents & (POLLIN | POLLHUP))) {
      char buf[4096];
      ssize_t n;
      while ((n = read(fd, buf, sizeof(buf))) > 0) {c.in.append(buf, n);}
      if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) {dead = true;}
      size_t nl;
      while ((nl = c.in.find('\n')) != std::string::npos) {
        std::string line = c.in.substr(0, nl);
        c.in.erase(0, nl + 1);
        c.out += handle_request(c, line);
        c.out += '\n';
      }
      if (c.in.size() > 65536) {dead = true;}
    }
    if (!c.out.empty()) {
      ssize_t n = write(fd, c.out.data(), c.out.size());
      if (n > 0) {c.out.erase(0, n);}
      else if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {dead = true;}
    }
    if (dead || (c.close_after_write && c.out.empty())) {
      drop_client_rules(c.id);
      close(fd);
      g_clients.erase(it);
    }
  }
}

// ---------------------------------------------------------------- babeltrace graph

struct SinkCtx {
  bool meta;
};

bt_graph_simple_sink_component_initialize_func_status sink_init(bt_message_iterator *, void *)
{
  return BT_GRAPH_SIMPLE_SINK_COMPONENT_INITIALIZE_FUNC_STATUS_OK;
}

bt_graph_simple_sink_component_consume_func_status sink_consume(bt_message_iterator * it,
  void * data)
{
  bool meta = static_cast<SinkCtx *>(data)->meta;
  bt_message_array_const msgs;
  uint64_t n = 0;
  switch (bt_message_iterator_next(it, &msgs, &n)) {
    case BT_MESSAGE_ITERATOR_NEXT_STATUS_OK: break;
    case BT_MESSAGE_ITERATOR_NEXT_STATUS_END:
      return BT_GRAPH_SIMPLE_SINK_COMPONENT_CONSUME_FUNC_STATUS_END;
    case BT_MESSAGE_ITERATOR_NEXT_STATUS_AGAIN:
      return BT_GRAPH_SIMPLE_SINK_COMPONENT_CONSUME_FUNC_STATUS_AGAIN;
    case BT_MESSAGE_ITERATOR_NEXT_STATUS_MEMORY_ERROR:
      return BT_GRAPH_SIMPLE_SINK_COMPONENT_CONSUME_FUNC_STATUS_MEMORY_ERROR;
    default:
      return BT_GRAPH_SIMPLE_SINK_COMPONENT_CONSUME_FUNC_STATUS_ERROR;
  }
  (meta ? g.meta_msgs : g.hot_msgs) += n;
  for (uint64_t i = 0; i < n; ++i) {
    const bt_message * m = msgs[i];
    switch (bt_message_get_type(m)) {
      case BT_MESSAGE_TYPE_EVENT:
        handle_event(m, meta);
        break;
      case BT_MESSAGE_TYPE_DISCARDED_EVENTS: {
        uint64_t c = 0;
        if (bt_message_discarded_events_get_count(m, &c) == BT_PROPERTY_AVAILABILITY_AVAILABLE) {
          (meta ? g.discarded_meta : g.discarded) += c;
        }
        break;
      }
      default: break;
    }
    bt_message_put_ref(m);
  }
  return BT_GRAPH_SIMPLE_SINK_COMPONENT_CONSUME_FUNC_STATUS_OK;
}

const bt_plugin * find_plugin(const char * name)
{
  const bt_plugin * p = nullptr;
  if (bt_plugin_find(name, BT_TRUE, BT_TRUE, BT_TRUE, BT_TRUE, BT_FALSE, &p) !=
    BT_PLUGIN_FIND_STATUS_OK)
  {
    die(std::string("babeltrace2 plugin not found: ") + name + " (install libbabeltrace2-0)");
  }
  return p;
}

const bt_port_input * free_input_port(const bt_component_filter * f)
{
  uint64_t n = bt_component_filter_get_input_port_count(f);
  for (uint64_t i = 0; i < n; ++i) {
    auto p = bt_component_filter_borrow_input_port_by_index_const(f, i);
    if (!bt_port_is_connected(bt_port_input_as_port_const(p))) {return p;}
  }
  return nullptr;
}

bt_graph * make_graph(bool live, const std::vector<std::string> & inputs, SinkCtx * ctx)
{
  const bt_plugin * ctf = find_plugin("ctf");
  const bt_plugin * utils = find_plugin("utils");
  auto src_cls = bt_plugin_borrow_source_component_class_by_name_const(
    ctf, live ? "lttng-live" : "fs");
  auto mux_cls = bt_plugin_borrow_filter_component_class_by_name_const(utils, "muxer");
  if (!src_cls || !mux_cls) {die("component class missing");}

  bt_value * params = bt_value_map_create();
  bt_value * arr = nullptr;
  bt_value_map_insert_empty_array_entry(params, "inputs", &arr);
  for (auto & in : inputs) {bt_value_array_append_string_element(arr, in.c_str());}
  if (live) {bt_value_map_insert_string_entry(params, "session-not-found-action", "continue");}

  bt_graph * graph = bt_graph_create(0);
  const bt_component_source * src = nullptr;
  const bt_component_filter * mux = nullptr;
  const bt_component_sink * sink = nullptr;
  if (bt_graph_add_source_component(graph, src_cls, "src", params, BT_LOGGING_LEVEL_WARNING,
    &src) != BT_GRAPH_ADD_COMPONENT_STATUS_OK) {die("cannot create source");}
  if (bt_graph_add_filter_component(graph, mux_cls, "mux", nullptr, BT_LOGGING_LEVEL_WARNING,
    &mux) != BT_GRAPH_ADD_COMPONENT_STATUS_OK) {die("cannot create muxer");}
  if (bt_graph_add_simple_sink_component(graph, "sink", sink_init, sink_consume, nullptr, ctx,
    &sink) != BT_GRAPH_ADD_COMPONENT_STATUS_OK) {die("cannot create sink");}
  bt_value_put_ref(params);

  uint64_t nout = bt_component_source_get_output_port_count(src);
  for (uint64_t i = 0; i < nout; ++i) {
    auto in = free_input_port(mux);
    if (!in || bt_graph_connect_ports(graph, bt_component_source_borrow_output_port_by_index_const(
        src, i), in, nullptr) != BT_GRAPH_CONNECT_PORTS_STATUS_OK) {die("connect src->mux");}
  }
  if (bt_graph_connect_ports(graph, bt_component_filter_borrow_output_port_by_name_const(mux, "out"),
    bt_component_sink_borrow_input_port_by_name_const(sink, "in"), nullptr) !=
    BT_GRAPH_CONNECT_PORTS_STATUS_OK) {die("connect mux->sink");}
  return graph;
}

std::vector<std::string> find_traces(const std::string & root)
{
  namespace fs = std::filesystem;
  std::vector<std::string> out;
  std::error_code ec;
  for (auto it = fs::recursive_directory_iterator(root, ec); !ec && it != fs::end(it);
    it.increment(ec))
  {
    if (it->path().filename() == "metadata") {out.push_back(it->path().parent_path());}
  }
  if (out.empty()) {die("no CTF trace (metadata file) found in " + root);}
  return out;
}

int usage()
{
  std::fprintf(stderr,
    "usage: trace_daemon live --meta URL --hot URL --hot-session NAME --socket PATH\n"
    "                         [--lag-ms 700] [--bucket-ms 100] [--prune-s 5] [--history-s 60]\n"
    "                         [--idle-ms 100]\n"
    "       trace_daemon fs DIR [--window S] [--bucket-ms 100]\n"
    "       trace_daemon check\n");
  return 2;
}

int run_offline(const std::string & dir, double window)
{
  g.offline = true;
  g.applied_all = true;
  g.all_since = 1;  // everything traced from the start
  g.history_ns = INT64_MAX / 2;
  SinkCtx ctx{false};
  bt_graph * graph = make_graph(false, find_traces(dir), &ctx);
  for (;;) {
    auto st = bt_graph_run_once(graph);
    if (st == BT_GRAPH_RUN_ONCE_STATUS_END) {break;}
    if (st != BT_GRAPH_RUN_ONCE_STATUS_OK && st != BT_GRAPH_RUN_ONCE_STATUS_AGAIN) {
      die("graph run failed");
    }
  }
  bt_graph_put_ref(graph);
  StatsQuery q;
  q.window = window > 0 ? window :
    std::max(g.bucket_ns, eval_time() - (g.first_ts - g.first_ts % g.bucket_ns)) / 1e9;
  std::puts(stats_json(q).c_str());
  return 0;
}

}  // namespace

int main(int argc, char ** argv)
{
  std::vector<std::string> pos;
  std::string meta_url, hot_url, sock_path;
  double window = 0;
  int64_t idle_ns = 100'000'000;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto val = [&]() -> std::string {
        if (i + 1 >= argc) {die("missing value for " + a);}
        return argv[++i];
      };
    if (a == "--bucket-ms") {g.bucket_ns = std::atoll(val().c_str()) * 1'000'000;}
    else if (a == "--lag-ms") {g.lag_ns = std::atoll(val().c_str()) * 1'000'000;}
    else if (a == "--prune-s") {g.prune_ns = std::atoll(val().c_str()) * 1'000'000'000;}
    else if (a == "--history-s") {g.history_ns = std::atoll(val().c_str()) * 1'000'000'000;}
    else if (a == "--idle-ms") {idle_ns = std::atoll(val().c_str()) * 1'000'000;}
    else if (a == "--window") {window = std::atof(val().c_str());}
    else if (a == "--meta") {meta_url = val();}
    else if (a == "--hot") {hot_url = val();}
    else if (a == "--hot-session") {g.hot_session = val();}
    else if (a == "--socket") {sock_path = val();}
    else {pos.push_back(a);}
  }
  if (g.bucket_ns <= 0) {return usage();}
  if (pos.size() == 1 && pos[0] == "check") {
    find_plugin("ctf");
    find_plugin("utils");
    std::puts("ok");
    return 0;
  }
  if (pos.size() == 2 && pos[0] == "fs") {return run_offline(pos[1], window);}
  if (pos.size() != 1 || pos[0] != "live" || meta_url.empty() || hot_url.empty() ||
    g.hot_session.empty() || sock_path.empty()) {return usage();}

  signal(SIGPIPE, SIG_IGN);
  struct sigaction sa{};
  sa.sa_handler = [](int) {g_stop = 1;};
  sigaction(SIGTERM, &sa, nullptr);
  sigaction(SIGINT, &sa, nullptr);

  g.started = mono_ns();
  int lfd = make_server(sock_path);
  SinkCtx meta_ctx{true}, hot_ctx{false};
  // An idle live source (AGAIN) is not polled again for `idle` ns: each poll costs relayd
  // requests (one per stream), and polling an idle graph while the other is busy loads
  // relayd heavily. Names (meta) tolerate more latency than counts (hot).
  struct G {bt_graph * graph; const char * name; int64_t idle; int64_t next = 0;};
  G graphs[] = {{make_graph(true, {meta_url}, &meta_ctx), "meta", 2 * idle_ns},
    {make_graph(true, {hot_url}, &hot_ctx), "hot", idle_ns}};
  log("listening on %s (meta %s, hot %s)", sock_path.c_str(), meta_url.c_str(), hot_url.c_str());

  int64_t next_prune = mono_ns() + g.prune_ns;
  int rc = 0;
  while (!g_stop) {
    bool busy = false;
    int64_t now = mono_ns(), wake = now + idle_ns;
    for (auto & gr : graphs) {
      if (now >= gr.next) {
        auto st = bt_graph_run_once(gr.graph);
        if (st == BT_GRAPH_RUN_ONCE_STATUS_OK) {busy = true;}
        else if (st == BT_GRAPH_RUN_ONCE_STATUS_AGAIN) {gr.next = now + gr.idle;}
        else if (st == BT_GRAPH_RUN_ONCE_STATUS_END) {log("%s session ended", gr.name); g_stop = 1; rc = 1;}
        else {log("%s graph failed", gr.name); g_stop = 1; rc = 1;}
      }
      wake = std::min(wake, gr.next);
    }
    g_runner.poll();
    int timeout_ms = busy ? 0 : static_cast<int>(std::max<int64_t>(0, wake - mono_ns()) / 1'000'000);
    serve(lfd, std::min(timeout_ms, 50));
    if (g.prune_ns > 0 && mono_ns() >= next_prune) {
      next_prune = mono_ns() + g.prune_ns;
      prune();
    }
  }
  // flush pending replies (e.g. to "shutdown") before exiting
  for (auto & [fd, c] : g_clients) {
    if (!c.out.empty()) {(void)!write(fd, c.out.data(), c.out.size());}
    close(fd);
  }
  close(lfd);
  unlink(sock_path.c_str());
  for (auto & gr : graphs) {bt_graph_put_ref(gr.graph);}
  log("exit");
  return rc;
}
