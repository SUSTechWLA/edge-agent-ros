"""Install the pinned Delft campus reconstruction into an offline scene build."""
import gzip
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


DATA = Path(__file__).with_name("real_site_data")
SITE_XY = (395, 12)


def append_site(world, output):
    """Add one relocated, metre-scale, east/north/up copy of the survey."""
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    target = Path(output) / "real_site"
    target.mkdir(parents=True, exist_ok=True)
    for filename, digest in manifest["runtime_files"].items():
        path = DATA / filename
        content = gzip.decompress(path.read_bytes()) if filename.endswith(".gz") else path.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f"Invalid Delft campus map asset: {filename}")
        (target / filename.removesuffix(".gz")).write_bytes(content)
    model = ET.SubElement(world, "model", name="delft_surveyed_site")
    ET.SubElement(model, "static").text = "true"
    ET.SubElement(model, "pose").text = f"{SITE_XY[0]} {SITE_XY[1]} 0 0 0 0"
    link = ET.SubElement(model, "link", name="site")
    for kind, name in (("visual", "terrain"), ("visual", "buildings"), ("collision", "buildings")):
        element = ET.SubElement(link, kind, name=f"{name}_{kind}")
        mesh = ET.SubElement(ET.SubElement(element, "geometry"), "mesh")
        ET.SubElement(mesh, "uri").text = f"file:///campus/real_site/{name}.obj"
    west, south, east, north = manifest["bbox_rd_m"]
    x, y = SITE_XY
    return {
        "id": "campus", "label": "代尔夫特理工大学 · 实测建筑群",
        "bounds": [x, y, x + east - west, y + north - south],
        "center": [x + (east-west)/2, y + (north-south)/2],
        "origin": "荷兰 3DBAG LoD2.2 + PDOK 2025 正射影像",
        "description": f"{manifest['buildings']} 栋建筑，保留实测重建的屋顶、轮廓和高度。地面与屋顶采用航片，立面窗格为示意材质。",
        "camera": {"eye": [980, 82, 115], "target": [757, 330, 18]},
        "street_camera": {"eye": [724, 455, 3.2], "target": [751, 257, 39]},
    }
