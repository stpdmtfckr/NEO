# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Owner-only Telegram remote mouse helpers.

Screenshots use Windows virtual-desktop/global coordinates, including negative
coordinates on monitors positioned left/above the primary display.
"""

from pathlib import Path
from typing import Any
import ctypes
from ctypes import wintypes
import math
import os


def monitor_inventory() -> list[dict[str, int]]:
    import mss
    with mss.mss() as sct:
        rows=[]
        for idx, mon in enumerate(sct.monitors[1:], 1):
            rows.append({"index":idx,"left":int(mon["left"]),"top":int(mon["top"]),"width":int(mon["width"]),"height":int(mon["height"])})
        return rows


def monitor_by_index(index: int) -> dict[str, int]:
    rows=monitor_inventory()
    for row in rows:
        if int(row["index"]) == int(index): return row
    raise ValueError(f"Монитор {index} не найден")


def capture_coordinate_grid(config: Any, index: int) -> tuple[str, dict[str, int]]:
    import mss
    from PIL import Image, ImageDraw, ImageFont
    mon=monitor_by_index(index)
    with mss.mss() as sct:
        shot=sct.grab({k:mon[k] for k in ("left","top","width","height")})
        img=Image.frombytes("RGB", shot.size, shot.rgb)
    draw=ImageDraw.Draw(img, "RGBA")
    # Around 10-16 cells per axis. Thin high-contrast double stroke without
    # covering meaningful screen content.
    step_x=max(100, int(math.ceil(mon["width"] / 12 / 50.0)*50))
    step_y=max(100, int(math.ceil(mon["height"] / 10 / 50.0)*50))
    # Large coordinate labels remain readable after Telegram compresses/scales
    # the screenshot on a phone. Prefer Windows Segoe UI, then Arial, then PIL.
    font = None
    font_px = max(18, min(30, int(round(min(mon["width"], mon["height"]) / 52.0))))
    for candidate in (r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(candidate, font_px)
            break
        except Exception:
            continue
    if font is None:
        try: font = ImageFont.load_default()
        except Exception: font = None
    for gx in range((mon["left"]//step_x)*step_x, mon["left"]+mon["width"]+step_x, step_x):
        lx=gx-mon["left"]
        if 0 <= lx < mon["width"]:
            draw.line([(lx,0),(lx,mon["height"]-1)], fill=(0,0,0,145), width=2)
            draw.line([(lx,0),(lx,mon["height"]-1)], fill=(255,255,255,165), width=1)
            label = f"X {gx}"
            try: box = draw.textbbox((0, 0), label, font=font)
            except Exception: box = (0, 0, 72, font_px + 4)
            tw, th = max(72, box[2]-box[0]+12), max(font_px+8, box[3]-box[1]+10)
            x0 = max(2, min(mon["width"]-tw-2, lx+3))
            draw.rounded_rectangle([(x0,3),(x0+tw,3+th)],radius=5,fill=(0,0,0,205),outline=(255,255,255,210),width=2)
            draw.text((x0+6,5), label, fill=(255,255,255,255), font=font)
    for gy in range((mon["top"]//step_y)*step_y, mon["top"]+mon["height"]+step_y, step_y):
        ly=gy-mon["top"]
        if 0 <= ly < mon["height"]:
            draw.line([(0,ly),(mon["width"]-1,ly)], fill=(0,0,0,145), width=2)
            draw.line([(0,ly),(mon["width"]-1,ly)], fill=(255,255,255,165), width=1)
            label = f"Y {gy}"
            try: box = draw.textbbox((0, 0), label, font=font)
            except Exception: box = (0, 0, 76, font_px + 4)
            tw, th = max(76, box[2]-box[0]+12), max(font_px+8, box[3]-box[1]+10)
            y0 = max(2, min(mon["height"]-th-2, ly+3))
            draw.rounded_rectangle([(3,y0),(3+tw,y0+th)],radius=5,fill=(0,0,0,205),outline=(255,255,255,210),width=2)
            draw.text((9,y0+2), label, fill=(255,255,255,255), font=font)
    # Border and exact coordinate range.
    draw.rectangle([(0,0),(mon["width"]-1,mon["height"]-1)], outline=(255,255,255,220), width=2)
    caption=f"Экран {index}: X {mon['left']}..{mon['left']+mon['width']-1}, Y {mon['top']}..{mon['top']+mon['height']-1}"
    try: cbox = draw.textbbox((0, 0), caption, font=font)
    except Exception: cbox = (0, 0, min(mon["width"]-24, len(caption)*8), font_px+4)
    cw = min(mon["width"]-16, max(120, cbox[2]-cbox[0]+16))
    ch = max(font_px+10, cbox[3]-cbox[1]+12)
    cy0 = mon["height"]-ch-8
    draw.rounded_rectangle([(8,cy0),(8+cw,mon["height"]-8)],radius=6,fill=(0,0,0,215),outline=(255,255,255,220),width=2)
    draw.text((16,cy0+4),caption,fill=(255,255,255,255),font=font)
    root=Path(config.paths.root)/"telegram_mouse"
    root.mkdir(parents=True,exist_ok=True)
    path=root/f"monitor_{index}_grid.png"
    img.save(path,"PNG",optimize=True)
    return str(path),mon


def click_global(index: int, x: int, y: int, button: str) -> dict[str, Any]:
    if os.name != "nt": raise RuntimeError("Удалённый клик мышью поддерживается только в Windows")
    mon=monitor_by_index(index)
    x=int(x); y=int(y)
    if not (mon["left"] <= x < mon["left"]+mon["width"] and mon["top"] <= y < mon["top"]+mon["height"]):
        raise ValueError(f"Координаты X={x}, Y={y} находятся вне выбранного экрана {index}")
    b=str(button or "left").casefold()
    if b not in {"left","right"}: raise ValueError("Разрешена только левая или правая кнопка мыши")
    user32=ctypes.WinDLL("user32", use_last_error=True)
    user32.SetCursorPos.argtypes=(ctypes.c_int,ctypes.c_int)
    user32.SetCursorPos.restype=wintypes.BOOL
    if not user32.SetCursorPos(x,y):
        raise OSError(ctypes.get_last_error(), "Windows не смог переместить курсор")

    ULONG_PTR=ctypes.c_size_t
    class MOUSEINPUT(ctypes.Structure):
        _fields_=[("dx",wintypes.LONG),("dy",wintypes.LONG),("mouseData",wintypes.DWORD),("dwFlags",wintypes.DWORD),("time",wintypes.DWORD),("dwExtraInfo",ULONG_PTR)]
    class KEYBDINPUT(ctypes.Structure):
        _fields_=[("wVk",wintypes.WORD),("wScan",wintypes.WORD),("dwFlags",wintypes.DWORD),("time",wintypes.DWORD),("dwExtraInfo",ULONG_PTR)]
    class HARDWAREINPUT(ctypes.Structure):
        _fields_=[("uMsg",wintypes.DWORD),("wParamL",wintypes.WORD),("wParamH",wintypes.WORD)]
    class INPUTUNION(ctypes.Union):
        _fields_=[("mi",MOUSEINPUT),("ki",KEYBDINPUT),("hi",HARDWAREINPUT)]
    class INPUT(ctypes.Structure):
        _anonymous_=("u",); _fields_=[("type",wintypes.DWORD),("u",INPUTUNION)]

    INPUT_MOUSE=0
    down,up=((0x0002,0x0004) if b=="left" else (0x0008,0x0010))
    events=(INPUT*2)(
        INPUT(type=INPUT_MOUSE,mi=MOUSEINPUT(0,0,0,down,0,0)),
        INPUT(type=INPUT_MOUSE,mi=MOUSEINPUT(0,0,0,up,0,0)),
    )
    user32.SendInput.argtypes=(wintypes.UINT,ctypes.POINTER(INPUT),ctypes.c_int)
    user32.SendInput.restype=wintypes.UINT
    ctypes.set_last_error(0)
    sent=int(user32.SendInput(2,events,ctypes.sizeof(INPUT)) or 0)
    if sent != 2:
        # Some Windows builds/drivers reject the ctypes INPUT layout even though
        # SetCursorPos succeeds. Fall back to the documented legacy mouse_event
        # down/up pair before reporting an error. This does not bypass UIPI; it
        # simply removes an ABI/layout failure from ordinary desktop clicks.
        user32.mouse_event.argtypes=(wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.c_size_t)
        user32.mouse_event.restype=None
        try:
            user32.mouse_event(down,0,0,0,0)
            user32.mouse_event(up,0,0,0,0)
            sent = 2
        except Exception:
            code=int(ctypes.get_last_error() or 0)
            detail=f"Windows не смог отправить клик мыши (SendInput {sent}/2)"
            if sent == 0:
                detail += ". Для окна администратора NEO должен иметь тот же уровень прав (Windows UIPI)."
            raise OSError(code,detail)

    class POINT(ctypes.Structure):
        _fields_=[("x",wintypes.LONG),("y",wintypes.LONG)]
    point=POINT()
    cursor_verified=bool(user32.GetCursorPos(ctypes.byref(point))) and int(point.x)==x and int(point.y)==y
    return {"monitor":index,"x":x,"y":y,"button":b,"verified":bool(cursor_verified and sent==2),"events_sent":sent,"cursor_verified":cursor_verified}
