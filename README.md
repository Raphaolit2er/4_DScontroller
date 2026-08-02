DS Controller: A tool that turns your Nintendo DS into a wireless gamepad/mouse to control your computer over local Wi-Fi.

What You Need:
- A Nintendo DS flashcart or a DS emulator that supports Wi-Fi simulation (such as **MelonDS** or **No$GBA**).
- A computer to run the Python code
- Python 3.8 or higher
- Run `pip install Pillow vgamepad pynput`

How to Run:
1. Open the Python server script file.
2. Click the Start Server button. The server will listen via TCP on your chosen port.
3. Boot the compiled client "ds-controller-client.nds" file on your DS hardware or emulator.
4. Press Y to open the server config, use the D-pad to enter your host PC's local IP address, and type the port number using the on-screen keypad. Press A to confirm.
5. Press A to switch the DS into Controller Mode.
6. Change your mapping or add combos to the server.
7. You can save or load files into a dscon format (just a json underneath).
8. Enjoy your games with your Nintendo DS as a controller!

Disclaimers:
- Multiple consoles at a time aren't tested and aren't recommended.
- The control responsiveness relies entirely on local Wi-Fi stability and TCP packet transmission. From my testing, it was fast enough to play games such as Roblox Doors, Roblox Forsaken and BeamNG.Drive.
- Memory leaks are a possibility although might be rare.
- Windows note: `vgamepad` installs a virtual driver allowing your PC to recognize the DS as a real Xbox 360 or PlayStation 4 controller.
- This project was made using AIs, as I am not a good enough coder to do that on my own. Sorry to people who thought it was made by hand.
