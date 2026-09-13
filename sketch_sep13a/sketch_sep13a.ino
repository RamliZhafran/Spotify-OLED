#include <Arduino.h>
#include <Wire.h>
#include <U8g2lib.h>

// OLED SSD1306 128x64 I2C (Page Buffer — saves RAM vs _F_)
U8G2_SSD1306_128X64_NONAME_1_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE);

// ── playback fields ─────────────────────────────────────────────────────
char title[36];
char artist[28];
char position[8];
char duration[8];
char l1_rev[26];
char l1_full[26];
char l2_rev[26];
char l2_full[26];
char bottomInfo[64];

byte playerStatus = 0;
byte progress = 0;

// ── serial framing & marquee ────────────────────────────────────────────
char serialBuffer[200];
bool newData = false;
int  scrollX = 0;
unsigned long lastScrollTime = 0;

// Empty-field-safe tokenizer (strtok drops empty fields)
char *getNextToken(char **cursor)
{
  if (*cursor == NULL) return NULL;
  char *start = *cursor;
  char *delim = strchr(start, ';');
  if (delim) { *delim = '\0'; *cursor = delim + 1; }
  else       { *cursor = NULL; }
  return start;
}

// ── setup ───────────────────────────────────────────────────────────────
void setup()
{
  Serial.begin(115200);

  Wire.begin();
  Wire.setClock(400000);
  u8g2.begin();
  u8g2.setBusClock(400000);

  title[0]    = '\0';
  artist[0]   = '\0';
  position[0] = '\0';
  duration[0] = '\0';
  l1_rev[0]   = '\0';
  l1_full[0]  = '\0';
  l2_rev[0]   = '\0';
  l2_full[0]  = '\0';
  bottomInfo[0] = '\0';

  u8g2.firstPage();
  do {
    u8g2.setFont(u8g2_font_6x10_tf);
    u8g2.drawStr(35, 28, "SPOTIFY");
    u8g2.drawStr(25, 44, "Connecting...");
  } while (u8g2.nextPage());
}

// ── main loop ───────────────────────────────────────────────────────────
void loop()
{
  unsigned long nowMs = millis();
  readSerial();

  // smooth marquee
  int infoW = u8g2.getStrWidth(bottomInfo);
  bool needScroll = (infoW > 128 && nowMs - lastScrollTime > 50);
  if (needScroll) {
    scrollX = (scrollX + 2) % (infoW + 30);
    lastScrollTime = nowMs;
  }

  if (newData || needScroll) {
    drawSpotify();
    newData = false;
  }
}

// ── packet parser ───────────────────────────────────────────────────────
// status;volume;title;artist;position;duration;progress;
// l1_rev;l1_full;l2_rev;l2_full
void parseData(char *data)
{
  char *cursor = data;

  char *tok = getNextToken(&cursor);
  if (tok) playerStatus = atoi(tok);

  getNextToken(&cursor); // volume (unused)

  tok = getNextToken(&cursor);
  if (tok) { strncpy(title, tok, sizeof(title)-1); title[sizeof(title)-1]='\0'; }

  tok = getNextToken(&cursor);
  if (tok) { strncpy(artist, tok, sizeof(artist)-1); artist[sizeof(artist)-1]='\0'; }

  snprintf(bottomInfo, sizeof(bottomInfo), "%s - %s", title, artist);

  tok = getNextToken(&cursor);
  if (tok) { strncpy(position, tok, sizeof(position)-1); position[sizeof(position)-1]='\0'; }

  tok = getNextToken(&cursor);
  if (tok) { strncpy(duration, tok, sizeof(duration)-1); duration[sizeof(duration)-1]='\0'; }

  tok = getNextToken(&cursor);
  if (tok) {
    int val = atoi(tok);
    progress = (val < 0) ? 0 : (val > 100) ? 100 : val;
  }

  tok = getNextToken(&cursor);
  if (tok) { strncpy(l1_rev, tok, sizeof(l1_rev)-1); l1_rev[sizeof(l1_rev)-1]='\0'; }
  else     { l1_rev[0] = '\0'; }

  tok = getNextToken(&cursor);
  if (tok) { strncpy(l1_full, tok, sizeof(l1_full)-1); l1_full[sizeof(l1_full)-1]='\0'; }
  else     { l1_full[0] = '\0'; }

  tok = getNextToken(&cursor);
  if (tok) { strncpy(l2_rev, tok, sizeof(l2_rev)-1); l2_rev[sizeof(l2_rev)-1]='\0'; }
  else     { l2_rev[0] = '\0'; }

  tok = getNextToken(&cursor);
  if (tok) { strncpy(l2_full, tok, sizeof(l2_full)-1); l2_full[sizeof(l2_full)-1]='\0'; }
  else     { l2_full[0] = '\0'; }
}

// ── framed serial reader ────────────────────────────────────────────────
void readSerial()
{
  static byte index = 0;
  static bool inPacket = false;

  while (Serial.available()) {
    char c = Serial.read();

    if (c == '$') { index = 0; inPacket = true; continue; }
    if (!inPacket) continue;

    if (c == '\n') {
      serialBuffer[index] = '\0';
      if (index > 0) { parseData(serialBuffer); newData = true; }
      index = 0;
      inPacket = false;
    } else if (c != '\r') {
      if (index < sizeof(serialBuffer)-1)
        serialBuffer[index++] = c;
      else { index = 0; inPacket = false; }
    }
  }
}

// ── OLED draw ───────────────────────────────────────────────────────────
void drawSpotify()
{
  u8g2.firstPage();
  do {
    // 1. Header — SPOTIFY | PLAY/PAUSE | position
    u8g2.setFont(u8g2_font_5x7_tf);
    u8g2.drawStr(2, 8, "SPOTIFY");
    u8g2.drawStr(54, 8, (playerStatus == 1) ? "PLAY" : "PAUSE");
    int posW = u8g2.getStrWidth(position);
    u8g2.drawStr(126 - posW, 8, position);
    u8g2.drawLine(0, 10, 127, 10);

    // 2. Centered lyrics — typewriter reveal (no underline bar)
    if (strlen(l1_full) > 0) {
      if (strlen(l2_full) == 0) {
        // Single line: 7x13 font, centered at y=30
        u8g2.setFont(u8g2_font_7x13_tf);
        int w = u8g2.getStrWidth(l1_full);
        int x = (128 - w) / 2;
        if (x < 2) x = 2;
        u8g2.drawStr(x, 30, l1_rev);
      } else {
        // Two lines: 6x10 font, centered
        u8g2.setFont(u8g2_font_6x10_tf);
        int w1 = u8g2.getStrWidth(l1_full);
        int x1 = (128 - w1) / 2;
        if (x1 < 2) x1 = 2;
        u8g2.drawStr(x1, 23, l1_rev);

        int w2 = u8g2.getStrWidth(l2_full);
        int x2 = (128 - w2) / 2;
        if (x2 < 2) x2 = 2;
        u8g2.drawStr(x2, 37, l2_rev);
      }
    } else {
      u8g2.setFont(u8g2_font_6x10_tf);
      u8g2.drawStr(48, 30, "~ ~ ~");
    }

    // 3. Progress bar (y:45-49)
    u8g2.drawFrame(2, 45, 124, 4);
    int pW = map(progress, 0, 100, 0, 120);
    if (pW > 0) u8g2.drawBox(4, 46, pW, 2);

    // 4. Footer — Title - Artist (marquee if >128px)
    u8g2.setFont(u8g2_font_5x7_tf);
    int infoW = u8g2.getStrWidth(bottomInfo);
    if (infoW <= 128) {
      int infoX = (128 - infoW) / 2;
      u8g2.drawStr(infoX, 59, bottomInfo);
    } else {
      u8g2.drawStr(-scrollX, 59, bottomInfo);
    }

  } while (u8g2.nextPage());
}
