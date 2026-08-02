import socket
import struct
import threading
import sys
import queue
import time
import json
import os

# Dependency Guard
try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    from PIL import Image, ImageDraw, ImageTk
    import vgamepad as vg
    from pynput.mouse import Button, Controller as MouseController
    from pynput.keyboard import Controller as KeyboardController, Key
except ImportError as e:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Missing Dependencies", 
        f"Please install required packages:\npip install vgamepad pynput pillow\n\nDetailed Error: {e}"
    )
    sys.exit(1)

# Pillow Compatibility Fallback
try:
    RESAMPLE_NEAREST = Image.Resampling.NEAREST
except AttributeError:
    RESAMPLE_NEAREST = Image.NEAREST

# ============================================================
# CONSTANTS
# ============================================================

DS_WIDTH = 256
DS_HEIGHT = 192
MAX_STICK = 32767
MOUSE_MOVE_SCALE = 1.5
RIGHT_STICK_SCALE = 900
DEFAULT_PORT = 8888

# ============================================================
# CONFIG FILE
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_FILE = os.path.join(SCRIPT_DIR, "ds_config.dscon")
config_path = DEFAULT_CONFIG_FILE

def choose_config_file(parent=None):
    """Open a dialog to pick a .dscon file. Returns True if a file was selected."""
    global config_path
    from tkinter import filedialog
    path = filedialog.askopenfilename(
        parent=parent,
        title="Select DS Controller Config",
        filetypes=[("DS Controller Config", "*.dscon")],
        initialdir=SCRIPT_DIR
    )
    if path:
        config_path = path
        load_config()
        return True
    return False

# ============================================================
# RLE, PALETTE + NETWORKING
# ============================================================

def rgb_to_index(r, g, b):
    return (((r >> 5) & 7) << 5 | ((g >> 5) & 7) << 2 | ((b >> 6) & 3))

def rgb332_palette():
    palette = []
    for r in range(8):
        for g in range(8):
            for b in range(4):
                palette.append((
                    (r << 5) | (r << 2) | (r >> 1),
                    (g << 5) | (g << 2) | (g >> 1),
                    (b << 6) | (b << 4) | (b << 2) | b
                ))
    return palette

RGB332_PALETTE = rgb332_palette()

def encode_image_rle(img):
    data = bytearray()
    prev = None
    count = 0
    img_rgba = img.convert("RGBA")
    
    for r, g, b, a in img_rgba.getdata():
        idx = (r & 0xE0) | ((g & 0xE0) >> 3) | (b >> 6)
        
        if idx == prev:
            count += 1
            if count == 255:
                data.extend((255, prev))
                count = 0
        else:
            if count > 0:
                data.extend((count, prev))
            prev = idx
            count = 1
            
    if count > 0:
        data.extend((count, prev))
        
    data.append(0)
    return bytes(data)

# ============================================================
# GLOBAL STATE & THREADING
# ============================================================

server_running = False
server_socket = None
client_conn = None
sock_lock = threading.Lock()
state_lock = threading.RLock()
input_queue = queue.Queue()

ds_update_timer = None

# Lazy Init Gamepads
xbox_pad = None
ps_pad = None
mouse = MouseController()
keyboard = KeyboardController()

ds_live_state = {"keys": 0, "touch": False}

touch_zones = []
combos = []
single_mappings = {}

use_zones = True
show_zones_on_ds = True
fallback_action = "Mouse Move"
touch_sensitivity = 1.0  

ds_canvas_image = Image.new("RGBA", (DS_WIDTH, DS_HEIGHT), (20, 20, 20, 0))
ds_canvas_draw = ImageDraw.Draw(ds_canvas_image)

last_pynput_actions = set()
last_special_actions = set()

xbox_used = False
ps_used = False

held_keys = 0

def zones_overlap(rect1, ignore=None):
    with state_lock:
        for i, z in enumerate(touch_zones):
            if i == ignore:
                continue
            r = z["rect"]
            if not (rect1[2] <= r[0] or rect1[0] >= r[2] or rect1[3] <= r[1] or rect1[1] >= r[3]):
                return True
    return False

# ============================================================
# DS BUTTONS
# ============================================================

DS_KEYS_LIST = [
    "A", "B", "SELECT", "START", "RIGHT", "LEFT", "UP", "DOWN",
    "R", "L", "X", "Y", "TOUCH_PRESSED", "TOUCH_LEFT",
    "TOUCH_RIGHT", "TOUCH_UP", "TOUCH_DOWN"
]

DS_KEYS = {
    "A": 1 << 0, "B": 1 << 1, "SELECT": 1 << 2, "START": 1 << 3,
    "RIGHT": 1 << 4, "LEFT": 1 << 5, "UP": 1 << 6, "DOWN": 1 << 7,
    "R": 1 << 8, "L": 1 << 9, "X": 1 << 10, "Y": 1 << 11
}

# ============================================================
# ACTION MAP
# ============================================================

ACTION_MAP = {
    "None": "None",
    "Xbox A": vg.XUSB_BUTTON.XUSB_GAMEPAD_A, "Xbox B": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
    "Xbox X": vg.XUSB_BUTTON.XUSB_GAMEPAD_X, "Xbox Y": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
    "Xbox Up": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP, "Xbox Down": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
    "Xbox Left": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT, "Xbox Right": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
    "Xbox Start": vg.XUSB_BUTTON.XUSB_GAMEPAD_START, "Xbox Select": vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
    "Xbox LB": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER, "Xbox RB": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
    "Xbox LS": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB, "Xbox RS": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
    "Xbox LT": "Xbox LT", "Xbox RT": "Xbox RT", 
    "PS Cross": vg.DS4_BUTTONS.DS4_BUTTON_CROSS, "PS Circle": vg.DS4_BUTTONS.DS4_BUTTON_CIRCLE,
    "PS Square": vg.DS4_BUTTONS.DS4_BUTTON_SQUARE, "PS Triangle": vg.DS4_BUTTONS.DS4_BUTTON_TRIANGLE,
    "PS Options": vg.DS4_BUTTONS.DS4_BUTTON_OPTIONS, "PS Share": vg.DS4_BUTTONS.DS4_BUTTON_SHARE,
    "PS L1": vg.DS4_BUTTONS.DS4_BUTTON_SHOULDER_LEFT, "PS R1": vg.DS4_BUTTONS.DS4_BUTTON_SHOULDER_RIGHT,
    "PS L3": vg.DS4_BUTTONS.DS4_BUTTON_THUMB_LEFT, "PS R3": vg.DS4_BUTTONS.DS4_BUTTON_THUMB_RIGHT,
    "PS L2": "PS L2", "PS R2": "PS R2",
    "PS Up": vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NORTH, 
    "PS Down": vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_SOUTH,
    "PS Left": vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_WEST, 
    "PS Right": vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_EAST, 
    "Mouse Left": Button.left, "Mouse Right": Button.right, "Mouse Middle": Button.middle,
    "Scroll Up": "Scroll Up", "Scroll Down": "Scroll Down", 
    "Key Space": Key.space, "Key Enter": Key.enter, "Key Esc": Key.esc,
    "Key Shift": Key.shift, "Key Ctrl": Key.ctrl, "Key Alt": Key.alt, "Key Tab": Key.tab, "Key Win": Key.cmd,
    "Key Up": Key.up, "Key Down": Key.down, "Key Left": Key.left, "Key Right": Key.right,
    "Key Backspace": Key.backspace,
}

STICK_ACTIONS = [
    "Xbox Left Stick Up", "Xbox Left Stick Down", "Xbox Left Stick Left", "Xbox Left Stick Right",
    "Xbox Right Stick Up", "Xbox Right Stick Down", "Xbox Right Stick Left", "Xbox Right Stick Right",
    "PS Left Stick Up", "PS Left Stick Down", "PS Left Stick Left", "PS Left Stick Right",
    "PS Right Stick Up", "PS Right Stick Down", "PS Right Stick Left", "PS Right Stick Right",
]

for a in STICK_ACTIONS:
    ACTION_MAP[a] = a

for c in "abcdefghijklmnopqrstuvwxyz1234567890-=[]\\;',./`":
    ACTION_MAP["Key " + c.upper()] = c
for i in range(1, 13):
    ACTION_MAP[f"Key F{i}"] = getattr(Key, f"f{i}")

# ============================================================
# SAVE / LOAD CONFIGURATION
# ============================================================

def save_config():
    global touch_zones, combos, single_mappings, use_zones, show_zones_on_ds, fallback_action, touch_sensitivity
    data = {
        "touch_zones": touch_zones,
        "combos": combos,
        "single_mappings": single_mappings,
        "use_zones": use_zones,
        "show_zones_on_ds": show_zones_on_ds,
        "fallback_action": fallback_action,
        "touch_sensitivity": touch_sensitivity,
    }
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Failed to save config: {e}")

def save_config_dialog(parent=None):
    """Open a dialog to choose where to save the .dscon file."""
    global config_path
    from tkinter import filedialog
    
    path = filedialog.asksaveasfilename(
        parent=parent,
        title="Save DS Controller Config As",
        defaultextension=".dscon",
        filetypes=[("DS Controller Config", "*.dscon")],
        initialdir=SCRIPT_DIR,
        initialfile="ds_config.dscon"
    )
    
    if path:
        # Enforce the .dscon extension if the user didn't type it
        if not path.lower().endswith(".dscon"):
            path += ".dscon"
            
        config_path = path
        save_config()  # Write the data to the newly selected path
        return True
    return False

def load_config():
    global touch_zones, combos, single_mappings, use_zones, show_zones_on_ds, fallback_action, touch_sensitivity
    if not os.path.exists(config_path):
        return
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with state_lock:
            touch_zones = data.get("touch_zones", [])
            combos = data.get("combos", [])
            single_mappings = data.get("single_mappings", {})
            use_zones = data.get("use_zones", True)
            show_zones_on_ds = data.get("show_zones_on_ds", True)
            fallback_action = data.get("fallback_action", "Mouse Move")
            touch_sensitivity = data.get("touch_sensitivity", 1.0)
        print(f"Config loaded from {config_path}")
        
        # ---> ADD THIS LINE HERE <---
        send_current_overlay_to_ds()
        
    except Exception as e:
        print(f"Failed to load config: {e}")

# ============================================================
# ACTION ENGINE
# ============================================================

def apply_action(action_id, current_pynput, special_actions, ps_dpad_active, stick_state):
    global xbox_used, ps_used
    if action_id not in ACTION_MAP or action_id == "None":
        return
    act = ACTION_MAP[action_id]

    if action_id.startswith("Xbox"):
        xbox_used = True
        if action_id == "Xbox LT":
            xbox_pad.left_trigger_float(1.0)
        elif action_id == "Xbox RT":
            xbox_pad.right_trigger_float(1.0)
        elif "Stick" in action_id:
            if action_id == "Xbox Left Stick Up": stick_state['x_lsy'] += MAX_STICK
            elif action_id == "Xbox Left Stick Down": stick_state['x_lsy'] -= MAX_STICK
            elif action_id == "Xbox Left Stick Left": stick_state['x_lsx'] -= MAX_STICK
            elif action_id == "Xbox Left Stick Right": stick_state['x_lsx'] += MAX_STICK
            elif action_id == "Xbox Right Stick Up": stick_state['x_rsy'] += MAX_STICK
            elif action_id == "Xbox Right Stick Down": stick_state['x_rsy'] -= MAX_STICK
            elif action_id == "Xbox Right Stick Left": stick_state['x_rsx'] -= MAX_STICK
            elif action_id == "Xbox Right Stick Right": stick_state['x_rsx'] += MAX_STICK
        else:
            xbox_pad.press_button(button=act)

    elif action_id.startswith("PS"):
        ps_used = True
        if action_id == "PS L2":
            ps_pad.left_trigger_float(1.0)
        elif action_id == "PS R2":
            ps_pad.right_trigger_float(1.0)
        elif action_id in ("PS Up", "PS Down", "PS Left", "PS Right"):
            ps_dpad_active.add(act)
        elif "Stick" in action_id:
            if action_id == "PS Left Stick Up": stick_state['p_lsy'] -= 1.0
            elif action_id == "PS Left Stick Down": stick_state['p_lsy'] += 1.0
            elif action_id == "PS Left Stick Left": stick_state['p_lsx'] -= 1.0
            elif action_id == "PS Left Stick Right": stick_state['p_lsx'] += 1.0
            elif action_id == "PS Right Stick Up": stick_state['p_rsy'] -= 1.0
            elif action_id == "PS Right Stick Down": stick_state['p_rsy'] += 1.0
            elif action_id == "PS Right Stick Left": stick_state['p_rsx'] -= 1.0
            elif action_id == "PS Right Stick Right": stick_state['p_rsx'] += 1.0
        else:
            ps_pad.press_button(button=act)

    elif action_id.startswith("Mouse") or action_id.startswith("Key"):
        current_pynput.add(act)

    elif act in ("Scroll Up", "Scroll Down"):
        special_actions.add(act)

# ============================================================
# INPUT PROCESSING
# ============================================================

def process_inputs(keys_held, touch_active, tx, ty, last_tx, last_ty):
    global last_pynput_actions, last_special_actions, ds_live_state
    global xbox_used, ps_used, touch_sensitivity

    ds_live_state["keys"] = keys_held
    ds_live_state["touch"] = touch_active

    xbox_was_used = xbox_used
    ps_was_used = ps_used

    if xbox_pad:
        xbox_pad.reset()
    if ps_pad:
        ps_pad.reset()
    xbox_used = False
    ps_used = False

    current_pynput, current_special = set(), set()
    ps_dpad_active = set()
    
    stick_state = {
        'x_lsx': 0, 'x_lsy': 0, 'x_rsx': 0, 'x_rsy': 0,
        'p_lsx': 0.0, 'p_lsy': 0.0, 'p_rsx': 0.0, 'p_rsy': 0.0
    }

    available_keys = {name for name, mask in DS_KEYS.items() if keys_held & mask}
    zone_found = False

    if touch_active:
        available_keys.add("TOUCH_PRESSED")
        available_keys.add("TOUCH_LEFT" if tx < DS_WIDTH // 2 else "TOUCH_RIGHT")
        available_keys.add("TOUCH_UP" if ty < DS_HEIGHT // 2 else "TOUCH_DOWN")

        with state_lock:
            if use_zones:
                for zone in touch_zones:
                    x1, y1, x2, y2 = zone["rect"]
                    if min(x1, x2) <= tx <= max(x1, x2) and min(y1, y2) <= ty <= max(y1, y2):
                        zone_found = True
                        available_keys.add(f"ZONE:{zone['name']}")
                        apply_action(zone["action"], current_pynput, current_special, ps_dpad_active, stick_state)

        if not zone_found:
            if last_tx != -1:
                dx = tx - last_tx
                dy = ty - last_ty
                
                if fallback_action == "Mouse Move":
                    mouse.move(dx * MOUSE_MOVE_SCALE * touch_sensitivity, dy * MOUSE_MOVE_SCALE * touch_sensitivity)
                elif fallback_action == "Right Stick" and xbox_pad and ps_pad:
                    strength = RIGHT_STICK_SCALE * touch_sensitivity
                    stick_state['x_rsx'] += dx * strength
                    stick_state['x_rsy'] -= dy * strength                     # Xbox Y is +UP/-DOWN
                    stick_state['p_rsx'] += (dx * strength) / float(MAX_STICK)
                    stick_state['p_rsy'] += (dy * strength) / float(MAX_STICK) # PS4 Y is -UP/+DOWN (Fixed polarity)
                    xbox_used = True
                    ps_used = True

    # ----------------------- COMBO LOGIC -----------------------
    with state_lock:
        current_combos = list(combos)

    used_keys = set()
    for combo in sorted(current_combos, key=lambda c: len(c.get("triggers", [])), reverse=True):
        triggers = set(str(t).upper().strip() for t in combo.get("triggers", []))
        if triggers and triggers.issubset(available_keys) and not triggers.intersection(used_keys):
            used_keys.update(triggers)
            for output in combo.get("outputs", []):
                apply_action(output, current_pynput, current_special, ps_dpad_active, stick_state)

    available_keys -= used_keys
    
    for key in available_keys:
        if key in single_mappings:
            apply_action(single_mappings[key], current_pynput, current_special, ps_dpad_active, stick_state)
    # ----------------------------------------------------------------------

    if xbox_pad and (xbox_used or xbox_was_used):
        xbox_pad.left_joystick(
            int(max(-MAX_STICK, min(MAX_STICK, stick_state['x_lsx']))),
            int(max(-MAX_STICK, min(MAX_STICK, stick_state['x_lsy'])))
        )
        xbox_pad.right_joystick(
            int(max(-MAX_STICK, min(MAX_STICK, stick_state['x_rsx']))),
            int(max(-MAX_STICK, min(MAX_STICK, stick_state['x_rsy'])))
        )
        try:
            xbox_pad.update()
        except Exception as e:
            print(f"Xbox update failed: {e}")

    if ps_pad and (ps_used or ps_was_used):
        final_dpad = vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NONE
        if vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NORTH in ps_dpad_active:
            if vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_EAST in ps_dpad_active:
                final_dpad = vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NORTHEAST
            elif vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_WEST in ps_dpad_active:
                final_dpad = vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NORTHWEST
            else:
                final_dpad = vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NORTH
        elif vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_SOUTH in ps_dpad_active:
            if vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_EAST in ps_dpad_active:
                final_dpad = vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_SOUTHEAST
            elif vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_WEST in ps_dpad_active:
                final_dpad = vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_SOUTHWEST
            else:
                final_dpad = vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_SOUTH
        elif vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_EAST in ps_dpad_active:
            final_dpad = vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_EAST
        elif vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_WEST in ps_dpad_active:
            final_dpad = vg.DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_WEST

        ps_pad.directional_pad(final_dpad)
        ps_pad.left_joystick_float(
            max(-1.0, min(1.0, stick_state['p_lsx'])),
            max(-1.0, min(1.0, stick_state['p_lsy']))
        )
        ps_pad.right_joystick_float(
            max(-1.0, min(1.0, stick_state['p_rsx'])),
            max(-1.0, min(1.0, stick_state['p_rsy']))
        )
        try:
            ps_pad.update()
        except Exception as e:
            print(f"PS4 update failed: {e}")

    for action in (current_pynput - last_pynput_actions):
        if isinstance(action, Button):
            mouse.press(action)
        else:
            keyboard.press(action)
    for action in (last_pynput_actions - current_pynput):
        if isinstance(action, Button):
            mouse.release(action)
        else:
            keyboard.release(action)

    if "Scroll Up" in current_special and "Scroll Up" not in last_special_actions:
        mouse.scroll(0, 1)
    if "Scroll Down" in current_special and "Scroll Down" not in last_special_actions:
        mouse.scroll(0, -1)

    last_pynput_actions = current_pynput
    last_special_actions = current_special

# ============================================================
# NETWORKING: HYBRID PROTOCOL
# ============================================================

def send_current_overlay_to_ds():
    """Builds and sends the current touch zone layout to the connected DS."""
    try:
        # Explicitly declare all the variables we need to read/write
        global ds_canvas_image, show_zones_on_ds, use_zones, touch_zones, client_conn
        
        # 1. Check if we even have a connection to avoid useless processing
        with sock_lock:
            if client_conn is None:
                return  # Silently skip if no DS is connected
            conn = client_conn

        # 2. Create the base background
        base_img = Image.new("RGBA", (DS_WIDTH, DS_HEIGHT), (20, 20, 20, 255))
        draw = ImageDraw.Draw(base_img)
        
        with state_lock:
            if show_zones_on_ds and use_zones:
                for zone in touch_zones:
                    x1, y1, x2, y2 = zone["rect"]
                    fill_color = zone.get("color", "#883333")
                    outline_color = zone.get("outline", "#ffffff")
                    if fill_color == "":
                        fill_color = (0, 0, 0, 0)
                    draw.rectangle(
                        [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)],
                        fill=fill_color, outline=outline_color, width=2
                    )
                    draw.text(
                        (min(x1, x2) + 4, min(y1, y2) + 4),
                        f"{zone.get('name', 'Zone')}\n({zone['action']})",
                        fill="#ffffff"
                    )
            # Overlay any custom paint drawn by the user
            base_img.alpha_composite(ds_canvas_image)

        # 3. Encode and build the packet
        rle = encode_image_rle(base_img)
        header = struct.pack(">BHHHH", 0x01, 0, 0, DS_WIDTH, DS_HEIGHT)
        size_field = struct.pack(">I", len(rle))
        packet = header + size_field + rle + b"\xFE"

        # 4. Send it over the socket
        conn.settimeout(5.0)
        conn.sendall(packet)
        conn.settimeout(1.0)
        print("Overlay successfully sent to DS.")

    except Exception as e:
        # If anything fails, pop up an error box so we can see the exact crash
        import traceback
        traceback.print_exc()
        try:
            from tkinter import messagebox
            messagebox.showerror("Send Error", f"Failed to send to DS:\n\n{e}")
        except:
            pass

def recv_exact(sock, size, max_timeouts=5):
    data = b""
    timeouts = 0
    while len(data) < size and server_running:
        try:
            chunk = sock.recv(size - len(data))
            if not chunk:
                return None
            data += chunk
            timeouts = 0
        except socket.timeout:
            timeouts += 1
            if timeouts >= max_timeouts:
                return None
        except Exception:
            return None
    return data

def handle_client(conn, addr):
    global client_conn, held_keys
    with sock_lock:
        client_conn = conn
    print(f"Connected: {addr}")

    held_keys = 0
    touch_active = False
    last_tx, last_ty = -1, -1

    conn.settimeout(1.0)

    def push_state(k, t, x, y, px, py, limit):
        while input_queue.qsize() > limit:
            try: input_queue.get_nowait()
            except queue.Empty: break
        input_queue.put((k, t, x, y, px, py))

    try:
        while server_running:
            try:
                data = conn.recv(1)
            except socket.timeout:
                continue

            if not data:
                break

            event = data[0]

            if event < 12:
                held_keys |= (1 << event)
                push_state(held_keys, touch_active, last_tx, last_ty, last_tx, last_ty, 10)

            elif 128 <= event < 140:
                held_keys &= ~(1 << (event - 128))
                push_state(held_keys, touch_active, last_tx, last_ty, last_tx, last_ty, 10)

            elif event == 0xFC:
                count_data = recv_exact(conn, 1)
                if not count_data:
                    break

                point_count = count_data[0]
                if point_count > 10:
                    break

                if point_count > 0:
                    coord_data = recv_exact(conn, point_count * 4)
                    if not coord_data:
                        break
                    end_data = recv_exact(conn, 1)
                    if not end_data or end_data[0] != 0xFD:
                        break

                    for i in range(point_count):
                        tx = (coord_data[i*4] << 8) | coord_data[i*4+1]
                        ty = (coord_data[i*4+2] << 8) | coord_data[i*4+3]

                        tx = max(0, min(DS_WIDTH - 1, tx))
                        ty = max(0, min(DS_HEIGHT - 1, ty))

                        prev_tx = last_tx if touch_active else tx
                        prev_ty = last_ty if touch_active else ty
                        touch_active = True
                        last_tx, last_ty = tx, ty

                        push_state(held_keys, True, tx, ty, prev_tx, prev_ty, 15)
                else:
                    end_data = recv_exact(conn, 1)
                    if not end_data or end_data[0] != 0xFD:
                        break
                    touch_active = False
                    last_tx, last_ty = -1, -1
                    push_state(held_keys, False, 0, 0, -1, -1, 15)
            else:
                break

    except Exception as e:
        print(f"Client error: {e}")

    finally:
        with sock_lock:
            still_owner = (client_conn is conn)
            if still_owner:
                client_conn = None
        if still_owner:
            held_keys = 0
            try:
                input_queue.put_nowait((0, False, 0, 0, -1, -1))
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass
        print(f"Disconnected: {addr}")

def accept_loop(port):
    global server_socket, server_running

    try:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("0.0.0.0", port))
        server_socket.listen(1)
        server_socket.settimeout(1.0)
    except OSError as e:
        print(f"Server startup failed: {e}")
        server_running = False
        return

    while server_running:
        try:
            conn, addr = server_socket.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

            with sock_lock:
                old = client_conn
            if old:
                try: old.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                try: old.close()
                except OSError: pass

            threading.Thread(
                target=handle_client,
                args=(conn, addr),
                daemon=True
            ).start()
        except socket.timeout:
            continue
        except OSError:
            break
        except Exception as e:
            print(f"Accept error: {e}")
            break

    try:
        server_socket.close()
    except Exception:
        pass
    server_socket = None
    server_running = False

# ============================================================
# TEARDOWN HANDLER
# ============================================================

def cleanup_and_exit(root_window):
    global server_running, server_socket
    save_config()
    server_running = False
    
    with sock_lock:
        if client_conn:
            try: client_conn.close()
            except Exception: pass

    if server_socket:
        try: server_socket.close()
        except Exception: pass
            
    try:
        if xbox_pad:
            xbox_pad.reset()
            try: xbox_pad.update()
            except Exception: pass
        if ps_pad:
            ps_pad.reset()
            try: ps_pad.update()
            except Exception: pass
        for k in last_pynput_actions:
            if isinstance(k, Button): mouse.release(k)
            else: keyboard.release(k)
    except Exception:
        pass
    root_window.destroy()

# ============================================================
# UI COMPONENTS
# ============================================================

def palette_picker(parent, callback):
    win = tk.Toplevel(parent)
    win.title("RGB332 Palette")
    win.grab_set()
    win.focus_force()

    for i, color in enumerate(RGB332_PALETTE):
        hex_color = "#%02x%02x%02x" % color
        tk.Button(
            win,
            bg=hex_color,
            width=2,
            height=1,
            command=lambda c=hex_color: (
                callback(c),
                win.destroy()
            )
        ).grid(row=i//16, column=i%16, padx=0, pady=0)

class ActionSelectorDialog(tk.Toplevel):
    def __init__(self, parent, multi_select=False, callback=None):
        super().__init__(parent)
        self.transient(parent)
        self.title("Select PC Output Action")
        self.geometry("550x600")
        
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_visibility()
        self.grab_set()
        self.focus_force()

        self.callback = callback
        self.multi_select = multi_select
        self.selected = []

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        xbox = tk.Frame(self.notebook)
        self.notebook.add(xbox, text="Xbox")
        xb = tk.Frame(xbox)
        xb.pack(expand=True)
        xbox_buttons = [
            ("A", "Xbox A"), ("B", "Xbox B"), ("X", "Xbox X"), ("Y", "Xbox Y"),
            ("UP", "Xbox Up"), ("DOWN", "Xbox Down"), ("LEFT", "Xbox Left"), ("RIGHT", "Xbox Right"),
            ("LB", "Xbox LB"), ("RB", "Xbox RB"), ("LT", "Xbox LT"), ("RT", "Xbox RT"),
            ("LS", "Xbox LS"), ("RS", "Xbox RS"),
            ("START", "Xbox Start"), ("SELECT", "Xbox Select"),
            ("LS UP", "Xbox Left Stick Up"), ("LS DOWN", "Xbox Left Stick Down"), 
            ("LS LEFT", "Xbox Left Stick Left"), ("LS RIGHT", "Xbox Left Stick Right"),
            ("RS UP", "Xbox Right Stick Up"), ("RS DOWN", "Xbox Right Stick Down"), 
            ("RS LEFT", "Xbox Right Stick Left"), ("RS RIGHT", "Xbox Right Stick Right")
        ]
        self.create_button_grid(xb, xbox_buttons, cols=4)

        ps = tk.Frame(self.notebook)
        self.notebook.add(ps, text="PlayStation")
        ps_buttons = [
            ("Cross", "PS Cross"), ("Circle", "PS Circle"), ("Square", "PS Square"), ("Triangle", "PS Triangle"),
            ("L1", "PS L1"), ("R1", "PS R1"), ("L2", "PS L2"), ("R2", "PS R2"),
            ("L3", "PS L3"), ("R3", "PS R3"),
            ("OPTIONS", "PS Options"), ("SHARE", "PS Share"),
            ("UP", "PS Up"), ("DOWN", "PS Down"), ("LEFT", "PS Left"), ("RIGHT", "PS Right"),
            ("LS UP", "PS Left Stick Up"), ("LS DOWN", "PS Left Stick Down"), 
            ("LS LEFT", "PS Left Stick Left"), ("LS RIGHT", "PS Left Stick Right"),
            ("RS UP", "PS Right Stick Up"), ("RS DOWN", "PS Right Stick Down"), 
            ("RS LEFT", "PS Right Stick Left"), ("RS RIGHT", "PS Right Stick Right")
        ]
        self.create_button_grid(ps, ps_buttons, cols=4)

        kb = tk.Frame(self.notebook)
        self.notebook.add(kb, text="Keyboard / Mouse")
        keys = list(ACTION_MAP.keys())
        self.key_box = tk.Listbox(kb)
        self.key_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        for key in keys:
            if key.startswith("Key") or key.startswith("Mouse") or key.startswith("Scroll"):
                self.key_box.insert(tk.END, key)

        tk.Button(kb, text="Select", command=self.on_listbox_select).pack(pady=5)

        # Bottom Frame for None/Clear and Confirm options
        bottom_frame = tk.Frame(self)
        bottom_frame.pack(fill=tk.X, padx=10, pady=5)

        tk.Button(bottom_frame, text="None / Clear (Unmap)", bg="#ffcccc", command=lambda: self.select("None")).pack(side=tk.LEFT, padx=5, pady=5)

        if multi_select:
            self.label = tk.Label(bottom_frame, text="Selected: None")
            self.label.pack(side=tk.LEFT, padx=5)
            tk.Button(bottom_frame, text="Confirm", bg="#ccffcc", command=self.confirm).pack(side=tk.RIGHT, padx=5, pady=5)

    def on_listbox_select(self):
        sel = self.key_box.curselection()
        if sel:
            self.select(self.key_box.get(sel[0]))

    def create_button_grid(self, frame, buttons, cols=3):
        for i, (name, action) in enumerate(buttons):
            tk.Button(frame, text=name, width=12, command=lambda a=action: self.select(a)).grid(row=i//cols, column=i%cols, padx=5, pady=5)

    def select(self, action):
        if not self.multi_select:
            if self.callback:
                self.callback([action])
            self.destroy()
        else:
            if action == "None":
                self.selected = ["None"]
            else:
                if "None" in self.selected:
                    self.selected.remove("None")
                if action not in self.selected:
                    self.selected.append(action)
            self.label.config(text="Selected: " + ", ".join(self.selected))

    def confirm(self):
        if self.callback:
            self.callback(self.selected)
        self.destroy()

    def destroy(self):
        try:
            self.grab_release()
        except Exception:
            pass
        super().destroy()

class ComboBuilder(tk.Toplevel):
    def __init__(self, parent, on_save_callback, existing_data=None, edit_index=None):
        super().__init__(parent)
        self.transient(parent)
        self.title("Edit Combo" if existing_data else "Build New Combo")
        self.geometry("500x380")
        
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_visibility()
        self.grab_set()
        self.focus_force()

        self.on_save = on_save_callback
        self.edit_index = edit_index
        self.triggers = list(existing_data["triggers"]) if existing_data else []
        self.outputs = list(existing_data["outputs"]) if existing_data else []

        f_l = tk.Frame(self)
        f_l.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        f_r = tk.Frame(self)
        f_r.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        tk.Label(f_l, text="DS Triggers", font=("Arial", 10, "bold")).pack()
        self.lb_triggers = tk.Listbox(f_l, height=10)
        self.lb_triggers.pack(fill=tk.BOTH, expand=True)
        tk.Button(f_l, text="+ Add DS/Zone Trigger", command=self.add_trigger).pack(pady=5)
        tk.Button(f_l, text="Remove Trigger", bg="#ffcccc", command=self.remove_trigger).pack(pady=2)

        tk.Label(f_r, text="PC Outputs", font=("Arial", 10, "bold")).pack()
        self.lb_outputs = tk.Listbox(f_r, height=10)
        self.lb_outputs.pack(fill=tk.BOTH, expand=True)
        tk.Button(f_r, text="+ Add PC Action", command=self.add_output).pack(pady=5)
        tk.Button(f_r, text="Remove Output", bg="#ffcccc", command=self.remove_output).pack(pady=2)

        tk.Button(self, text="Save Combo", bg="#ccffcc", font=("Arial", 10, "bold"), command=self.save).pack(side=tk.BOTTOM, pady=10)
        self.refresh()

    def refresh(self):
        self.lb_triggers.delete(0, tk.END)
        for t in self.triggers:
            self.lb_triggers.insert(tk.END, t)
        self.lb_outputs.delete(0, tk.END)
        for o in self.outputs:
            self.lb_outputs.insert(tk.END, o)

    def add_trigger(self):
        tw = tk.Toplevel(self)
        tw.transient(self)
        tw.title("Select Trigger")
        
        tw.protocol("WM_DELETE_WINDOW", tw.destroy)
        tw.wait_visibility()
        tw.grab_set()
        tw.focus_force()
        
        with state_lock:
            zone_names = [f"ZONE:{z['name']}" for z in touch_zones]
            
        cb = ttk.Combobox(tw, values=DS_KEYS_LIST + zone_names, state="readonly")
        cb.pack(padx=20, pady=10)
        cb.current(0)
        
        def confirm():
            val = cb.get()
            if val and val not in self.triggers:
                self.triggers.append(val)
            self.refresh()
            tw.destroy()
            
        tk.Button(tw, text="Add", command=confirm).pack(pady=5)

    def remove_trigger(self):
        sel = self.lb_triggers.curselection()
        if sel:
            del self.triggers[sel[0]]
            self.refresh()

    def add_output(self):
        ActionSelectorDialog(self, multi_select=True, callback=lambda a: (self.outputs.extend([x for x in a if x != "None"]), self.refresh()))

    def remove_output(self):
        sel = self.lb_outputs.curselection()
        if sel:
            del self.outputs[sel[0]]
            self.refresh()

    def save(self):
        if not self.triggers or not self.outputs:
            messagebox.showerror("Error", "Need at least 1 trigger and 1 output.")
            return
        data = {"triggers": self.triggers, "outputs": list(set(self.outputs))}
        self.on_save(data, self.edit_index)
        self.destroy()

    def destroy(self):
        try:
            self.grab_release()
        except Exception:
            pass
        super().destroy()

class ComboEditor(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.transient(parent)
        self.title("Combo Manager")
        self.geometry("480x380")
        
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_visibility()
        self.grab_set()
        self.focus_force()
        
        self.listbox = tk.Listbox(self)
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.refresh()

        btn_frame = tk.Frame(self)
        btn_frame.pack(pady=5)
        tk.Button(btn_frame, text="Add Combo", bg="#ccffcc", command=lambda: ComboBuilder(self, self.on_save_combo)).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Edit Selected", bg="#ffffcc", command=self.edit_combo).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Delete Selected", bg="#ffcccc", command=self.del_combo).pack(side=tk.LEFT, padx=5)

    def on_save_combo(self, data, edit_index=None):
        with state_lock:
            if edit_index is not None and 0 <= edit_index < len(combos):
                combos[edit_index] = data
            else:
                combos.append(data)
        save_config()
        self.refresh()

    def edit_combo(self):
        sel = self.listbox.curselection()
        if sel:
            idx = sel[0]
            with state_lock:
                combo_data = combos[idx]
            ComboBuilder(self, self.on_save_combo, existing_data=combo_data, edit_index=idx)

    def del_combo(self):
        sel = self.listbox.curselection()
        if sel:
            with state_lock:
                del combos[sel[0]]
            save_config()
            self.refresh()

    def refresh(self):
        self.listbox.delete(0, tk.END)
        with state_lock:
            for c in combos:
                self.listbox.insert(tk.END, f"[{'+'.join(c['triggers'])}] ➔ [{', '.join(c['outputs'])}]")

    def destroy(self):
        try:
            self.grab_release()
        except Exception:
            pass
        super().destroy()


class VisualTouchEditor(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.transient(parent)
        self.title("Nintendo DS Touch Screen Editor")
        self.geometry("820x860")
  
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_visibility()
        self.grab_set()
        self.focus_force()

        self.scale = 3

        self.canvas = tk.Canvas(
            self, width=DS_WIDTH * self.scale, height=DS_HEIGHT * self.scale, bg="#141414", cursor="cross"
        )
        self.canvas.pack(pady=10)

        settings = tk.Frame(self)
        settings.pack(fill=tk.X, padx=10, pady=5)

        self.zone_var = tk.BooleanVar(value=use_zones)
        self.show_var = tk.BooleanVar(value=show_zones_on_ds)

        tk.Checkbutton(settings, text="Enable Touch Zones", variable=self.zone_var, command=self.update_settings).pack(side=tk.LEFT, padx=5)
        tk.Checkbutton(settings, text="Show Zones On DS", variable=self.show_var, command=self.update_settings).pack(side=tk.LEFT, padx=5)

        self.fallback = tk.StringVar(value=fallback_action)
        tk.Label(settings, text="Outside zone func:").pack(side=tk.LEFT, padx=5)
        ttk.Combobox(settings, textvariable=self.fallback, values=["Mouse Move", "Right Stick", "None"], state="readonly", width=12).pack(side=tk.LEFT, padx=5)
        self.fallback.trace("w", lambda *args: self.update_settings())

        tk.Button(settings, text="Apply", bg="#ffffcc", command=send_current_overlay_to_ds).pack(side=tk.RIGHT)

        # --- SENSITIVITY (INVERSION REMOVED) ---
        sens_frame = tk.Frame(self)
        sens_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.sens_var = tk.DoubleVar(value=touch_sensitivity)
        tk.Scale(
            sens_frame, 
            variable=self.sens_var, 
            from_=0.1, to=5.0, resolution=0.1, 
            orient=tk.HORIZONTAL, 
            label="Touchscreen Sensitivity (Mouse / Right Stick)", 
            command=self.update_sens
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        tools_top = tk.Frame(self)
        tools_top.pack(pady=2)

        self.edit_mode = tk.StringVar(value="zone")
        tk.Radiobutton(tools_top, text="Draw Zones", variable=self.edit_mode, value="zone").pack(side=tk.LEFT, padx=5)
        tk.Radiobutton(tools_top, text="Paint", variable=self.edit_mode, value="paint").pack(side=tk.LEFT, padx=5)
        tk.Radiobutton(tools_top, text="Erase", variable=self.edit_mode, value="erase").pack(side=tk.LEFT, padx=5)
        
        tk.Label(tools_top, text="|").pack(side=tk.LEFT, padx=10)
        
        self.brush_color = "#FFFFFF"
        tk.Button(tools_top, text="[Brush Color]", bg="#e0e0e0", command=self.choose_color).pack(side=tk.LEFT, padx=5)
        
        tk.Label(tools_top, text="Size:").pack(side=tk.LEFT)
        self.brush_size = tk.IntVar(value=4)
        tk.Scale(tools_top, from_=1, to=20, orient=tk.HORIZONTAL, variable=self.brush_size, length=80, showvalue=False).pack(side=tk.LEFT)
        
        tk.Button(tools_top, text="Clear Paint", command=self.clear_paint).pack(side=tk.LEFT, padx=15)
        
        tools_bottom = tk.Frame(self)
        tools_bottom.pack(pady=5)
        
        tk.Button(tools_bottom, text="Reassign Action", command=self.reassign_action).pack(side=tk.LEFT, padx=5)
        tk.Button(tools_bottom, text="Recolor Fill", command=self.recolor_zone).pack(side=tk.LEFT, padx=5)
        tk.Button(tools_bottom, text="Transparent Fill", command=self.transparent_zone).pack(side=tk.LEFT, padx=5)
        tk.Button(tools_bottom, text="Recolor Outline", command=self.recolor_outline).pack(side=tk.LEFT, padx=5)
        
        tk.Label(tools_bottom, text="|").pack(side=tk.LEFT, padx=10)
        
        tk.Button(tools_bottom, text="Delete Zone", command=self.delete_zone).pack(side=tk.LEFT, padx=5)
        tk.Button(tools_bottom, text="Clear All Zones", command=self.clear_zones).pack(side=tk.LEFT, padx=5)

        self.selected = None
        self.mode = None
        self.start = None
        self.temp = None
        self.last_paint_pt = None
        self.orig_rect = None
        self.resize_edges = (False, False, False, False)
        
        self.tk_bg_img = None
        self.paint_update_pending = False
        
        self.canvas.create_image(0, 0, anchor="nw", tags="paint_layer")

        self.canvas.bind("<ButtonPress-1>", self.left_down)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.left_up)

        self.update_paint_layer()
        self.redraw()

    def update_paint_layer(self):
        with state_lock:
            img = ds_canvas_image.resize(
                (DS_WIDTH * self.scale, DS_HEIGHT * self.scale),
                RESAMPLE_NEAREST
            )
        self.tk_bg_img = ImageTk.PhotoImage(img)
        self.canvas.itemconfig("paint_layer", image=self.tk_bg_img)
        self.canvas.tag_lower("paint_layer")

    def request_paint_update(self):
        if not self.paint_update_pending:
            self.paint_update_pending = True
            self.after_idle(self.finish_paint_update)

    def finish_paint_update(self):
        self.paint_update_pending = False
        self.update_paint_layer()

    def update_settings(self):
        global use_zones, show_zones_on_ds, fallback_action
        use_zones = self.zone_var.get()
        show_zones_on_ds = self.show_var.get()
        fallback_action = self.fallback.get()
        save_config()
        self.redraw()

    def update_sens(self, val):
        global touch_sensitivity
        touch_sensitivity = float(val)
        save_config()

    def choose_color(self):
        palette_picker(self, lambda c: setattr(self, "brush_color", c))

    def clear_paint(self):
        global ds_canvas_image, ds_canvas_draw
        with state_lock:
            ds_canvas_image = Image.new("RGBA", (DS_WIDTH, DS_HEIGHT), (20, 20, 20, 0))
            ds_canvas_draw = ImageDraw.Draw(ds_canvas_image)
        self.update_paint_layer()
        
    def clear_zones(self):
        with state_lock:
            touch_zones.clear()
        self.selected = None
        save_config()
        self.redraw()

    def delete_zone(self):
        if self.selected is not None and self.selected < len(touch_zones):
            with state_lock:
                del touch_zones[self.selected]
            self.selected = None
            save_config()
            self.redraw()

    def reassign_action(self):
        if self.selected is not None and self.selected < len(touch_zones):
            def cb(a):
                if a:
                    with state_lock:
                        touch_zones[self.selected]["action"] = "" if a[0] == "None" else a[0]
                    save_config()
                    self.redraw()
            ActionSelectorDialog(self, callback=cb)

    def recolor_zone(self):
        if self.selected is not None and self.selected < len(touch_zones):
            palette_picker(self, lambda c: (touch_zones[self.selected].update({"color": c}), save_config(), self.redraw()))
            
    def transparent_zone(self):
        if self.selected is not None and self.selected < len(touch_zones):
            touch_zones[self.selected]["color"] = ""
            save_config()
            self.redraw()
        
    def recolor_outline(self):
        if self.selected is not None and self.selected < len(touch_zones):
            palette_picker(self, lambda c: (touch_zones[self.selected].update({"outline": c}), save_config(), self.redraw()))

    def erase_at(self, x, y, b_size):
        global ds_canvas_draw
        with state_lock:
            ds_canvas_draw.ellipse([x - b_size / 2, y - b_size / 2, x + b_size / 2, y + b_size / 2], fill=(0, 0, 0, 0))

    def redraw(self):
        self.canvas.delete("zone_layer")

        with state_lock:
            for i, z in enumerate(touch_zones):
                x1, y1, x2, y2 = z["rect"]
                min_x, max_x = min(x1, x2), max(x1, x2)
                min_y, max_y = min(y1, y2), max(y1, y2)
                
                fill_col = z.get("color", "#883333")
                out_col = z.get("outline", "#ffffff")
                width = 3 if i == self.selected else 2

                self.canvas.create_rectangle(
                    min_x * self.scale, min_y * self.scale,
                    max_x * self.scale, max_y * self.scale,
                    fill=fill_col if fill_col else "",
                    outline=out_col,
                    width=width,
                    tags="zone_layer"
                )

                self.canvas.create_text(
                    min_x * self.scale + 5, min_y * self.scale + 5,
                    anchor="nw",
                    text=f"{z.get('name', 'Zone')}\n({z['action']})",
                    fill="white",
                    tags="zone_layer"
                )
                
                if i == self.selected:
                    h_size = 4
                    for hx in (min_x, (min_x + max_x) / 2, max_x):
                        for hy in (min_y, (min_y + max_y) / 2, max_y):
                            if hx == (min_x + max_x) / 2 and hy == (min_y + max_y) / 2:
                                continue 
                            self.canvas.create_rectangle(
                                hx * self.scale - h_size, hy * self.scale - h_size,
                                hx * self.scale + h_size, hy * self.scale + h_size,
                                fill="white", outline="black",
                                tags="zone_layer"
                            )
        self.canvas.tag_lower("paint_layer")

    def left_down(self, event):
        x = event.x / self.scale
        y = event.y / self.scale

        if self.edit_mode.get() in ("paint", "erase"):
            self.last_paint_pt = (x, y)
            b_size = self.brush_size.get()
            if self.edit_mode.get() == "paint":
                hex_color = self.brush_color.lstrip("#")
                rgba = (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16), 255) if len(hex_color) == 6 else (255, 255, 255, 255)
                with state_lock:
                    ds_canvas_draw.ellipse([x - b_size/2, y - b_size/2, x + b_size/2, y + b_size/2], fill=rgba)
            else:
                self.erase_at(x, y, b_size)
            self.request_paint_update()
            return

        with state_lock:
            for i in range(len(touch_zones)-1, -1, -1):
                z = touch_zones[i]
                x1, y1, x2, y2 = z["rect"]
                min_x, max_x = min(x1, x2), max(x1, x2)
                min_y, max_y = min(y1, y2), max(y1, y2)
                
                resize_margin = 8
                if (min_x - resize_margin) <= x <= (max_x + resize_margin) and (min_y - resize_margin) <= y <= (max_y + resize_margin):
                    left = abs(x - min_x) <= resize_margin
                    right = abs(x - max_x) <= resize_margin
                    top = abs(y - min_y) <= resize_margin
                    bottom = abs(y - max_y) <= resize_margin
                    
                    if i == self.selected and (left or right or top or bottom):
                        self.mode = "resize"
                        self.resize_edges = (left, right, top, bottom)
                    else:
                        self.selected = i
                        self.mode = "move"
                        
                    self.start = (x, y)
                    self.orig_rect = list(z["rect"])
                    self.redraw()
                    return

        self.selected = None
        self.mode = "draw"
        self.start = (x, y)
        self.redraw()
        self.temp = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="green", width=2, tags="temp_zone")

    def drag(self, event):
        x = max(0, min(DS_WIDTH, event.x / self.scale))
        y = max(0, min(DS_HEIGHT, event.y / self.scale))

        if self.edit_mode.get() in ("paint", "erase"):
            curr_pt = (x, y)
            if self.last_paint_pt:
                b_size = self.brush_size.get()
                if self.edit_mode.get() == "paint":
                    hex_color = self.brush_color.lstrip("#")
                    rgba = (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16), 255) if len(hex_color) == 6 else (255, 255, 255, 255)
                    lx, ly = self.last_paint_pt
                    cx, cy = curr_pt
                    dist = max(1, int(((cx-lx)**2 + (cy-ly)**2)**0.5))
                    with state_lock:
                        for i in range(dist + 1):
                            ix = lx + (cx-lx) * (i / dist)
                            iy = ly + (cy-ly) * (i / dist) 
                            ds_canvas_draw.ellipse([ix - b_size/2, iy - b_size/2, ix + b_size/2, iy + b_size/2], fill=rgba)
                else:
                    lx, ly = self.last_paint_pt
                    cx, cy = curr_pt
                    dist = max(1, int(((cx-lx)**2 + (cy-ly)**2)**0.5))
                    for i in range(dist + 1):
                        ix = lx + (cx - lx) * (i / dist)
                        iy = ly + (cy - ly) * (i / dist)
                        self.erase_at(ix, iy, b_size)
                self.request_paint_update()
            self.last_paint_pt = curr_pt
            return

        if self.mode == "draw":
            self.canvas.coords(self.temp, self.start[0] * self.scale, self.start[1] * self.scale, x * self.scale, y * self.scale)
        elif self.mode == "move":
            with state_lock:
                if self.selected is None or self.selected >= len(touch_zones):
                    return
                dx = x - self.start[0]
                dy = y - self.start[1]
                r = self.orig_rect
                min_x, max_x = min(r[0], r[2]), max(r[0], r[2])
                min_y, max_y = min(r[1], r[3]), max(r[1], r[3])
                
                if min_x + dx < 0: dx = -min_x
                if max_x + dx > DS_WIDTH: dx = DS_WIDTH - max_x
                if min_y + dy < 0: dy = -min_y
                if max_y + dy > DS_HEIGHT: dy = DS_HEIGHT - max_y

                new_rect = [r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy]
                if not zones_overlap(new_rect, self.selected):
                    touch_zones[self.selected]["rect"] = new_rect
            self.redraw()
        elif self.mode == "resize":
            with state_lock:
                if self.selected is None or self.selected >= len(touch_zones):
                    return
                dx = x - self.start[0]
                dy = y - self.start[1]
                r = self.orig_rect
                min_x, max_x = min(r[0], r[2]), max(r[0], r[2])
                min_y, max_y = min(r[1], r[3]), max(r[1], r[3])
                left, right, top, bottom = self.resize_edges
                
                if left: min_x += dx
                if right: max_x += dx
                if top: min_y += dy
                if bottom: max_y += dy
                
                if min_x > max_x - 5:
                    if left: min_x = max_x - 5
                    if right: max_x = min_x + 5
                if min_y > max_y - 5:
                    if top: min_y = max_y - 5
                    if bottom: max_y = min_y + 5
                    
                min_x = max(0, min(DS_WIDTH, min_x))
                max_x = max(0, min(DS_WIDTH, max_x))
                min_y = max(0, min(DS_HEIGHT, min_y))
                max_y = max(0, min(DS_HEIGHT, max_y))
                
                new_rect = [min_x, min_y, max_x, max_y]
                if not zones_overlap(new_rect, self.selected):
                    touch_zones[self.selected]["rect"] = new_rect
            self.redraw()

    def left_up(self, event):
        if self.edit_mode.get() in ("paint", "erase"):
            self.last_paint_pt = None
            return

        if self.mode == "draw":
            self.canvas.delete("temp_zone")
            x1, y1 = self.start[0], self.start[1]
            x2, y2 = event.x / self.scale, event.y / self.scale

            if abs(x2 - x1) > 10 and abs(y2 - y1) > 10:
                new_zone_rect = [
                    max(0, min(x1, x2)), max(0, min(y1, y2)), 
                    min(DS_WIDTH, max(x1, x2)), min(DS_HEIGHT, max(y1, y2))
                ]
                if zones_overlap(new_zone_rect):
                    self.mode = None
                    return
                
                with state_lock:
                    touch_zones.append({
                        "rect": new_zone_rect,
                        "action": "Mouse Move",
                        "name": f"Zone {len(touch_zones) + 1}",
                        "color": "#993333",
                        "outline": "#ffffff"
                    })
                    self.selected = len(touch_zones) - 1
                    
                save_config()
                self.redraw()

                def choose(action):
                    if action and self.selected is not None:
                        with state_lock:
                            touch_zones[self.selected]["action"] = "" if action[0] == "None" else action[0]
                        save_config()
                        self.redraw()

                ActionSelectorDialog(self, callback=choose)
        self.mode = None

    def destroy(self):
        try:
            self.grab_release()
        except Exception:
            pass
        super().destroy()

class ControlsWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.transient(parent)
        self.title("Nintendo DS Controls & Combos")
        self.geometry("550x650")

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_visibility()
        self.grab_set()
        self.focus_force()

        tk.Label(self, text="Nintendo DS Layout", font=("Arial", 12, "bold")).pack(pady=5)
        tk.Label(self, text="Buttons flash RED when pressed. Click to map.").pack()

        self.canvas = tk.Canvas(self, width=510, height=480, bg="#dddddd")
        self.canvas.pack(pady=5)

        tk.Button(
            self, 
            text="Open Combo Editor", 
            font=("Arial", 10, "bold"), 
            bg="#ccffcc", 
            command=lambda: ComboEditor(self)
        ).pack(pady=10)

        self.draw_ds()
        self.redraw_mappings()
        self.after(50, self.update_feedback)

    def draw_ds(self):
        c = self.canvas
        c.create_rectangle(20, 20, 490, 220, fill="#ccc")
        c.create_rectangle(20, 240, 490, 480, fill="#ccc")
        c.create_rectangle(127, 25, 383, 217, fill="black", tags="top_screen")

        c.create_rectangle(127, 260, 383, 452, fill="#222", tags="touch_screen")
        c.create_text(255, 356, text="EDIT\nTOUCH\nSCREEN", fill="white", justify=tk.CENTER, tags="touch_screen")
        c.tag_bind("touch_screen", "<Button-1>", lambda e: VisualTouchEditor(self))

        buttons = [
            ("A", "ds_A", 455, 345), ("B", "ds_B", 435, 365), ("X", "ds_X", 435, 325), ("Y", "ds_Y", 415, 345),
            ("START", "ds_START", 455, 457), ("SELECT", "ds_SELECT", 425, 457),
            ("L", "ds_L", 65, 205), ("R", "ds_R", 445, 205)
        ]
        for text, tag, x, y in buttons:
            self.make_button(x, y, tag, text)

        for name, x, y in [("UP", 65, 335), ("DOWN", 65, 375), ("LEFT", 45, 355), ("RIGHT", 85, 355)]:
            self.make_button(x, y, "ds_" + name, name)

    def make_button(self, x, y, tag, text):
        self.canvas.create_oval(x - 15, y - 15, x + 15, y + 15, fill="gray", tags=tag)
        self.canvas.create_text(x, y, text=text, tags=tag)
        key = tag.replace("ds_", "")
        self.canvas.tag_bind(tag, "<Button-1>", lambda e, k=key: self.map_button(k))

    def map_button(self, key):
        def cb(a):
            if a:
                if a[0] == "None":
                    if key in single_mappings:
                        del single_mappings[key]
                else:
                    single_mappings[key] = a[0]
                save_config()
                self.redraw_mappings()
        ActionSelectorDialog(self, callback=cb)

    def redraw_mappings(self):
        self.canvas.delete("mapping_text")
        coords = {
            'A': (485, 330), 'B': (465, 380), 'X': (445, 305), 'Y': (385, 330),
            'UP': (67, 315), 'DOWN': (67, 393), 'LEFT': (25, 353), 'RIGHT': (110, 353),
            'L': (65, 205), 'R': (445, 205), 'START': (485, 470), 'SELECT': (395, 470)
        }
        for k, action in single_mappings.items():
            if k in coords and action != "None":
                x, y = coords[k]
                lbl = action.replace("Xbox ", "X-").replace("PS ", "P-").replace("Key ", "")
                self.canvas.create_text(x, y, text=lbl, fill="blue", font=("Arial", 8, "bold"), tags="mapping_text")

    def update_feedback(self):
        for key, mask in DS_KEYS.items():
            try:
                self.canvas.itemconfig("ds_" + key, fill=("red" if ds_live_state["keys"] & mask else "gray"))
            except Exception:
                pass
        self.after(50, self.update_feedback)

    def destroy(self):
        try:
            self.grab_release()
        except Exception:
            pass
        super().destroy()

class MainApp:
    def __init__(self, root):
        self.root = root
        root.title("DS Controller Server")
        root.geometry("380x250")

        load_config()

        frame = tk.Frame(root)
        frame.pack(pady=20)
        tk.Label(frame, text="Port:").pack(side=tk.LEFT)
        
        self.port = tk.StringVar(value=str(DEFAULT_PORT))
        tk.Entry(frame, textvariable=self.port, width=8).pack(side=tk.LEFT)

        self.start = tk.Button(root, text="Start Server", bg="#ccffcc", command=self.toggle)
        self.start.pack(pady=10)

        tk.Button(root, text="Controls / Mapping Manager", width=25, command=lambda: ControlsWindow(root)).pack(pady=3)
        tk.Button(root, text="Quick Save", width=25, command=save_config).pack(pady=3)
        tk.Button(root, text="Save Config As...", width=25, command=self.save_as_config).pack(pady=3)
        tk.Button(root, text="Load Config...", width=25, command=self.pick_config).pack(pady=3)
        
        self.config_label = tk.Label(root, text=f"Config: {os.path.basename(config_path)}", fg="gray", font=("Arial", 8))
        self.config_label.pack()

        try:
            global xbox_pad, ps_pad
            if xbox_pad is None:
                xbox_pad = vg.VX360Gamepad()
            if ps_pad is None:
                ps_pad = vg.VDS4Gamepad()
        except Exception as e:
            messagebox.showwarning(
                "Gamepad Warning", 
                f"Virtual gamepad driver failed to initialize. Controller outputs may not work.\n\nError: {e}"
            )
            
        self.root.after(10, self.poll_inputs)

    def pick_config(self):
        if choose_config_file(self.root):
            self.config_label.config(text=f"Config: {os.path.basename(config_path)}")

    def save_as_config(self):
        if save_config_dialog(self.root):
            self.config_label.config(text=f"Config: {os.path.basename(config_path)}")

    def toggle(self):
        global server_running, server_socket

        if server_running:
            server_running = False
            self.start.config(text="Start Server", bg="#ccffcc")

            with sock_lock:
                if client_conn:
                    try: client_conn.close()
                    except Exception: pass

            if server_socket:
                try: server_socket.close()
                except Exception: pass
            return

        try:
            port = int(self.port.get().strip())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Port", "Please enter a port between 1 and 65535.")
            return

        server_running = True
        threading.Thread(target=accept_loop, args=(port,), daemon=True).start()

        self.start.config(text="Stop Server", bg="#ffcccc")

    def poll_inputs(self):
        while True:
            try:
                keys, touch, tx, ty, last_tx, last_ty = input_queue.get_nowait()
            except queue.Empty:
                break
            
            try:
                process_inputs(keys, touch, tx, ty, last_tx, last_ty)
            except Exception as e:
                print(f"Input Processing Error: {e}")

        self.root.after(10, self.poll_inputs)

if __name__ == "__main__":
    root = tk.Tk()
    app = MainApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: cleanup_and_exit(root))
    root.mainloop()
