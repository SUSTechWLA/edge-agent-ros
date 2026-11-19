"""Check the actual offline map player against public recording evidence and decode its MP4."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def verify(recording, video, out, browser_path, ffmpeg):
    from playwright.sync_api import sync_playwright
    out.mkdir(parents=True,exist_ok=False)
    exported=json.loads((video/"video.json").read_bytes())
    head=json.loads((recording/"recording.json").read_bytes())
    timeline=recording/"media/mission/timeline.jsonl"
    assert sha(timeline)==exported["timeline_sha256"] and sha(video/"task.mp4")==exported["video_sha256"]
    assert head["task_id"]==exported["task_id"] and head["status"]!="ACTIVE"
    wire=[json.loads(x) for x in timeline.read_bytes().splitlines() if x]
    rows={e["seq"]:e for e in wire}
    subprocess.run([ffmpeg,"-nostdin","-v","error","-xerror","-i",str(video/"task.mp4"),"-map","0:v:0","-f","null","-"],check=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=browser_path,headless=True)
        page=browser.new_page(viewport={"width":1360,"height":900})
        errors=[]
        page.on("pageerror",lambda e:errors.append(str(e)))
        try:
            page.goto((video/"map.html").as_uri())
            model=page.locator("#data").evaluate("e=>JSON.parse(e.textContent)")
            rev=model["revisions"]
            assert len(rev)==exported["map"]["revisions"]
            for r in rev:
                if r.get("base"): continue
                source=rows[r["seq"]]
                assert source["kind"]=="map" and source["event_id"]==r["event_id"]
                assert r["sha256"]==source["sha256"]==sha(recording/"media/mission"/source["file"])
                assert str(source["stamp_ns"])==r["stamp_ns"]
            def seek(ms):
                page.locator("#time").evaluate("(e,n)=>{e.value=n;e.dispatchEvent(new Event('input'))}",ms)
            def visible_count():
                return int(page.locator("#known").inner_text().replace(",",""))
            def pixels():
                return hashlib.sha256(page.locator("#map").evaluate("c=>c.toDataURL()").encode()).hexdigest()
            total=source_counts=0
            results=dict(task_id=head["task_id"],video_sha256=exported["video_sha256"],timeline_sha256=exported["timeline_sha256"],
                         frames=exported["frames"],duration_s=exported["duration_s"],full_decode=True,revision_samples=[])
            faults=model.get("diagnostics",[])
            assert faults==exported["map"].get("diagnostics",[])
            if faults:
                page.locator("#mapFaults summary").click()
                assert page.locator("#faultLinks button").count()==len(faults)
                for index,fault in enumerate(faults):
                    row=rows[fault["seq"]]
                    assert row["kind"]=="map" and row["event_id"]==fault["event_id"]
                    page.locator("#faultLinks button").nth(index).click()
                    assert int(page.locator("#time").input_value())==fault["t"]
                    expected=next((r["known_cells"] for r in reversed(rev) if r["t"]<=fault["t"]),0)
                    assert visible_count()==expected
                page.locator("#mapFaults summary").click()
            if rev:
                seek(0)
                initial=next((r["known_cells"] for r in reversed(rev) if r["t"]<=0),0)
                assert visible_count()==initial
                page.screenshot(path=str(out/"opening.png"))
                for i in sorted({0,len(rev)//3,len(rev)//2,len(rev)-1}):
                    r=rev[i];seek(r["t"])
                    at=next(q for q in reversed(rev) if q["t"]<=r["t"])
                    assert visible_count()==at["known_cells"]
                    results["revision_samples"].append(dict(ms=r["t"],segment=at["segment"],known_cells=visible_count(),canvas_sha256=pixels()))
                seek(rev[len(rev)//2]["t"])
                before=pixels();page.locator("#plus").click();assert pixels()!=before
                page.locator("#fit").click();assert pixels()==before
                page.locator("#map").focus();page.keyboard.press("ArrowRight")
                assert int(page.locator("#time").input_value())>=rev[len(rev)//2]["t"]
                page.locator("#highlight").click();assert page.locator("#highlight").get_attribute("aria-pressed")=="false"
                page.locator("#highlight").click();page.screenshot(path=str(out/"exploration.png"))
                seek(model["duration"]);assert visible_count()==rev[-1]["known_cells"]
                total=visible_count()
                page.locator("#saves summary").click()
                assert page.locator("#savedLinks a").count()==len(exported["map"]["segments"])
                original_url=page.url
                for i,s in enumerate(exported["map"]["segments"]):
                    page.locator("#savedLinks a").nth(i).click()
                    page.wait_for_url((video/s["folder"]/"index.html").as_uri())
                    assert page.locator("#transport").is_hidden()
                    native=json.loads((video/s["folder"]/"manifest.json").read_bytes())
                    assert visible_count()==native["known_cells"] and sha(video/s["folder"]/"manifest.json")==s["manifest_sha256"]
                    page.goto(original_url)
                    page.locator("#saves summary").click()
                if model["cameras"]: assert page.locator("#photo").is_hidden()
                for k,shots in model["cameras"].items():
                    page.select_option("#role",k);seek(model["duration"])
                    page.wait_for_function("document.querySelector('#image').naturalWidth>0")
                    source_counts+=len(shots)
                if not model["cameras"]: assert page.locator("#camera").is_hidden()
                seek(0);assert visible_count()==initial
            elif faults:
                assert page.locator("#grid").is_visible() and visible_count()==0
                assert page.locator("#mapFault").is_visible() and not exported["map"]["segments"]
            else:
                assert page.locator("#grid").is_hidden() and page.locator("#software").is_visible()
                assert page.locator("#events li").count()==len(model["events"])
            page.set_viewport_size({"width":390,"height":800})
            assert page.evaluate("document.documentElement.scrollWidth")<=390
            assert not errors,errors
            results.update(final_segment_known=total,segments=len(exported["map"]["segments"]),camera_preview_samples=source_counts,
                           camera_roles=sorted(model["cameras"]),mobile_width=390,page_errors=errors,
                           rejected_map_samples=len(faults))
        finally:
            browser.close()
    (out/"qa.json").write_text(json.dumps(results,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    print(json.dumps({k:results[k] for k in ("task_id","segments","frames","final_segment_known","camera_roles")}))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording",type=Path,required=True)
    parser.add_argument("--video",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--browser")
    parser.add_argument("--ffmpeg",default="ffmpeg")
    a=parser.parse_args()
    verify(a.recording.resolve(),a.video.resolve(),a.out.resolve(),a.browser,a.ffmpeg)
