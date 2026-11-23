#pragma once

#include <json-c/json.h>
#include <memory>
#include <stdexcept>
#include <string>

#include "local_planner.hpp"

namespace edge::local {
// The file is selected when the physical Gateway starts. A different reference
// point would change the sweep even if the overall length and width matched.
inline VehicleFootprint load_vehicle_profile(const std::string& filename) {
  std::unique_ptr<json_object, decltype(&json_object_put)> doc(
      json_object_from_file(filename.c_str()), json_object_put);
  if (!doc || !json_object_is_type(doc.get(), json_type_object))
    throw std::invalid_argument("vehicle_profile must name a readable JSON object");
  auto field = [&](const char* name) {
    json_object* value = nullptr;
    if (!json_object_object_get_ex(doc.get(), name, &value))
      throw std::invalid_argument(std::string("vehicle_profile missing ") + name);
    return value;
  };
  auto text = [&](const char* name) -> std::string {
    auto* value = field(name);
    if (!json_object_is_type(value, json_type_string))
      throw std::invalid_argument(std::string("vehicle_profile expected string ") + name);
    return json_object_get_string(value);
  };
  auto meters = [&](const char* name) {
    auto* value = field(name);
    if (!json_object_is_type(value, json_type_double) && !json_object_is_type(value, json_type_int))
      throw std::invalid_argument(std::string("vehicle_profile expected meters for ") + name);
    return json_object_get_double(value);
  };
  auto* version=field("schema_version");
  if (!json_object_is_type(version,json_type_int) || json_object_get_int64(version)!=1 ||
      text("kinematics")!="differential_drive" || text("reference_frame")!="base_link" ||
      text("reference_point")!="drive_center")
    throw std::invalid_argument("unsupported vehicle footprint schema, drive, frame or reference point");
  return {text("profile_id"), meters("front_m"), meters("rear_m"),
          meters("left_m"), meters("right_m"), meters("track_m")};
}
}  // namespace edge::local
