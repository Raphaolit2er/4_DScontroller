#include <nds.h>
#include <dswifi9.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <sys/ioctl.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>

#ifndef TCP_NODELAY
#define TCP_NODELAY 1
#endif

// ----- Configuration -----
char serverIP[16] = "192.168.1.100";
int serverPort = 8888;

// ----- Screen & Networking States -----
u16 backBottom[256 * 192] __attribute__((aligned(4)));
int bgBottom = -1;
u16 palette[256];
volatile bool running = false;

// Buffer global et Machine à états (Anti-Freeze)
static u8 rx_buf[200000] __attribute__((aligned(4)));
static u32 rx_got = 0;
static u32 rx_needed = 1;
static int rx_state = 0; // 0: CMD, 1: HEADER, 2: PAYLOAD
static u16 img_x, img_y, img_w, img_h;
static u32 img_dlen;

// ----- Custom Native Keyboard -----
#define KB_ROWS 4
#define KB_COLS 3
const char kb_keys[KB_ROWS][KB_COLS] = {
    {'7', '8', '9'}, {'4', '5', '6'},
    {'1', '2', '3'}, {'0', '\b', '\n'}
};
int kb_cx = 0, kb_cy = 0;
bool kb_visible = false;

const u8* getGlyph(char ch) {
    static const u8 glyph_0[] = {0x3E,0x51,0x49,0x45,0x3E};
    static const u8 glyph_1[] = {0x00,0x42,0x7F,0x40,0x00};
    static const u8 glyph_2[] = {0x42,0x61,0x51,0x49,0x46};
    static const u8 glyph_3[] = {0x21,0x41,0x45,0x4B,0x31};
    static const u8 glyph_4[] = {0x18,0x14,0x12,0x7F,0x10};
    static const u8 glyph_5[] = {0x27,0x45,0x45,0x45,0x39};
    static const u8 glyph_6[] = {0x3C,0x4A,0x49,0x49,0x30};
    static const u8 glyph_7[] = {0x01,0x71,0x09,0x05,0x03};
    static const u8 glyph_8[] = {0x36,0x49,0x49,0x49,0x36};
    static const u8 glyph_9[] = {0x06,0x49,0x49,0x29,0x1E};
    static const u8 glyph_less[]  = {0x00,0x08,0x14,0x22,0x41};
    static const u8 glyph_minus[] = {0x08,0x08,0x08,0x08,0x08};
    static const u8 glyph_E[] = {0x7F,0x49,0x49,0x49,0x41};
    static const u8 glyph_N[] = {0x7F,0x04,0x08,0x10,0x7F};
    static const u8 glyph_T[] = {0x01,0x01,0x7F,0x01,0x01};
    static const u8 glyph_blank[] = {0,0,0,0,0};

    switch(ch) {
        case '0': return glyph_0; case '1': return glyph_1;
        case '2': return glyph_2; case '3': return glyph_3;
        case '4': return glyph_4; case '5': return glyph_5;
        case '6': return glyph_6; case '7': return glyph_7;
        case '8': return glyph_8; case '9': return glyph_9;
        case '<': return glyph_less; case '-': return glyph_minus;
        case 'E': return glyph_E; case 'N': return glyph_N;
        case 'T': return glyph_T; default:  return glyph_blank;
    }
}

void swapBottom(void) {
    if (bgBottom != -1) {
        dmaCopyHalfWords(3, backBottom, bgGetGfxPtr(bgBottom), 256 * 192 * sizeof(u16));
    }
}

void drawKeyboardToBuffer(void) {
    if (!kb_visible) return;
    for (int i = 0; i < 256*192; i++) backBottom[i] = RGB15(3,3,3)|BIT(15);
    for (int row = 0; row < KB_ROWS; row++) {
        for (int col = 0; col < KB_COLS; col++) {
            int kx = 40 + col*60, ky = 96 + row*24, kw = 56, kh = 20;
            u16 bg = (kb_cy==row && kb_cx==col) ? RGB15(31,0,0)|BIT(15) : RGB15(12,12,12)|BIT(15);
            for (int py=0; py<kh; py++)
                for (int px=0; px<kw; px++) backBottom[(ky+py)*256 + (kx+px)] = bg;
            
            char label[4]; char c = kb_keys[row][col];
            if (c == '\b') strcpy(label, "<-"); 
            else if (c == '\n') strcpy(label, "ENT"); 
            else { label[0]=c; label[1]='\0'; }
            
            int textLen = strlen(label); int textPx = kx + (kw - textLen*6)/2 + 1; int textPy = ky + 6;
            for (int i=0; i<textLen; i++) {
                const u8* glyph = getGlyph(label[i]);
                for (int gx=0; gx<5; gx++) {
                    u8 colBits = glyph[gx];
                    for (int gy=0; gy<7; gy++) if ((colBits >> gy) & 1) backBottom[(textPy+gy)*256 + (textPx+gx)] = RGB15(31,31,31)|BIT(15);
                }
                textPx += 6;
            }
        }
    }
}

void refreshKeyboard(void) { drawKeyboardToBuffer(); swapBottom(); }
void showCustomKeyboard(void) { kb_cx=0; kb_cy=0; kb_visible=true; refreshKeyboard(); }
void hideCustomKeyboard(void) { kb_visible=false; memset(backBottom, 0, sizeof(backBottom)); swapBottom(); }

char updateCustomKeyboard(u32 down, u32 repeat) {
    bool changed = false;
    if (repeat & KEY_UP)    { kb_cy--; if (kb_cy<0) kb_cy=KB_ROWS-1; changed=true; }
    if (repeat & KEY_DOWN)  { kb_cy++; if (kb_cy>=KB_ROWS) kb_cy=0; changed=true; }
    if (repeat & KEY_LEFT)  { kb_cx--; if (kb_cx<0) kb_cx=KB_COLS-1; changed=true; }
    if (repeat & KEY_RIGHT) { kb_cx++; if (kb_cx>=KB_COLS) kb_cx=0; changed=true; }
    if (down & KEY_TOUCH) {
        touchPosition touch; touchRead(&touch);
        if (touch.py >= 96 && touch.px >= 40) {
            int gx = (touch.px - 40) / 60; int gy = (touch.py - 96) / 24;
            if (gx>=0 && gx<KB_COLS && gy>=0 && gy<KB_ROWS) { kb_cx = gx; kb_cy = gy; down |= KEY_A; changed = true; }
        }
    }
    if (changed) refreshKeyboard();
    if (down & KEY_A) return kb_keys[kb_cy][kb_cx];
    return 0;
}

int getInputString(char *buf, int maxLen) {
    showCustomKeyboard();
    int len = 0; buf[0] = '\0';
    iprintf("\x1b[1;0H> %-7s   ", buf); 
    
    while (1) {
        swiWaitForVBlank(); scanKeys();
        char c = updateCustomKeyboard(keysDown(), keysDownRepeat());
        if (c > 0) {
            if (c == '\n') break;
            else if (c == '\b') { if (len > 0) buf[--len] = '\0'; }
            else if (len < maxLen - 1) { buf[len++] = c; buf[len] = '\0'; }
            iprintf("\x1b[1;0H> %-7s   ", buf); 
        }
        if (keysDown() & KEY_SELECT) { buf[0] = '\0'; len = 0; break; }
    }
    hideCustomKeyboard();
    return len;
}

void editIP(void) {
    int octets[4] = {0}; int cursor=0, holdFrames=0; const int HOLD_DELAY=20, REPEAT_RATE=3;
    sscanf(serverIP, "%d.%d.%d.%d", &octets[0], &octets[1], &octets[2], &octets[3]);
    consoleClear();
    bool draw = true;
    while (1) {
        if (draw) {
            printf("\x1b[0;0HSet Server IP (D-Pad, A=OK):\n");
            for (int i=0; i<4; i++) {
                if (i==cursor) printf("[%3d]", octets[i]); else printf(" %3d ", octets[i]);
                if (i<3) printf(".");
            }
            printf("\n\nHold UP/DOWN to change\nA: confirm IP");
            draw = false;
        }
        
        swiWaitForVBlank(); scanKeys(); u32 held = keysHeld(), down = keysDown();
        if (down & KEY_RIGHT && cursor<3) { cursor++; draw = true; }
        if (down & KEY_LEFT  && cursor>0) { cursor--; draw = true; }
        
        if (held & KEY_UP) {
            if (down & KEY_UP) { octets[cursor] = (octets[cursor]+1)%256; holdFrames=0; draw = true; }
            else { holdFrames++; if (holdFrames>=HOLD_DELAY && (holdFrames-HOLD_DELAY)%REPEAT_RATE==0) { octets[cursor] = (octets[cursor]+1)%256; draw = true; } }
        } else if (held & KEY_DOWN) {
            if (down & KEY_DOWN) { octets[cursor] = (octets[cursor]-1<0)?255:octets[cursor]-1; holdFrames=0; draw = true; }
            else { holdFrames++; if (holdFrames>=HOLD_DELAY && (holdFrames-HOLD_DELAY)%REPEAT_RATE==0) { octets[cursor] = (octets[cursor]-1<0)?255:octets[cursor]-1; draw = true; } }
        } else holdFrames=0;
        
        if (down & KEY_A) break;
    }
    sprintf(serverIP, "%d.%d.%d.%d", octets[0], octets[1], octets[2], octets[3]);
    consoleClear();
}

// ----- RLE Rendering -----
void drawImageRLE(u16 *buf, u16 x, u16 y, u16 w, u16 h, const u8 *data, u32 dataLen) {
    if (x >= 256 || y >= 192) return; 
    int cx = x, cy = y;
    const u8 *ptr = data, *end = data + dataLen;
    while (ptr < end) {
        u8 count = *ptr++;
        if (!count || ptr >= end) break; 
        u8 idx = *ptr++;
        u16 color = palette[idx];
        
        for (u8 i = 0; i < count; i++) {
            if (cx < 256 && cy < 192) buf[cy * 256 + cx] = color | BIT(15); 
            cx++;
            if (cx >= x + w) { cx = x; cy++; if (cy >= y + h) return; }
        }
    }
}

// ----- Machine à états Non-Bloquante -----
void process_network(int sock) {
    while (running) {
        int r = recv(sock, rx_buf + rx_got, rx_needed - rx_got, 0);
        
        if (r == 0) {
            running = false; 
            return;
        }
        if (r < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) return; 
            running = false; 
            return;
        }

        rx_got += r;
        if (rx_got < rx_needed) continue; 

        if (rx_state == 0) { 
            u8 cmd = rx_buf[0];
            rx_got = 0;
            if (cmd == 0xFE) {
                swapBottom();
                rx_needed = 1;
            } 
            else if (cmd == 0x01) {
                rx_state = 1;
                rx_needed = 12; 
            } else {
                rx_needed = 1; 
            }
        } 
        else if (rx_state == 1) { 
            img_x = (rx_buf[0] << 8) | rx_buf[1];
            img_y = (rx_buf[2] << 8) | rx_buf[3];
            img_w = (rx_buf[4] << 8) | rx_buf[5];
            img_h = (rx_buf[6] << 8) | rx_buf[7];
            img_dlen = (rx_buf[8] << 24) | (rx_buf[9] << 16) | (rx_buf[10] << 8) | rx_buf[11];
            
            rx_got = 0;
            if (img_dlen > 0 && img_dlen <= sizeof(rx_buf)) {
                rx_state = 2;
                rx_needed = img_dlen;
            } else {
                rx_state = 0;
                rx_needed = 1;
            }
        } 
        else if (rx_state == 2) { 
            drawImageRLE(backBottom, img_x, img_y, img_w, img_h, rx_buf, img_dlen);
            rx_got = 0;
            rx_state = 0; 
            rx_needed = 1; 
        }
    }
}

// ----- Reliable send: retries until EVERY byte is queued -----
// Non-blocking send() can return fewer bytes than requested (partial send)
// or -1/EAGAIN when the Wi-Fi TX buffer is full. Dropping those bytes
// desyncs the TCP stream and freezes the PC's parser. This wrapper loops
// until the whole packet is sent, waiting one VBlank between retries.
int send_all(int sock, const uint8_t *data, int len) {
    int total = 0;
    while (total < len && running) {
        int n = send(sock, data + total, len - total, 0);
        if (n > 0) {
            total += n;
        } else if (n == 0) {
            swiWaitForVBlank();
        } else if (errno == EAGAIN || errno == EWOULDBLOCK) {
            swiWaitForVBlank();   // let the Wi-Fi hardware drain, then retry
        } else {
            return -1;            // fatal error (peer closed, etc.)
        }
    }
    return total;
}

void menuMode(void) {
    videoSetMode(MODE_0_2D); vramSetBankA(VRAM_A_MAIN_BG);
    videoSetModeSub(MODE_5_2D); vramSetBankC(VRAM_C_SUB_BG);
    bgBottom = bgInitSub(3, BgType_Bmp16, BgSize_B16_256x256, 0, 0); 
    REG_DISPCNT_SUB = MODE_5_2D | DISPLAY_BG3_ACTIVE;
    consoleInit(NULL, 0, BgType_Text4bpp, BgSize_T_256x256, 31, 0, true, true);
    memset(backBottom, 0, sizeof(backBottom)); swapBottom();
}

void runController(void) {
    iprintf("Connecting to AP...\n");
    if (!Wifi_InitDefault(WFC_CONNECT)) { iprintf("WiFi init failed!\n"); while(1) swiWaitForVBlank(); }
    while (1) {
        int st = Wifi_AssocStatus();
        if (st == ASSOCSTATUS_ASSOCIATED) break;
        if (st == ASSOCSTATUS_CANNOTCONNECT) { iprintf("Cannot connect to AP.\n"); while (1) swiWaitForVBlank(); }
        swiWaitForVBlank();
    }

    // Touch stroke history (persists across reconnects, reset per connection)
    static touchPosition touch_buffer[10];
    static int touch_count = 0;
    static int last_touch_state = 0;
    static int last_tx = -1, last_ty = -1;

    bool quit = false;
    while (!quit) {
        int sock = socket(AF_INET, SOCK_STREAM, 0);
        if (sock < 0) { iprintf("Socket error\n"); while(1) swiWaitForVBlank(); }

        struct sockaddr_in saddr;
        memset(&saddr, 0, sizeof(saddr));
        saddr.sin_family = AF_INET;
        saddr.sin_port = htons(serverPort);
        saddr.sin_addr.s_addr = inet_addr(serverIP);

        iprintf("Connecting to PC... (B=quit)\n");
        if (connect(sock, (struct sockaddr*)&saddr, sizeof(saddr)) < 0) {
            iprintf("Failed! Retrying... (B=quit)\n");
            closesocket(sock);
            for (int i = 0; i < 120 && !quit; i++) {   // ~2s retry delay
                swiWaitForVBlank(); scanKeys();
                if (keysDown() & KEY_B) quit = true;
            }
            continue;
        }

        iprintf("Connected!\n");

        memset(backBottom, 0, sizeof(backBottom));
        swapBottom();

        // Low latency (no batching of 1-byte packets) + non-blocking I/O
        int one = 1;
        setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        ioctl(sock, FIONBIO, &one);

        running = true;
        int frameCounter = 0;

        rx_state = 0; rx_got = 0; rx_needed = 1;
        touch_count = 0; last_touch_state = 0; last_tx = -1; last_ty = -1;

        while (running) {
            swiWaitForVBlank();
            scanKeys();
            frameCounter++;

            u32 held = keysHeld();
            u32 pressed = keysDown();
            u32 released = keysUp();

            process_network(sock);
            if (!running) break;

            // 1. Send Buttons Instantly (1 byte protocol) — CHECKED sends
            for (int i = 0; i < 12; i++) {
                if (pressed & (1 << i)) {
                    uint8_t packet = i;
                    if (send_all(sock, &packet, 1) < 0) { running = false; break; }
                }
                if (released & (1 << i)) {
                    uint8_t packet = i | 0x80;
                    if (send_all(sock, &packet, 1) < 0) { running = false; break; }
                }
            }
            if (!running) break;

            // 2. Buffer Touch Movements (60Hz reading)
            int touch_active = (held & KEY_TOUCH) ? 1 : 0;
            if (touch_active) {
                touchPosition touch;
                touchRead(&touch);
                if (!last_touch_state || abs(touch.px - last_tx) > 0 || abs(touch.py - last_ty) > 0) {
                    if (touch_count < 10) touch_buffer[touch_count++] = touch;
                    last_tx = touch.px;
                    last_ty = touch.py;
                }
            }

            // 3. Flush the list to the network (30Hz pacing)
            if ((frameCounter & 1) == 0) {
                // A. Flush any buffered points first
                if (touch_count > 0) {
                    uint8_t packet[64];
                    int idx = 0;
                    packet[idx++] = 0xFC;           // List START
                    packet[idx++] = touch_count;    // How many points
                    for (int i = 0; i < touch_count; i++) {
                        packet[idx++] = (touch_buffer[i].px >> 8) & 0xFF;
                        packet[idx++] = touch_buffer[i].px & 0xFF;
                        packet[idx++] = (touch_buffer[i].py >> 8) & 0xFF;
                        packet[idx++] = touch_buffer[i].py & 0xFF;
                    }
                    packet[idx++] = 0xFD;           // List END
                    if (send_all(sock, packet, idx) < 0) running = false;
                    touch_count = 0; // Empty the buffer after sending
                }
                
                // B. Independently check if the screen was released
                if (!touch_active && last_touch_state) {
                    // Screen was just released (List with 0 items)
                    uint8_t packet[3] = {0xFC, 0x00, 0xFD};
                    if (send_all(sock, packet, 3) < 0) running = false;
                }
                
                last_touch_state = touch_active;
            }
        }

        running = false;
        closesocket(sock);

        if (!quit) {
            iprintf("Disconnected. Reconnecting... (B=quit)\n");
            for (int i = 0; i < 60 && !quit; i++) {   // ~1s before reconnect
                swiWaitForVBlank(); scanKeys();
                if (keysDown() & KEY_B) quit = true;
            }
        }
    }
}

int main(void) {
    for (int i = 0; i < 256; i++) {
        u8 r = (i>>5)&7, g = (i>>2)&7, b = i&3;
        palette[i] = RGB15((r*31)/7, (g*31)/7, (b*31)/3) | BIT(15);
    }
    menuMode();

    while (1) {
        iprintf("\x1b[2JDS Controller\n\n");
        iprintf("Target: %s:%d\n\n", serverIP, serverPort);
        iprintf("A: Start Controller Mode\n");
        iprintf("Y: Config IP and Port\n");

        while (1) {
            swiWaitForVBlank(); scanKeys();
            u32 down = keysDown();
            
            if (down & KEY_A) {
                iprintf("\x1b[2J");
                runController();
                menuMode();
                break;
            }
            if (down & KEY_Y) {
                editIP();
                iprintf("\x1b[2JEnter new Port:\n\n> ");
                char newPortStr[8];
                if (getInputString(newPortStr, sizeof(newPortStr)) > 0) {
                    int p = atoi(newPortStr);
                    if (p > 0 && p <= 65535) serverPort = p;
                }
                menuMode();
                break;
            }
        }
    }
    return 0;
}
