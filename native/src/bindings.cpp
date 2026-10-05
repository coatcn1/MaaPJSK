#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <deque>
#include <stdexcept>
#include <string>
#include <vector>

#include "maabangdream/minitouch_client.hpp"
#include "maabangdream/minitouch_log.hpp"

namespace py = pybind11;
using mbdr::TouchLatencyOffsets;

namespace {

struct Event {
    double time;
    int order, contact, x, y, index;
    std::string kind;
};

class Timeline {
public:
    explicit Timeline(const py::list& source) {
        std::array<bool, 10> active{};
        for (const auto item : source) {
            const auto value = item.cast<py::dict>();
            Event event{value["time"].cast<double>(), value["order"].cast<int>(),
                value["contact"].cast<int>(), value["x"].cast<int>(), value["y"].cast<int>(),
                static_cast<int>(events_.size()), value["kind"].cast<std::string>()};
            if (!std::isfinite(event.time) || event.contact < 0 || event.contact >= 10
                || event.x < 0 || event.x >= 1280 || event.y < 0 || event.y >= 720) {
                throw std::invalid_argument("谱面触控参数无效");
            }
            events_.push_back(event);
        }
        std::stable_sort(events_.begin(), events_.end(), [](const Event& a, const Event& b) {
            return a.time == b.time ? a.order < b.order : a.time < b.time;
        });
        for (const auto& event : events_) {
            if (event.kind == "down" && !active[event.contact]) active[event.contact] = true;
            else if (event.kind == "up" && active[event.contact]) active[event.contact] = false;
            else if (event.kind != "move" || !active[event.contact])
                throw std::invalid_argument("谱面触点生命周期无效");
        }
        if (events_.empty() || std::any_of(active.begin(), active.end(), [](bool v) { return v; }))
            throw std::invalid_argument("谱面为空或结束后仍有触点");
    }

    void start(double epoch, double now, int offset_ms) {
        if (started_ || !std::isfinite(epoch) || !std::isfinite(now)
            || std::abs(offset_ms) > 600) throw std::invalid_argument("Native 开场参数无效");
        epoch_ = epoch + offset_ms / 1000.0;
        if (epoch_ + events_.front().time < now - .020)
            throw std::runtime_error("第一音时刻已过，拒绝从歌曲中途开始");
        tail_ = now;
        started_ = true;
    }

    void set_future_phase_correction(double value_ms) {
        if (!started_ || !std::isfinite(value_ms) || std::abs(value_ms) > 60.0
            || std::abs(value_ms - phase_ms_) > 5.0 + 1e-9)
            throw std::invalid_argument("游戏相位反馈无效或超过单步限幅");
        // 不改冻结起点、游标和已发布队列；游戏反馈仅影响下一窗口的目标时刻。
        phase_ms_ = value_ms;
        phase_activated_ = phase_activated_ || value_ms != 0.0;
    }

    py::object next(double now) {
        if (!started_ || !std::isfinite(now)) throw std::runtime_error("Native 尚未锁定开场");
        if (position_ == events_.size() || tail_ - now > .200) return py::none();
        if (chunks_ && now - tail_ > .180) throw std::runtime_error("Native 队列断粮超过 180 ms");
        if (chunks_ && now > tail_ + .030) ++underflows_;
        const double start = tail_;
        const double end = std::min(std::max(start, now) + .500,
            std::max(start, epoch_ + events_.back().time + phase_ms_ / 1000.0));
        py::list actions;
        while (position_ < events_.size()
            && std::max(start, epoch_ + events_[position_].time + phase_ms_ / 1000.0) <= end + 1e-9) {
            const auto& event = events_[position_++];
            py::dict row;
            const double planned = epoch_ + event.time;
            // 负修正不能穿过已发布窗口尾部；同时间和弦仍保持同一目标和原顺序。
            row["time"] = !phase_activated_ ? planned : std::max(start, planned + phase_ms_ / 1000.0);
            row["planned_time"] = planned;
            row["game_phase_correction_ms"] = (row["time"].cast<double>() - planned) * 1000.0;
            row["kind"] = event.kind;
            row["contact"] = event.contact; row["x"] = event.x; row["y"] = event.y;
            row["index"] = event.index;
            actions.append(row);
        }
        tail_ = end;
        ++chunks_;
        py::dict result;
        result["sequence"] = chunks_; result["start"] = start; result["end"] = end;
        result["events"] = actions; result["final"] = position_ == events_.size();
        result["game_phase_correction_ms"] = phase_ms_;
        return result;
    }

    std::size_t sent() const { return position_; }
    int underflows() const { return underflows_; }

private:
    std::vector<Event> events_;
    std::size_t position_ = 0;
    double epoch_ = 0, tail_ = 0, phase_ms_ = 0;
    bool started_ = false, phase_activated_ = false;
    int chunks_ = 0, underflows_ = 0;
};

class ScriptCompiler {
public:
    void set_offsets(TouchLatencyOffsets value) { offsets_ = value; }
    void add_residual_ms(double value) {
        if (!std::isfinite(value)) throw std::invalid_argument("设备耗时补偿无效");
        residual_ += value;
    }
    py::dict compile(const py::dict& chunk, int max_x, int max_y, int rotation) {
        if (max_x <= 0 || max_y <= 0 || rotation < 0 || rotation > 3)
            throw std::invalid_argument("设备触摸面无效");
        auto active = active_;
        double residual = residual_, loss = loss_;
        double cursor = chunk["start"].cast<double>();
        std::vector<std::string> lines;
        py::list receipts;
        std::array<bool, 10> pending_contacts{};
        auto account = [&](double cost) { residual += offsets_.interval_ms + cost; };
        auto commit = [&]() {
            if (lines.empty() || lines.back() != "c") { account(0); lines.push_back("c"); }
            pending_contacts.fill(false);
        };
        auto wait = [&](double until) {
            double gap_ms = (until - cursor) * 1000.0;
            if (gap_ms <= 1e-6) return;
            // 与参考引擎一致，欠账和整数等待误差跨块保留，绝对谱面游标不随取整漂移。
            account(offsets_.wait_ms);
            // 已测得的等待成本也需偿还；只限于实际存在的正向等待，负向仍限制为 1 ms。
            const double positive_budget = std::min(gap_ms, 1.0 + std::max(0.0, offsets_.wait_ms + offsets_.interval_ms));
            const double adjustment = std::clamp(residual, -1.0, positive_budget);
            residual -= adjustment;
            const double compensated = gap_ms - adjustment;
            double remaining = std::max(0.0, compensated - std::clamp(loss, -2.0, 2.0));
            int emitted = 0, count = 0;
            while (remaining > 1e-6) {
                const double piece = std::min(250.0, remaining);
                const int rounded = static_cast<int>(std::lround(piece));
                if (rounded > 0) { commit(); lines.push_back("w " + std::to_string(rounded)); emitted += rounded; ++count; }
                remaining -= piece;
            }
            residual += (count - 1) * (offsets_.wait_ms + offsets_.interval_ms);
            loss += emitted - compensated;
            cursor = until;
        };
        const auto events = chunk["events"].cast<py::list>();
        for (const auto item : events) {
            const auto event = item.cast<py::dict>();
            const double when = event["time"].cast<double>();
            const std::string kind = event["kind"].cast<std::string>();
            const int contact = event["contact"].cast<int>();
            if (contact < 0 || contact >= 10 || !std::isfinite(when))
                throw std::invalid_argument("窗口触控参数无效");
            // 相同时间的和弦合并提交；复用同一触点时先提交抬起，避免覆盖生命周期。
            if (when > cursor + 1e-9) { commit(); wait(when); }
            if (pending_contacts[contact]) commit();
            pending_contacts[contact] = true;
            int x = event["x"].cast<int>(), y = event["y"].cast<int>();
            int mapped_x = x, mapped_y = y;
            if (rotation == 1) { mapped_x = max_x - 1 - y; mapped_y = x; }
            if (rotation == 2) { mapped_x = max_x - 1 - x; mapped_y = max_y - 1 - y; }
            if (rotation == 3) { mapped_x = y; mapped_y = max_y - 1 - x; }
            if (kind != "up" && (mapped_x < 0 || mapped_x > max_x || mapped_y < 0 || mapped_y > max_y))
                throw std::invalid_argument("旋转后的触控坐标超出设备范围");
            std::string command;
            if (kind == "down" && !active[contact]) {
                active[contact] = true; account(offsets_.down_ms); command = "d";
            } else if (kind == "up" && active[contact]) {
                active[contact] = false; account(offsets_.up_ms); command = "u";
            } else if (kind == "move" && active[contact]) {
                account(offsets_.move_ms); command = "m";
            } else throw std::invalid_argument("窗口触点生命周期无效");
            py::dict receipt;
            receipt["line"] = lines.size(); receipt["time"] = when; receipt["index"] = event["index"];
            receipt["planned_time"] = event.contains("planned_time") ? event["planned_time"] : event["time"];
            receipt["game_phase_correction_ms"] = (when - receipt["planned_time"].cast<double>()) * 1000.0;
            receipts.append(receipt);
            command += " " + std::to_string(contact);
            if (kind != "up") command += " " + std::to_string(mapped_x) + " " + std::to_string(mapped_y) + " 50";
            lines.push_back(command);
        }
        commit(); wait(chunk["end"].cast<double>()); commit();
        if (chunk["final"].cast<bool>() && std::any_of(active.begin(), active.end(), [](bool v) { return v; }))
            throw std::runtime_error("Native 最后一块仍有触点");
        active_ = active; residual_ = residual; loss_ = loss;
        py::dict result; result["lines"] = lines; result["receipts"] = receipts;
        return result;
    }
private:
    std::array<bool, 10> active_{};
    TouchLatencyOffsets offsets_;
    double residual_ = 0, loss_ = 0;
};

}

PYBIND11_MODULE(maapjsk_native, module) {
    module.def("version", []() { return "1.2.0"; });
    py::class_<Timeline>(module, "Timeline").def(py::init<py::list>())
        .def("start", &Timeline::start).def("next", &Timeline::next)
        .def("set_future_phase_correction", &Timeline::set_future_phase_correction)
        .def_property_readonly("sent", &Timeline::sent).def_property_readonly("underflows", &Timeline::underflows);
    py::class_<TouchLatencyOffsets>(module, "TouchLatencyOffsets")
        .def(py::init<>()).def_readwrite("down_ms", &TouchLatencyOffsets::down_ms)
        .def_readwrite("up_ms", &TouchLatencyOffsets::up_ms).def_readwrite("move_ms", &TouchLatencyOffsets::move_ms)
        .def_readwrite("wait_ms", &TouchLatencyOffsets::wait_ms).def_readwrite("interval_ms", &TouchLatencyOffsets::interval_ms);
    py::class_<ScriptCompiler>(module, "ScriptCompiler").def(py::init<>())
        .def("compile", &ScriptCompiler::compile).def("set_offsets", &ScriptCompiler::set_offsets)
        .def("add_residual_ms", &ScriptCompiler::add_residual_ms);
    py::class_<mbdr::MinitouchClient>(module, "MinitouchClient").def(py::init<>())
        .def("connect", &mbdr::MinitouchClient::connect, py::call_guard<py::gil_scoped_release>())
        .def("publish", [](mbdr::MinitouchClient& self, const std::string& value) {
            py::gil_scoped_release release; return self.publish(value);
        })
        .def("receive", &mbdr::MinitouchClient::receive, py::call_guard<py::gil_scoped_release>())
        .def("close", &mbdr::MinitouchClient::close)
        .def_property_readonly("connected", &mbdr::MinitouchClient::connected)
        .def_property_readonly("last_publish_diagnostics", [](const mbdr::MinitouchClient& self) {
            const auto d = self.last_publish_diagnostics(); py::dict result;
            result["payload_bytes"] = d.payload_bytes; result["send_calls"] = d.send_calls;
            result["sent_bytes"] = d.sent_bytes; result["success"] = d.success; return result;
        });
    module.def("parse_minitouch_log", [](const std::string& line) -> py::object {
        mbdr::MinitouchLogEvent event;
        if (!mbdr::parse_minitouch_log(line, &event)) return py::none();
        py::dict result; result["start_ms"] = event.start_ms; result["end_ms"] = event.end_ms;
        result["cost_ms"] = event.cost_ms; result["command"] = event.command; return result;
    });
    py::class_<mbdr::LatencyCalibrator>(module, "LatencyCalibrator").def(py::init<>())
        .def("observe", [](mbdr::LatencyCalibrator& self, const py::dict& value) {
            self.observe({value["start_ms"].cast<double>(), value["end_ms"].cast<double>(),
                value["cost_ms"].cast<double>(), value["command"].cast<std::string>()});
        })
        .def_property_readonly("offsets", &mbdr::LatencyCalibrator::offsets)
        .def_property_readonly("sample_counts", [](const mbdr::LatencyCalibrator& self) {
            const auto c = self.sample_counts(); py::dict result;
            result["down"] = c.down; result["up"] = c.up; result["move"] = c.move;
            result["wait"] = c.wait; result["interval"] = c.interval; return result;
        })
        .def("correction_ms", &mbdr::LatencyCalibrator::correction_ms)
        .def("reset", &mbdr::LatencyCalibrator::reset);
}
