#pragma once

namespace mbdr {

// 保留 MaaBanGDream 回读统计器的字段协议；PJSK 自行编译十二轨低层事件。
struct TouchLatencyOffsets {
    double down_ms = 0.0;
    double up_ms = 0.0;
    double move_ms = 0.0;
    double wait_ms = 0.0;
    double interval_ms = 0.0;
};

}
