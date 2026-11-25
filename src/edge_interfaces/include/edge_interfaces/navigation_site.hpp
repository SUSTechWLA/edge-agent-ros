#pragma once

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace edge {
struct NavigationSite {
  double xmin, ymin, xmax, ymax;
  std::chrono::seconds motion_deadline;

  template<class Node>
  static NavigationSite load(Node& node, int default_seconds) {
    const auto box = node.template declare_parameter<std::vector<double>>(
        "site_bounds_xyxy_m", {-4., -4., 4., 4.});
    const auto seconds = node.template declare_parameter<std::int64_t>(
        "motion_deadline_s", default_seconds);
    if (box.size() != 4 ||
        !std::all_of(box.begin(), box.end(), [](double v) { return std::isfinite(v); }) ||
        box[0] >= box[2] || box[1] >= box[3])
      throw std::invalid_argument("site_bounds_xyxy_m requires ordered finite limits");
    if (seconds <= 0 || seconds > 3600)
      throw std::invalid_argument("motion_deadline_s outside (0, 3600]");
    return {box[0], box[1], box[2], box[3], std::chrono::seconds(seconds)};
  }

  bool contains(double x, double y) const {
    return x >= xmin && x <= xmax && y >= ymin && y <= ymax;
  }
};
}  // namespace edge
