import sys
import os
import socket
import struct
import threading
import time
import subprocess
import re
import math
import tkinter as tk
from tkinter import ttk, messagebox
import pyaudio

# ── Defaults ──────────────────────────────────────────────────
DEFAULT_PORT = 8888

# ── Global server state ──────────────────────────────────────
server_running = False
server_socket = None
current_port = DEFAULT_PORT
current_sample_rate = 8000
ds_address = None
accept_thread = None
audio_output = None
monitor_active = False

# Global variable shared with GUI for visualizer RMS level
current_rms_level = 0.0

# ── IP listing ────────────────────────────────────────────────
def get_local_ips():
    ips = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        active_ip = s.getsockname()[0]
        ips["Primary Network (Wi-Fi/LAN)"] = active_ip
        s.close()
    except Exception:
        pass

    if sys.platform.startswith("win"):
        try:
            out = subprocess.check_output("ipconfig", encoding="utf-8", errors="ignore")
            adapter_re = re.compile(r'^([^\s].*?):$', re.IGNORECASE)
            ipv4_re = re.compile(r'IPv4.*?:\s*(\d+\.\d+\.\d+\.\d+)', re.IGNORECASE)
            current = None
            for line in out.splitlines():
                m = adapter_re.match(line)
                if m:
                    current = m.group(1).strip()
                elif current and "IPv4" in line:
                    m = ipv4_re.search(line)
                    if m:
                        ip = m.group(1)
                        if ip != "127.0.0.1" and ip not in ips.values():
                            ips[current] = ip
        except Exception:
            pass
        return ips
    return ips

def get_sorted_ips():
    all_ips = get_local_ips()
    primary, priority, other = [], [], []
    virtual_keywords = ["virtual", "vmware", "vbox", "hyper-v", "wsl", "tailscale", "vpn"]

    for name, ip in all_ips.items():
        lower_name = name.lower()
        if "primary network" in lower_name:
            primary.append((name, ip))
        elif not any(v in lower_name for v in virtual_keywords) and any(kw in lower_name for kw in ["ethernet", "wi-fi", "wireless"]):
            priority.append((name, ip))
        else:
            other.append((name, ip))
    return primary + priority + other

# ── Audio Device Discovery (Canonical De-Duplication) ─────────
# ── Audio Device Discovery (Canonical De-Duplication) ─────────
def get_audio_devices():
    p = pyaudio.PyAudio()
    inputs = []
    outputs = []
    
    seen_inputs = set()
    seen_outputs = set()

    for i in range(p.get_device_count()):
        try:
            dev = p.get_device_info_by_index(i)
            name = dev.get('name', 'Unknown')
            name_lower = name.lower()
            
            # Filter specifically for virtual pipelines
            if any(kw in name_lower for kw in ["cable", "virtual", "point", "voicemeeter", "sonar", "broadcast", "nintendo ds"]):
                
                # Fix VB-Audio names explicitly
                if "cable input" in name_lower:
                    canonical_key = "vb_cable_input"
                    clean_name = "CABLE Input (VB-Audio Virtual Cable)"
                elif "cable output" in name_lower:
                    canonical_key = "vb_cable_output"
                    clean_name = "CABLE Output (VB-Audio Virtual Cable)"
                elif "audio point" in name_lower:
                    canonical_key = "vb_audio_point"
                    clean_name = name
                else:
                    # Keep the full name for Nvidia Broadcast, Sonar, etc.
                    clean_name = name
                    # Match by the first 25 chars so Windows truncations merge together perfectly
                    canonical_key = name_lower[:25] 
                
                # Register Outputs 
                if dev.get('maxOutputChannels', 0) > 0 and canonical_key not in seen_outputs:
                    seen_outputs.add(canonical_key)
                    outputs.append((i, clean_name))
                    
                # Register Inputs 
                if dev.get('maxInputChannels', 0) > 0 and canonical_key not in seen_inputs:
                    seen_inputs.add(canonical_key)
                    inputs.append((i, clean_name))
        except Exception:
            pass
            
    p.terminate()
    return inputs, outputs

# ── Audio Output Handler (THREAD SAFE) ────────────────────────
class AudioOutput:
    def __init__(self, device_index=None):
        self.p = pyaudio.PyAudio()
        self.device_index = device_index
        self.stream = None
        self.lock = threading.Lock()

    def start(self, rate):
        with self.lock:
            if self.stream is not None:
                return True
            try:
                self.stream = self.p.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=rate,
                    output=True,
                    output_device_index=self.device_index,
                    frames_per_buffer=512
                )
                print(f"Audio stream opened at {rate}Hz on device index {self.device_index}.")
                return True
            except Exception as e:
                print(f"Failed to open audio: {e}")
                return False

    def write(self, data):
        with self.lock:
            if self.stream:
                try:
                    self.stream.write(data)
                except Exception:
                    pass

    def stop(self):
        with self.lock:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
                self.stream = None

    def close(self):
        self.stop()
        self.p.terminate()

# ── Server management ──────────────────────────────────────────
def send_settings():
    global server_socket, ds_address, current_sample_rate
    if server_socket and ds_address:
        try:
            payload = struct.pack('>BH', 0x11, current_sample_rate)
            server_socket.sendto(payload, ds_address)
            print(f"Pushed settings to DS: {current_sample_rate}Hz")
        except Exception as e:
            print(f"Failed to send settings: {e}")

def receive_loop():
    global server_running, server_socket, audio_output, ds_address, current_sample_rate, current_rms_level
    audio_active = False
    last_packet_time = time.time()

    server_socket.settimeout(1.0)
    print("Listening for DS packets...")

    while server_running:
        try:
            packet, addr = server_socket.recvfrom(4096)
            if not packet:
                continue

            ds_address = addr

            # DS requested settings (0x10)
            if packet[0] == 0x10:
                print(f"DS connected from {addr}. Sending settings...")
                send_settings()

            # DS sent audio frame (0x20)
            elif packet[0] == 0x20:
                length = struct.unpack('>H', packet[1:3])[0]
                if length > 0 and len(packet) >= 3 + length:
                    pcm_data = packet[3:3+length]
                    
                    # Compute simple RMS amplitude for visualizer
                    if len(pcm_data) >= 2:
                        samples = struct.unpack(f">{len(pcm_data)//2}h", pcm_data)
                        sum_squares = sum(s * s for s in samples)
                        rms = math.sqrt(sum_squares / len(samples)) if samples else 0
                        current_rms_level = min(rms / 32767.0, 1.0) # Normalize 0.0 to 1.0

                    if not audio_active:
                        if audio_output.start(current_sample_rate):
                            audio_active = True
                    if audio_active:
                        audio_output.write(pcm_data)
                    last_packet_time = time.time()

        except socket.timeout:
            if audio_active and (time.time() - last_packet_time > 2.0):
                print("DS stream inactive. Closing audio stream...")
                audio_output.stop()
                audio_active = False
                ds_address = None
                current_rms_level = 0.0
            continue
        except Exception as e:
            print(f"Receive error: {e}")
            break

def start_server(output_device_idx):
    global server_running, server_socket, accept_thread, audio_output
    if server_running:
        return True

    if audio_output is None:
        audio_output = AudioOutput(device_index=output_device_idx)

    try:
        if server_socket:
            server_socket.close()
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server_socket.bind(("0.0.0.0", current_port))

        server_running = True
        accept_thread = threading.Thread(target=receive_loop, daemon=True)
        accept_thread.start()
        return True
    except Exception as e:
        messagebox.showerror("Server Error", f"Failed to bind to port {current_port}: {e}")
        return False

def stop_server():
    global server_running, server_socket, audio_output, ds_address, current_rms_level
    server_running = False
    ds_address = None
    current_rms_level = 0.0
    if server_socket:
        try:
            server_socket.close()
        except:
            pass
        server_socket = None
    if audio_output:
        audio_output.close()
        audio_output = None

# ── GUI ───────────────────────────────────────────────────────
class ServerGUI:
    def __init__(self, root):
        self.root = root
        root.title("DS Mic Server & Monitor")
        root.geometry("480x820")
        root.resizable(False, False)

        tk.Label(root, text="Local IP Addresses", font=("Arial", 10, "bold")).pack(pady=(10,0))
        self.ip_listbox = tk.Listbox(root, height=4, selectmode=tk.SINGLE)
        self.ip_listbox.pack(fill=tk.X, padx=10, pady=5)
        self.refresh_ips()

        frame_port = tk.Frame(root)
        frame_port.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(frame_port, text="Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        tk.Entry(frame_port, textvariable=self.port_var, width=8).pack(side=tk.LEFT, padx=5)

        # Audio Quality Frame
        frame_mode = tk.Frame(root)
        frame_mode.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(frame_mode, text="Audio Quality:").pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value="Standard (8000 Hz)")
        ttk.Combobox(frame_mode, textvariable=self.mode_var,
                     values=["Low Quality (4000 Hz)", "Standard (8000 Hz)", "High Quality (16384 Hz)"],
                     state="readonly", width=24).pack(side=tk.LEFT, padx=5)

        # Fetch devices for dropdowns
        self.input_devices, self.output_devices = get_audio_devices()
        
        # Input Device Selection Frame (For Monitor / Physical Mic)
        frame_in = tk.Frame(root)
        frame_in.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(frame_in, text="Monitor Input:").pack(side=tk.LEFT)
        self.in_names = [name for idx, name in self.input_devices]
        self.in_var = tk.StringVar()
        self.in_combo = ttk.Combobox(frame_in, textvariable=self.in_var, values=self.in_names, state="readonly", width=30)
        self.in_combo.pack(side=tk.LEFT, padx=5)
        if self.in_names:
            self.in_combo.current(0)

        # Output Device Selection Frame (Virtual Cable Target)
        frame_out = tk.Frame(root)
        frame_out.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(frame_out, text="Virtual Cable Out:").pack(side=tk.LEFT)
        self.out_names = [name for idx, name in self.output_devices]
        self.out_var = tk.StringVar()
        self.out_combo = ttk.Combobox(frame_out, textvariable=self.out_var, values=self.out_names, state="readonly", width=30)
        self.out_combo.pack(side=tk.LEFT, padx=5)
        
        # Default Output to CABLE Input if available
        default_out_idx = 0
        for idx, name in enumerate(self.out_names):
            if "cable input" in name.lower():
                default_out_idx = idx
                break
        if self.out_names:
            self.out_combo.current(default_out_idx)

        # ── Visualizer Canvas ──────────────────────────────────
        vis_frame = tk.Frame(root, bg="#111111", relief=tk.SUNKEN, borderwidth=1)
        vis_frame.pack(fill=tk.X, padx=10, pady=8)
        
        tk.Label(vis_frame, text="-- Input Level --", fg="#888888", bg="#111111", font=("Arial", 8)).pack(anchor=tk.W, padx=5, pady=2)
        self.canvas = tk.Canvas(vis_frame, width=450, height=60, bg="#111111", highlightthickness=0)
        self.canvas.pack(padx=5, pady=2)

        # Buttons Frame
        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=5)
        self.start_btn = tk.Button(btn_frame, text="Start Server", command=self.toggle_server, width=11)
        self.start_btn.pack(side=tk.LEFT, padx=3)
        self.apply_btn = tk.Button(btn_frame, text="Apply", command=self.apply_settings, width=8)
        self.apply_btn.pack(side=tk.LEFT, padx=3)
        
        # Hear Yourself Monitor Toggle Button
        self.monitor_btn = tk.Button(btn_frame, text="Hear Self: OFF", bg="#ffcccc", command=self.toggle_monitor, width=13)
        self.monitor_btn.pack(side=tk.LEFT, padx=3)

        self.status_var = tk.StringVar(value="Stopped")
        tk.Label(root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(fill=tk.X, padx=10, pady=3)
        self.client_status_var = tk.StringVar(value="No client connected")
        tk.Label(root, textvariable=self.client_status_var, relief=tk.SUNKEN, anchor=tk.W).pack(fill=tk.X, padx=10, pady=3)

        tk.Button(root, text="Refresh Devices", command=self.refresh_all, width=18).pack(pady=3)

        self.update_loop()

    def refresh_ips(self):
        self.ip_listbox.delete(0, tk.END)
        for name, ip in get_sorted_ips():
            self.ip_listbox.insert(tk.END, f"{name}: {ip}")

    def refresh_all(self):
        self.refresh_ips()
        self.input_devices, self.output_devices = get_audio_devices()
        self.in_names = [name for idx, name in self.input_devices]
        self.out_names = [name for idx, name in self.output_devices]
        self.in_combo['values'] = self.in_names
        self.out_combo['values'] = self.out_names
        
        if self.in_names and self.in_var.get() not in self.in_names:
            self.in_combo.current(0)
        if self.out_names and self.out_var.get() not in self.out_names:
            self.out_combo.current(0)

    def get_selected_output_index(self):
        selected_name = self.out_var.get()
        for idx, name in self.output_devices:
            if name == selected_name:
                return idx
        return None

    def get_selected_input_index(self):
        selected_name = self.in_var.get()
        for idx, name in self.input_devices:
            if name == selected_name:
                return idx
        return None

    def toggle_server(self):
        global current_port, current_sample_rate
        if server_running:
            stop_server()
            self.start_btn.config(text="Start Server")
            self.status_var.set("Stopped")
        else:
            try:
                current_port = int(self.port_var.get())
            except ValueError:
                messagebox.showerror("Invalid Port", "Port must be a number.")
                return

            if "16384" in self.mode_var.get():
                current_sample_rate = 16384
            elif "4000" in self.mode_var.get():
                current_sample_rate = 4000
            else:
                current_sample_rate = 8000

            out_idx = self.get_selected_output_index()

            if start_server(out_idx):
                self.start_btn.config(text="Stop Server")
                self.status_var.set(f"Running on port {current_port} ({current_sample_rate} Hz)")

    def apply_settings(self):
        global current_port, current_sample_rate, audio_output
        try:
            new_port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("Invalid Port", "Port must be a number.")
            return

        if "16384" in self.mode_var.get():
            new_rate = 16384
        elif "4000" in self.mode_var.get():
            new_rate = 4000
        else:
            new_rate = 8000

        out_idx = self.get_selected_output_index()

        rate_changed = (new_rate != current_sample_rate)
        device_changed = (audio_output and audio_output.device_index != out_idx)

        if rate_changed or device_changed:
            current_sample_rate = new_rate
            if server_running:
                if audio_output:
                    audio_output.stop()
                    audio_output.device_index = out_idx
                    audio_output.start(current_sample_rate)
                if ds_address:
                    send_settings()

        if new_port != current_port:
            current_port = new_port
            if server_running:
                stop_server()
                start_server(out_idx)

        if server_running:
            self.status_var.set(f"Running on port {current_port} ({current_sample_rate} Hz)")

    def toggle_monitor(self):
        global monitor_active
        if monitor_active:
            monitor_active = False
            self.monitor_btn.config(text="Hear Self: OFF", bg="#ffcccc")
        else:
            in_idx = self.get_selected_input_index()
            out_idx = self.get_selected_output_index()
            if in_idx is None or out_idx is None:
                messagebox.showerror("Error", "Please select valid input and output devices.")
                return
            monitor_active = True
            self.monitor_btn.config(text="Hear Self: ON", bg="#ccffcc")
            threading.Thread(target=self.monitor_loop, args=(in_idx, out_idx), daemon=True).start()

    def monitor_loop(self, in_idx, out_idx):
        global monitor_active
        p = pyaudio.PyAudio()
        try:
            in_stream = p.open(format=pyaudio.paInt16, channels=1, rate=current_sample_rate, input=True, input_device_index=in_idx, frames_per_buffer=512)
            out_stream = p.open(format=pyaudio.paInt16, channels=1, rate=current_sample_rate, output=True, output_device_index=out_idx, frames_per_buffer=512)
            while monitor_active:
                try:
                    data = in_stream.read(512, exception_on_overflow=False)
                    out_stream.write(data)
                except Exception:
                    break
            in_stream.stop_stream()
            in_stream.close()
            out_stream.stop_stream()
            out_stream.close()
        except Exception as e:
            print(f"Monitor error: {e}")
            monitor_active = False
            # Needs to run on main thread to update UI safely
            self.root.after(0, lambda: self.monitor_btn.config(text="Hear Self: OFF", bg="#ffcccc")) 
        p.terminate()

    def update_loop(self):
        # Update client status text
        if ds_address:
            self.client_status_var.set(f"Client connected: {ds_address[0]}")
        else:
            self.client_status_var.set("No client connected")

        # Draw the real-time sound visualizer level bar
        self.canvas.delete("all")
        width, height = 450, 60
        
        # Draw background decibel lines
        for y_db in [-10, -20, -40]:
            y_pos = height - ((y_db + 50) / 50.0) * height
            self.canvas.create_line(0, y_pos, width, y_pos, fill="#222222")

        # Draw current live level bar
        bar_width = int(width * current_rms_level)
        self.canvas.create_rectangle(0, 0, bar_width, height, fill="#00ff66", outline="")

        self.root.after(50, self.update_loop)

if __name__ == "__main__":
    root = tk.Tk()
    app = ServerGUI(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (stop_server(), root.destroy()))
    root.mainloop()