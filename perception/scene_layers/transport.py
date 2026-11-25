"""Loopback DSH service calls for the isolated scene replay site."""
import http.client
import json
from urllib.parse import urlsplit

from edge_contracts.service_directory import NAME, PluginUnavailable, ServiceSnapshot


MAX_REQUEST = 128 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
VERSION = "1.0.0"
# Site release entries. Layer kinds select independently owned DSH service exports.
SERVICES = {
    "occupancy": "map.mid360.occupancy",
    "texture": "map.rgbd.surface",
    "grid2d": "map.nav2.grid",
    "fusion": "map.lidar_rgb.fused",
    "query": "map.scene.query",
}


class SceneRuntimeError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def exports_for_site(directory):
    """Optional trusted, read-only projection from layer kinds to unique DSH services."""
    try:
        with (directory.parent/"scene-layer-services.json").open("rb") as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        return SERVICES
    if len(raw) > 65536:
        raise SceneRuntimeError("scene.invalid_site_index")
    try:
        index = json.loads(raw)
        layers = index["layers"]
        if (set(index) != {"schema_version", "layers"} or type(index["schema_version"]) is not int or
                index["schema_version"] != 1 or not isinstance(layers, dict) or len(layers) > 128 or
                any(not isinstance(k, str) or not NAME.fullmatch(k) or not isinstance(v, str) or
                    not NAME.fullmatch(v) or k in SERVICES or v in SERVICES.values() for k, v in layers.items()) or
                len(set(layers.values())) != len(layers)):
            raise ValueError("invalid map layer registration")
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise SceneRuntimeError("scene.invalid_site_index") from exc
    return {**SERVICES, **layers}


def pin_service(directory, kind, expected=None, catalog=None):
    index = exports_for_site(directory)
    if not isinstance(kind, str) or kind not in index:
        raise SceneRuntimeError("scene.unsupported_layer")
    try:
        # Historical bindings can normally finish during draining. This method
        # admits new reads, including those asking for an old snapshot.
        pin = (catalog or ServiceSnapshot(directory)).pin(index[kind])
    except PluginUnavailable as error:
        raise SceneRuntimeError(error.code, str(error)) from error
    if expected is not None and pin["binding"] != expected:
        raise SceneRuntimeError("verification.binding_unavailable")
    return pin


def call(directory, provider_kind, operation, *, expected=None, **args):
    pin = pin_service(directory, provider_kind, expected)
    endpoint = urlsplit(pin["endpoint"])
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.path != "/invoke":
        raise SceneRuntimeError("scene.invalid_private_endpoint")
    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=8)
    payload = json.dumps({"binding": pin["binding"], "operation": operation, "arguments": args},
                         ensure_ascii=False, allow_nan=False).encode()
    if len(payload) > MAX_REQUEST:
        raise SceneRuntimeError("scene.request_too_large")
    try:
        connection.request("POST", endpoint.path, payload, {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise SceneRuntimeError("scene.reply_too_large")
        if response.status != 200:
            error = json.loads(data)["error"]
            raise SceneRuntimeError(error["code"], error["message"])
        if response.getheader("X-DSH-Binding") != pin["binding"]:
            raise SceneRuntimeError("verification.binding_unavailable")
        if operation == "texture_png":
            if response.getheader("Content-Type") != "image/png":
                raise SceneRuntimeError("scene.invalid_texture")
            return data
        return json.loads(data)
    except (OSError, http.client.HTTPException) as exc:
        raise SceneRuntimeError("plugin.service_unavailable", str(exc)) from exc
    finally:
        connection.close()
