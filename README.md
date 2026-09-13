# Spotify-OLED

Karaoke lyrics display untuk layar OLED SSD1306 (128x64) via Arduino UNO,
dengan lirik tersinkronisasi real-time dari pemutar Spotify.

## Demo

```
┌──────────────────────────────────────────┐
│  SPOTIFY  PLAY   [position]              │
├──────────────────────────────────────────┤
│              ~ ~ ~                        │
│            Purple rain,                   │
│                                            │
│  ████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │
│        Purple Rain - Prince               │
└──────────────────────────────────────────┘
```

## Fitur

- **Lirik tersinkronisasi di tengah layar** — reveal per karakter (typewriter)
  mengikuti timing word-level dari file LRC (Enhanced LRC jika tersedia).
- **Judul & penyanyi di footer** — marquee otomatis jika teks melebihi 128px.
- **Progress bar** timeline dengan posisi aktual.
- **Header** berisi status PLAY/PAUSE dan posisi saat ini.
- **Deteksi ganti lagu** lewat `mpris:trackid` — langsung respon ganti lagu.
- **Fetch lirik di background thread** — tidak pernah memblokir serial, jadi
  tidak ada latency pada tampilan.
- **Interpolasi posisi** via monotonic clock antara polling playerctl (0.5s)
  sehingga reveal lirik mulus tanpa perlu query terus-menerus.
- **Dukungan offset vokal** (`-o`) untuk menyetel sync jika lirik meleset.
- **Fallback lirik**: LRCLIB `syncedLyrics` → `plainLyrics` (distribusi
  sintetis merata) → pencarian LRCLIB.
- **Fallback pemutar**: jalan tanpa DBus; cukup `playerctl` + Spotify.

## Arsitektur

```
Spotify ──(MPRIS)──▶ playerctl ──poll 0.5s──▶ spotify_oled.py
                                                  │
                    LRCLIB ◀──fetch bg thread──┘  │ interpolate pos
                                                  │
                                     serial 115200 ($-framed)
                                                  │
                              Arduino UNO + SSD1306 128x64
```

Pipeline dimulai dari `playerctl metadata` (status, volume, judul, artis,
posisi mikro-detik, durasi, `mpris:trackid`). Posisi di-interpolasi dari
monotonic clock. Lirik di-ambil dari LRCLIB di thread terpisah sehingga
pergantian lagu langsung tampil di footer, dan lirik muncul begitu siap.

## Persyaratan

- Python 3.10+, `pyserial`, `playerctl`
- `playerctl` versi apa pun yang mendukung `{{mpris:trackid}}`
- `arduino-cli` untuk compile/upload sketch
- Aplikasi Spotify desktop berjalan & memutar lagu

Jalankan bridge via `uv` tanpa install manual:

```bash
uv run --with pyserial python3 spotify_oled.py
```

## Setup Hardware

| Komponen | Pin Arduino UNO |
|----------|-----------------|
| SSD1306 128x64 | SDA → A4, SCL → A5, VCC → 5V, GND → GND |

## Upload Sketch

```bash
arduino-cli compile --fqbn arduino:avr:uno sketch_sep13a
arduino-cli upload -p /dev/ttyACM0 --fqbn arduino:avr:uno sketch_sep13a
```

Ubah `/dev/ttyACM0` sesuai port Arduino Anda (lihat `ls /dev/ttyACM*`).

## Menjalankan

Bridge otomatis mendeteksi port dan baud 115200:

```bash
uv run --with pyserial python3 spotify_oled.py
```

### Opsi

| Opsi | Deskripsi |
|------|-----------|
| `-p, --port` | Port serial (default: auto-detect) |
| `-b, --baud` | Baud rate (default: 115200) |
| `-o, --offset` | Offset vokal detik; positif = lirik tampil lebih cepat |
| `--poll` | Interval polling playerctl (default: 0.5s) |

Contoh dengan offset:

```bash
# Jika lirik terasa telat 0.3 detik terhadap audio:
uv run --with pyserial python3 spotify_oled.py -o 0.3
```

## Sync Lirik

Sync lirik bergantung pada **dua sumber timing**:

1. **Posisi putar** datang dari MPRIS/playerctl (mikro-detik). Antara polling,
   posisi di-interpolasi dengan `time.monotonic()` sehingga reveal lirik mulus.
2. **Timing lirik** berasal dari file LRC di LRCLIB. Kualitas sync sebagian
   besar ditentukan oleh data LRC itu sendiri.

Jika lirik meleset (telat/cepat) terhadap audio, gunakan `-o`:
- Lirik telat → **offset positif** (mis. `-o 0.3`)
- Lirik terlalu cepat → **offset negatif** (mis. `-o -0.2`)

### Opsi lanjutan: Spotify Web API (progress yang lebih presisi)

Jika ingin posisi yang lebih akurat dari pemutar (terutama saat seek/skip dan
latensi MPRIS), `playerctl` sudah memadai untuk sebagian besar kasus. Untuk
presisi maksimum, opsi lanjutan adalah menembak endpoint
`GET /v1/me/player/currently-playing` dari [Spotify Web API][spotify-api]
(butuh OAUTH token) dan memakai `progress_ms` sebagai sumber anchor Timing.
MPRIS di Linux umumnya sudah melaporkan posisi dengan akurat, jadi tidak
wajib.

[spotify-api]: https://developer.spotify.com/documentation/web-api/reference/get-the-users-currently-playing-track

## Protokol Serial

Frame dipisah dengan prefix `$` dan diakhiri `\n`, baud 115200:

```
$status;volume;title;artist;position;duration;progress;l1_rev;l1_full;l2_rev;l2_full\n
```

| Field | Contoh | Arti |
|-------|--------|------|
| `status` | `1` | 1 = playing, 0 = paused |
| `volume` | `61` | Volume (0-100) |
| `title` | `Purple Rain` | Judul lagu |
| `artist` | `Prince` | Penyanyi |
| `position` | `03:12` | Posisi saat ini (mm:ss) |
| `duration` | `04:05` | Durasi lagu (mm:ss) |
| `progress` | `78` | Persen progress (0-100) |
| `l1_rev` | `Purple` | Line 1, teks yang sudah "di-reveal" |
| `l1_full` | `Purple rain,` | Line 1, teks penuh (untuk centering) |
| `l2_rev` | `purple` | Line 2, teks revealed (kosong jika satu baris) |
| `l2_full` | `purple rain` | Line 2, teks penuh |

Arduino memakai tokenizer berbasis `strchr` (`getNextToken`) yang aman
untuk field kosong — tidak seperti `strtok` bawaan AVR yang mengabaikan
kumpulan semicolon kosong.

## Cara Kerja Sync Reveal

1. `parse_lrc_precise()` mem-parsing LRC. Prioritas:
   - **Enhanced LRC** `<mm:ss.xx>word` — timing per kata asli dari file.
   - **Standard LRC** `[mm:ss.xx] line` — kata didistribusikan merata di
     rentang waktu antar-baris.
2. `split_char_timelines()` membangun timeline per karakter yang *monotonik*
   global (line 2 tidak pernah mulai sebelum line 1 selesai) — reveal selalu
   mengikuti urutan nyanyian.
3. `chars_revealed()` menggabungkan karakter yang waktunya telah lewat →
   efek typewriter `hu...huj...huja...hujan` sesuai posisi.
4. `get_active_lyrics()` menjaga baris tetap tampil selama jeda instrumental
   (tidak berkedip).

## Struktur Proyek

```
Spotify-OLED/
├── spotify_oled.py        # bridge Python (polling + interpolasi + fetch)
├── sketch_sep13a/
│   └── sketch_sep13a.ino  # firmware Arduino (layout OLED + parsing serial)
└── README.md
```

## Lisensi

MIT