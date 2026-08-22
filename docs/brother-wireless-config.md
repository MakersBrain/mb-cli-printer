# Brother wireless configuration

Notes on how Brother's iPrint&Label app puts a QL on a wireless network and on
the corresponding `mbprint wifi` implementation. Hardware validation of the
USB control path is still pending.

## How the app does it

`WifiSettingTask.startSetWiFiToDevice` builds an `InfrastructureWiFiSetter`
from the SSID, password, encryption and authentication, calls `createCommand()`,
and hands the resulting bytes to `PrinterDriver.transferBinaryData`. That is the
ordinary printer data channel, the same one raster jobs use, so the mechanism is
transport-agnostic: USB should carry it as well as Wireless Direct does.

`createCommand()` is native. The Java class only marshals parameters across JNI:

```text
decompiled/base/java/sources/com/brother/ptouch/wirelesssettingcommandcreator/
decompiled/base/java/sources/com/brother/ptouch/iprintandlabel/printersetting/WifiSettingTask.java
```

The bytes are assembled in `lib/arm64-v8a/libnative-lib.so` from the arm64 split
APK. That library keeps its C++ symbols, so the relevant functions are named:

| Symbol | Address |
|---|---|
| `br_widicom::InfrastructureWiFiSetter::createCommand` | `0x46c0e4` |
| `br_widicom::InfrastructureWiFiSetter::encrypt` | `0x46bfbc` |
| `br_widicom::InfrastructureWiFiSetter::convToCharCodeStr` | `0x46bc2c` |
| `br_widicom::CommandCreatorUtil::createSendableCommandData` | `0x468e08` |
| `br_widicom::CommandCreatorUtil::concatPjlCommand` | `0x468bb8` |

Addresses are as Binary Ninja loads the library, at base `0x400000`.

## Command structure

The payload is PJL carrying Brother's `OBJBRNET` parameters. String fragments in
the library are `%-12345X@PJL`, `@PJL %s`, `DEFAULT OBJBRNET="`,
`@PJL INQUIRE OBJBRNET`, and `@PJL INFO AVAILABLEWLAN`.

`createSendableCommandData(params, needReboot)` concatenates `pjlHeader`, one
command per parameter, and `pjlFooter`, then appends `rebootCommand` when
`needReboot` is set. The app always sets it.

The parameters are numeric ids rather than dotted OIDs, which is why searching
the library for `1.3.6.1` or `2435` finds nothing. `createCommand` emits them in
this order:

| Id | Value |
|---|---|
| `458867` | `"0"` |
| `458878` | `"1"` |
| `458877` | SSID, through `convToCharCodeStr` |
| `99458890` | obfuscated password, when authentication is WPA-PSK, WPA, or WPA2 |
| `99458889.1` | obfuscated key, when encryption is WEP |
| `458880` | encryption value |
| `458881` | authentication value |
| `459138.2` | present only when `infrastructure` is set |
| `459138.3` | present only when `wirelessDirect` is set |
| `458865` | trailing parameter |

The choice between `99458890` and `99458889.1` is a bitmask test on the
authentication value, `auth <= 0x13 && (1 << auth) & 0xc0008`, so bits 3, 18 and
19. Those are `wpaPsk`, `wpaOnly` and `wpa2Only` in the Java enum, which the
native code numbers identically.

Fields sit at these offsets in the C++ object: SSID `+0x10`, password `+0x28`,
encryption `+0x40`, authentication `+0x44`, `infrastructure` `+8`,
`wirelessDirect` `+9`, `needReboot` `+0xa`.

## Password obfuscation

`encrypt` is a repeating 16-byte XOR against a constant at `0x44c1c0`. There is
no key derivation and no state:

```python
KEY = bytes.fromhex("0daee4a18b7f265e725b177a71cdec4d")


def encrypt(value: bytes) -> bytes:
    return bytes(b ^ KEY[i % len(KEY)] for i, b in enumerate(value))
```

It is an involution, so the same function recovers a password from a captured
command. Treat a captured blob as containing the plaintext credential.

## Recovered value and envelope encodings

`458880` and `458881` are the decimal representations of the Java encryption
and authentication enum values. The mode values are decimal booleans; the app
sends infrastructure `1`, Wireless Direct `0`, and trailing `458865` value `1`.
An SSID is its UTF-8 bytes written as lowercase hex with a `-` before every
byte. Each parameter is serialized as:

```text
@PJL DEFAULT OBJBRNET="OID:value"\r\n
```

The complete envelope is:

```text
ESC%-12345X@PJL\r\n
...parameters...
ESC%-12345X
1b 69 58 2a 31 03 00 01 2e 00 00 00 2c 00   (reboot)
```

Generate a capture without touching hardware:

```sh
printf '%s\n' 'network password' | mbprint wifi --ssid 'Maker WiFi' \
  --password-stdin --dry-run --out brother-wifi.bin
```

Send it after first validating the transport on the target printer:

```sh
printf '%s\n' 'network password' | mbprint wifi --ssid 'Maker WiFi' \
  --password-stdin --transport usb --yes
```

The reversed read-only commands are also available for capture-testing:

```sh
mbprint usb-info
mbprint wifi status --raw
mbprint wifi scan --scan-wait 5 --raw
```

`usb-info` uses the standard USB Printer Class `GET_DEVICE_ID` and
`GET_PORT_STATUS` control requests. `wifi status` asks for `OBJBRNET` and
decodes wireless OID `458867` plus IPv4 OID `458967.2`. `wifi scan` starts the
Brother AP search (`458845:31-3a`) and then requests `INFO AVAILABLEWLAN`.
Use `--raw` during hardware validation because firmware variants may return a
row shape the conservative decoder does not yet recognize.

The capture contains a recoverable credential. Keep it out of version control
and delete it when it is no longer needed.

## What is not established

- Whether the QL accepts this on the channel `mbprint` prints through. A
  QL-1110NWB on USB did not answer read-only PJL (`@PJL INFO ID`,
  `INFO STATUS`, `INFO AVAILABLEWLAN`) on the interface 0 alt 0 bulk pair,
  either cold or after `ESC @`. It returned only leftover raster status blocks,
  then nothing. Interface 0 alt 1 and interface 1 alt 1 are both printer class
  with protocol 4. USB Printer Class 1.1 assigns IEEE 1284.4 to protocol 3 and
  reserves protocol 4, so Brother's interface must be capture-tested rather
  than assumed to use standard 1284.4 framing. Use `--usb-interface` and
  `--usb-alt` to select the descriptor pair while testing. See the
  [USB-IF Printer Class 1.1 specification](https://www.usb.org/sites/default/files/usbprint11a021811.pdf).

Capturing one real setup from the app would validate the recovered generator,
settle the USB framing, and give a known-good blob to compare against.

## Prior work

A search in August 2026 found nothing public covering this. `brother_ql` and its
forks document the raster language only, and Brother's own material describes
wireless setup solely through BRAdmin, WPS, the control panel or the web
interface, never as a byte stream. The `OBJBRNET` parameter, the numeric ids and
the class names in the library are all unindexed. Absence from search results is
not proof that nobody has done this, but there appears to be no published
description to check these notes against.

## Reproducing the analysis

Extract the library, which is ignored by git along with the rest of
`decompiled/`:

```sh
nix run .#pull-apk
nix run .#decompile-apk
unzip -o -j apk/com.brother.ptouch.iprintandlabel/split_config.arm64_v8a.apk \
  lib/arm64-v8a/libnative-lib.so -d decompiled/base/lib
```

Then open `decompiled/base/lib/libnative-lib.so` in Ghidra through
`nix develop .#native`, or in Binary Ninja. The symbols above are enough to
navigate without further setup.
