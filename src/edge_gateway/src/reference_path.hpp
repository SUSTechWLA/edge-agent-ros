#pragma once
#include "local_planner.hpp"
#include <algorithm>
#include <cmath>

namespace edge::local {
// Advance only along consecutive segments. A later lap at the same XY cannot
// consume intervening geometry. The reference never authorizes motion itself.
class ReferencePath {
public:
  explicit ReferencePath(std::vector<Point> points) : points_(std::move(points)) {}
  struct Target { Point point; double remaining; };
  Target target(Point robot, double lookahead=.65) {
    auto length=[this](std::size_t i) {
      return std::hypot(points_[i+1].x-points_[i].x,points_[i+1].y-points_[i].y);
    };
    auto projection=[&](std::size_t i) {
      const auto a=points_[i], b=points_[i+1];
      const double l=length(i);
      return l>1e-9 ? std::clamp(((robot.x-a.x)*(b.x-a.x)+(robot.y-a.y)*(b.y-a.y))/l,0.,l) : 0.;
    };
    auto error=[&](std::size_t i,double at) {
      const auto a=points_[i],b=points_[i+1];
      const double f=at/std::max(length(i),1e-9);
      return std::hypot(robot.x-a.x-f*(b.x-a.x),robot.y-a.y-f*(b.y-a.y));
    };
    along_=std::max(along_,projection(segment_));
    while (segment_+2<points_.size()) {
      const double next=projection(segment_+1);
      if (along_+1e-6<length(segment_) &&
          !(next>1e-6 && error(segment_+1,next)<error(segment_,along_))) break;
      ++segment_; along_=next;
    }
    double remaining=length(segment_)-along_;
    for (std::size_t i=segment_+1;i+1<points_.size();++i) remaining+=length(i);
    double ahead=along_+lookahead;
    for (std::size_t i=segment_;i+1<points_.size();++i) {
      const double l=length(i);
      if (l>1e-9 && ahead<l) {
        const auto a=points_[i],b=points_[i+1];
        return {{a.x+(b.x-a.x)*ahead/l,a.y+(b.y-a.y)*ahead/l},remaining};
      }
      ahead-=l;
    }
    return {points_.back(),remaining};
  }
private:
  std::vector<Point> points_;
  std::size_t segment_{};
  double along_{};
};
}
