"""Encode one frozen camera surface and its original RGB PNG as a self-contained GLB."""
import struct

from .core import canonical, digest, number, reference, require


def encode_textured_glb(scene_ref, layer, surface, png):
    mapping = surface.get("texture_mapping", {})
    require(mapping.get("schema_version") == 1 and mapping.get("origin") == "top_left"
            and mapping.get("coordinates") == "normalized_pixel_centers"
            and surface.get("axes") == "x_right_y_down_z_forward",
            "scene.unsupported_texture", "Expected a calibrated optical surface with top-left UV coordinates")
    require(layer["content_ref"] == reference(surface)
            and mapping.get("image_ref") == layer["source_refs"]["color"] == digest(png),
            "scene.artifact_mismatch", "Geometry or RGB raster differs from the frozen layer")
    vertices, triangles, uv = surface["vertices"], surface["triangles"], mapping["uv"]
    require(bool(triangles), "scene.no_texturable_surface", "Depth has no connected surface to export")
    require(len(uv) == len(vertices), "scene.invalid_texture", "UV count does not match the vertex grid")

    # Compact only vertices actually used by observed faces. This preserves the holes.
    indices = []
    used = {}
    positions = bytearray()
    texcoords = bytearray()
    for face in triangles:
        require(len(face) == 3, "scene.invalid_texture", "Expected triangle indices")
        for source in face:
            require(type(source) is int and 0 <= source < len(vertices),
                    "scene.invalid_texture", "Face refers outside the vertex grid")
            if source not in used:
                vertex, coord = vertices[source], uv[source]
                require(isinstance(vertex, list) and len(vertex) >= 3 and all(number(x) for x in vertex[:3])
                        and isinstance(coord, list) and len(coord) == 2
                        and all(number(x) and 0 <= x <= 1 for x in coord),
                        "scene.invalid_texture", "Face references an unobserved or untextured vertex")
                used[source] = len(used)
                x, y, z = vertex[:3]
                positions.extend(struct.pack("<fff", x, -y, -z))  # optical to +Y up / camera facing -Z
                texcoords.extend(struct.pack("<ff", *coord))
            indices.append(used[source])
    axes = list(zip(*struct.iter_unpack("<fff", positions)))
    binary = bytearray()
    views = []

    def add_view(data, target=None):
        binary.extend(bytes(-len(binary) % 4))
        view = {"buffer": 0, "byteOffset": len(binary), "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        views.append(view)
        binary.extend(data)
        return len(views) - 1

    pos_view = add_view(positions, 34962)
    uv_view = add_view(texcoords, 34962)
    index_view = add_view(struct.pack(f"<{len(indices)}I", *indices), 34963)
    image_view = add_view(png)
    model = {
        "asset": {"version": "2.0", "generator": "edge-agent-ros",
                  "extras": {"scene_ref": scene_ref, "layer_ref": layer["layer_ref"], "source_refs": layer["source_refs"],
                             "frame_id": layer["frame_id"], "stamp_ns": layer["stamp_ns"],
                             "optical_to_gltf_axes": ["x", "-y", "-z"], "unit": "m"}},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": "RGB-D surface"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2, "material": 0}]}],
        "extensionsUsed": ["KHR_materials_unlit"],
        "materials": [{"doubleSided": True, "extensions": {"KHR_materials_unlit": {}},
                       "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "metallicFactor": 0}}],
        "samplers": [{"magFilter": 9729, "minFilter": 9729, "wrapS": 33071, "wrapT": 33071}],
        "textures": [{"sampler": 0, "source": 0}], "images": [{"bufferView": image_view, "mimeType": "image/png"}],
        "buffers": [{"byteLength": len(binary)}], "bufferViews": views,
        "accessors": [
            {"bufferView": pos_view, "componentType": 5126, "count": len(used), "type": "VEC3",
             "min": [min(axis) for axis in axes], "max": [max(axis) for axis in axes]},
            {"bufferView": uv_view, "componentType": 5126, "count": len(used), "type": "VEC2"},
            {"bufferView": index_view, "componentType": 5125, "count": len(indices), "type": "SCALAR"}],
    }
    json_chunk = canonical(model)
    json_chunk += b" " * (-len(json_chunk) % 4)
    binary.extend(bytes(-len(binary) % 4))
    return b"".join((struct.pack("<4sII", b"glTF", 2, 28 + len(json_chunk) + len(binary)),
                     struct.pack("<I4s", len(json_chunk), b"JSON"), json_chunk,
                     struct.pack("<I4s", len(binary), b"BIN\0"), binary))
