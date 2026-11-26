"""Write a chaptered MP4 from held evidence frames and verify the decoded stream."""
import json
import subprocess

from .sensor_review import FPS, H, W, require


def write_video(target, scenes, ffmpeg, ffprobe):
    encode = [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
              "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264",
              "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
              "-movflags", "+faststart", str(target)]
    count = 0
    chapters = []
    with subprocess.Popen(encode, stdin=subprocess.PIPE) as proc:
        try:
            for title, seconds, image in scenes:
                require(image.size == (W, H) and image.mode == "RGB", "unsupported chapter frame")
                frames = round(seconds * FPS)
                require(frames > 0, "empty chapter")
                chapters.append({"title": title, "sec": count / FPS})
                frame = image.tobytes()
                for _ in range(frames):
                    proc.stdin.write(frame)
                count += frames
        finally:
            proc.stdin.close()
        require(proc.wait() == 0, "review video encoding failed")
    probe = [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=width,height,codec_name,nb_read_frames", "-of", "json", str(target)]
    stream, = json.loads(subprocess.check_output(probe))["streams"]
    require((stream["codec_name"], int(stream["nb_read_frames"]), stream["width"], stream["height"])
            == ("h264", count, W, H), "review video failed decode verification")
    return chapters, count
