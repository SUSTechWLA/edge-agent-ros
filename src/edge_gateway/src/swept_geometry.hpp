#pragma once

#include "local_planner.hpp"
#include <algorithm>
#include <cmath>

namespace edge::local::geometry {
inline double sq(double x) { return x*x; }
inline double to_segment2(Point p,Point a,Point b) {
  const double dx=b.x-a.x,dy=b.y-a.y,len2=sq(dx)+sq(dy);
  const double f=len2 ? std::clamp(((p.x-a.x)*dx+(p.y-a.y)*dy)/len2,0.,1.) : 0.;
  return sq(p.x-a.x-f*dx)+sq(p.y-a.y-f*dy);
}

// Exact squared Euclidean distance between a segment and a closed rectangle.
inline double box_distance2(Point a,Point b,double hx,double hy) {
  const double dx=b.x-a.x,dy=b.y-a.y;
  if (std::min(a.x,b.x)<=hx && std::max(a.x,b.x)>=-hx &&
      std::min(a.y,b.y)<=hy && std::max(a.y,b.y)>=-hy &&
      std::abs(a.x*dy-a.y*dx)<=hx*std::abs(dy)+hy*std::abs(dx)) return 0.;
  auto endpoint=[&](Point p) {
    return sq(std::max(0.,std::abs(p.x)-hx))+sq(std::max(0.,std::abs(p.y)-hy));
  };
  double result=std::min(endpoint(a),endpoint(b));
  for (double x:{-hx,hx}) for (double y:{-hy,hy})
    result=std::min(result,to_segment2({x,y},a,b));
  return result;
}
}
